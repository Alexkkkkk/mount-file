# SPDX-License-Identifier: MIT
"""Fail-closed NTFS replay publication for resolver-backed plans.

The ``$LogFile`` parser deliberately does not guess MFT/runlist locations. This
module supplies the missing publication boundary without weakening that rule:
a caller must provide a resolver for every replay step. The executor stages a
copy, validates the complete plan before the first write, applies redo then undo,
flushes and reads back every changed range, verifies source identity, and only
then atomically publishes. Resolver failures leave the source untouched.

The resolver is intentionally a callback rather than an implicit heuristic. A
future filesystem-aware resolver can implement MFT allocation, ``$MFTMirr``
repair, attribute-list expansion and index updates while retaining the same
transactional boundary.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO

from mountfile.native_write import (
    _atomic_replace,
    _fault,
    _fsync_path,
    _image_copy,
    _require_source_unchanged,
    _sha256_file,
    _unlink,
)
from mountfile.ntfs_logfile import (
    NtfsLogFileAnalysis,
    NtfsReplayExecutorError,
    OverlayProvenance,
    ReplayStep,
    ReplayTarget,
    replay_analysis_to_stream,
)
from mountfile.ntfs_mft import parse_ntfs_file_record_header
from mountfile.ntfs_mft_allocation import (
    MftAllocationCandidate,
    NtfsMftAllocationError,
    fixed_record_to_raw,
    plan_mft_record_reuse,
)


class NtfsReplayResolutionError(NtfsReplayExecutorError):
    """A log step could not be mapped to a validated on-disk byte range."""


@dataclass(frozen=True)
class NtfsReplayPublication:
    """Evidence returned after a staged replay is atomically published."""

    source: str
    sha256_before: str
    sha256_after: str
    provenance: tuple[OverlayProvenance, ...]


ReplayResolver = Callable[[ReplayStep], ReplayTarget]
MftCandidateProvider = Callable[
    [ReplayStep], MftAllocationCandidate | tuple[object, object, int, int] | None
]

# These operations require coordinated MFT/$MFTMirr, bitmap, or index writes;
# a byte-range resolver and candidate precondition check cannot authorize them.
_ALLOCATION_SENSITIVE_OPERATIONS = frozenset(
    (0x02, 0x03, *range(0x0C, 0x17), *range(0x21, 0x25))
)
_MAX_MFT_CANDIDATE_RANGE_BYTES = 1 << 20
_MAX_REPLAY_TARGET_BYTES = 1 << 30


def _validate_target_geometry(
    targets: dict[tuple[int, int], ReplayTarget],
) -> None:
    """Reject malformed or physically overlapping targets before mutation."""
    ranges: list[tuple[int, int, tuple[int, int]]] = []
    for key, target in targets.items():
        values = (target.target_attr, target.target_vcn, target.offset, target.length)
        if any(type(value) is not int for value in values):
            raise NtfsReplayResolutionError(
                f"resolver target {key} contains a non-integer coordinate"
            )
        if target.offset < 0 or target.length <= 0:
            raise NtfsReplayResolutionError(f"resolver target {key} range is invalid")
        if target.length > _MAX_REPLAY_TARGET_BYTES:
            raise NtfsReplayResolutionError(
                f"resolver target {key} exceeds the bounded range limit"
            )
        end = target.offset + target.length
        if end <= target.offset or end > (1 << 63):
            raise NtfsReplayResolutionError(f"resolver target {key} range overflows")
        ranges.append((target.offset, end, key))
    for index, (start, end, key) in enumerate(ranges):
        for other_start, other_end, other_key in ranges[index + 1 :]:
            if start < other_end and other_start < end:
                raise NtfsReplayResolutionError(
                    f"resolver targets {key} and {other_key} overlap"
                )


def _validate_disjoint_ranges(
    ranges: tuple[tuple[int, bytes, str], ...],
) -> None:
    """Reject overlapping physical extents used by coordinated metadata writes."""
    normalized: list[tuple[int, int, str]] = []
    for offset, payload, name in ranges:
        if type(offset) is not int or offset < 0 or not isinstance(payload, bytes):
            raise NtfsReplayResolutionError(f"{name} extent is invalid")
        end = offset + len(payload)
        if end <= offset or end > (1 << 63):
            raise NtfsReplayResolutionError(f"{name} extent overflows")
        normalized.append((offset, end, name))
    for index, (start, end, name) in enumerate(normalized):
        for other_start, other_end, other_name in normalized[index + 1 :]:
            if start < other_end and other_start < end:
                raise NtfsReplayResolutionError(
                    f"{name} and {other_name} candidate extents overlap"
                )


def _validate_mft_candidate(
    step: ReplayStep,
    provider: MftCandidateProvider | None,
) -> MftAllocationCandidate | None:
    """Validate a provider-supplied MFT/$MFTMirr candidate before mutation."""
    if provider is None:
        return None
    try:
        candidate = provider(step)
    except NtfsMftAllocationError:
        raise
    except Exception as exc:
        raise NtfsReplayResolutionError(
            f"MFT candidate provider failed for LSN {step.lsn}: {exc}"
        ) from exc
    if candidate is None:
        return None
    if isinstance(candidate, MftAllocationCandidate):
        raw_record = candidate.raw_record
        raw_mirror = candidate.raw_mirror
        reference = candidate.reference
        record_number = candidate.record_number
        for name in ("record_offset", "mirror_offset"):
            value = getattr(candidate, name)
            if type(value) is not int or value < 0:
                raise NtfsReplayResolutionError(f"MFT candidate {name} is invalid")
    elif isinstance(candidate, tuple) and len(candidate) == 4:
        raw_record, raw_mirror, reference, record_number = candidate
    else:
        raise NtfsReplayResolutionError(
            "MFT candidate provider must return MftAllocationCandidate, a 4-tuple or None"
        )
    try:
        plan_mft_record_reuse(
            raw_record,
            raw_mirror,
            reference,
            candidate_record_number=record_number,
        )
    except NtfsMftAllocationError as exc:
        raise NtfsReplayResolutionError(
            f"MFT allocation refused for LSN {step.lsn}: {exc.reason}"
        ) from exc
    return candidate if isinstance(candidate, MftAllocationCandidate) else None


def _resolve_targets(
    analysis: NtfsLogFileAnalysis,
    resolver: ReplayResolver,
    mft_candidate_provider: MftCandidateProvider | None = None,
) -> tuple[dict[tuple[int, int], ReplayTarget], dict[int, MftAllocationCandidate]]:
    if not isinstance(analysis, NtfsLogFileAnalysis):
        raise NtfsReplayResolutionError("replay analysis has an invalid type")
    if analysis.truncated:
        raise NtfsReplayResolutionError(
            "cannot publish replay from a truncated $LogFile stream"
        )
    if not callable(resolver):
        raise NtfsReplayResolutionError(
            "a filesystem-aware replay resolver is required"
        )
    targets: dict[tuple[int, int], ReplayTarget] = {}
    allocations: dict[int, MftAllocationCandidate] = {}
    for step in (*analysis.redo_plan, *analysis.undo_plan):
        if step.operation_name in {"Noop", "CommitTransaction", "ForgetTransaction"}:
            continue
        if (
            step.operation in _ALLOCATION_SENSITIVE_OPERATIONS
            and step.operation != 0x02
        ):
            raise NtfsReplayResolutionError(
                f"allocation-sensitive operation {step.operation_name} is refused"
            )
        candidate = _validate_mft_candidate(step, mft_candidate_provider)
        if step.operation in _ALLOCATION_SENSITIVE_OPERATIONS:
            if step.operation != 0x02 or candidate is None:
                raise NtfsReplayResolutionError(
                    f"allocation-sensitive operation {step.operation_name} is refused"
                )
            allocations[step.lsn] = candidate
        try:
            target = resolver(step)
        except NtfsReplayExecutorError:
            raise
        except Exception as exc:
            raise NtfsReplayResolutionError(
                f"resolver failed for LSN {step.lsn}: {exc}"
            ) from exc
        if not isinstance(target, ReplayTarget):
            raise NtfsReplayResolutionError(
                f"resolver returned an invalid target for LSN {step.lsn}"
            )
        key = (step.target_attr, step.target_vcn)
        if target.target_attr != key[0] or target.target_vcn != key[1]:
            raise NtfsReplayResolutionError(
                f"resolver target identity mismatch for LSN {step.lsn}"
            )
        previous = targets.get(key)
        if previous is not None and previous != target:
            raise NtfsReplayResolutionError(
                f"resolver returned inconsistent targets for attr/vcn {key}"
            )
        targets[key] = target
    _validate_target_geometry(targets)
    return targets, allocations


def _read_candidate_range(
    stream: BinaryIO, offset: int, expected: bytes, name: str
) -> bytes:
    """Read one provider-bounded range without materializing the image."""
    if type(offset) is not int or offset < 0:
        raise NtfsReplayResolutionError(f"{name} candidate range is invalid")
    if not isinstance(expected, bytes) or not expected:
        raise NtfsReplayResolutionError(f"{name} candidate bytes are invalid")
    if len(expected) > _MAX_MFT_CANDIDATE_RANGE_BYTES:
        raise NtfsReplayResolutionError(
            f"{name} candidate range exceeds the bounded read limit"
        )
    try:
        stream.seek(offset)
        data = bytearray()
        while len(data) < len(expected):
            chunk = stream.read(
                min(_MAX_MFT_CANDIDATE_RANGE_BYTES, len(expected) - len(data))
            )
            if not chunk:
                raise NtfsReplayResolutionError(
                    f"{name} candidate range is out of bounds"
                )
            data.extend(chunk)
    except NtfsReplayResolutionError:
        raise
    except (OSError, ValueError, OverflowError) as exc:
        raise NtfsReplayResolutionError(
            f"{name} candidate range is out of bounds"
        ) from exc
    return bytes(data)


def _replay_mft_allocation(
    stream: BinaryIO,
    analysis: NtfsLogFileAnalysis,
    targets: dict[tuple[int, int], ReplayTarget],
    allocations: dict[int, MftAllocationCandidate],
) -> tuple[OverlayProvenance, ...]:
    """Apply only a full-record InitializeFileRecordSegment allocation.

    The candidate provider supplies both physical ranges.  The executor checks
    source identity on the disposable stage, validates the initialized FILE
    record and writes identical USA-encoded bytes to $MFT and $MFTMirr.
    """
    steps = (*analysis.redo_plan, *analysis.undo_plan)
    allocation_steps = [step for step in steps if step.lsn in allocations]
    other_steps = [
        step for step in steps if step.operation not in _ALLOCATION_SENSITIVE_OPERATIONS
    ]
    if other_steps:
        raise NtfsReplayResolutionError(
            "MFT allocation replay cannot be combined with other replay operations"
        )
    if len(allocation_steps) != 1:
        raise NtfsReplayResolutionError(
            "bounded MFT allocation requires exactly one InitializeFileRecordSegment"
        )
    step = allocation_steps[0]
    candidate = allocations[step.lsn]
    target = targets.get((step.target_attr, step.target_vcn))
    if target is None or target.offset != candidate.record_offset:
        raise NtfsReplayResolutionError(
            "MFT allocation target does not match candidate range"
        )
    if target.length != len(candidate.raw_record) or len(step.payload) != target.length:
        raise NtfsReplayResolutionError(
            "MFT allocation payload is not one complete FILE record"
        )
    if (
        type(candidate.bitmap_offset) is not int
        or candidate.bitmap_offset < 0
        or not isinstance(candidate.bitmap_bytes, bytes)
        or not candidate.bitmap_bytes
        or type(candidate.bitmap_bit) is not int
        or candidate.bitmap_bit < 0
        or candidate.bitmap_bit >= len(candidate.bitmap_bytes) * 8
    ):
        raise NtfsReplayResolutionError(
            "MFT allocation requires an explicit physical bitmap byte range"
        )
    ranges = (
        (candidate.record_offset, candidate.raw_record, "MFT"),
        (candidate.mirror_offset, candidate.raw_mirror, "MFT mirror"),
        (candidate.bitmap_offset, candidate.bitmap_bytes, "bitmap"),
    )
    _validate_disjoint_ranges(ranges)
    for offset, expected, name in ranges:
        if _read_candidate_range(stream, offset, expected, name) != expected:
            raise NtfsReplayResolutionError(
                f"{name} candidate source identity changed on stage"
            )
    bit_mask = 1 << (candidate.bitmap_bit % 8)
    bitmap_index = candidate.bitmap_bit // 8
    if candidate.bitmap_bytes[bitmap_index] & bit_mask:
        raise NtfsReplayResolutionError(
            "MFT allocation bitmap bit is already allocated"
        )
    try:
        initialized = parse_ntfs_file_record_header(step.payload)
    except Exception as exc:
        raise NtfsReplayResolutionError("initialized MFT record is invalid") from exc
    plan = plan_mft_record_reuse(
        candidate.raw_record,
        candidate.raw_mirror,
        candidate.reference,
        candidate_record_number=candidate.record_number,
    )
    if initialized.sequence_number != plan.next_sequence or not initialized.in_use:
        raise NtfsReplayResolutionError(
            "initialized MFT record has an invalid successor identity"
        )
    raw_initialized = fixed_record_to_raw(initialized.fixed_record)
    if len(raw_initialized) != len(candidate.raw_record):
        raise NtfsReplayResolutionError(
            "initialized MFT record size differs from candidate"
        )
    updated_bitmap = bytearray(candidate.bitmap_bytes)
    updated_bitmap[bitmap_index] |= bit_mask
    writes = (
        (candidate.record_offset, raw_initialized),
        (candidate.mirror_offset, raw_initialized),
        (candidate.bitmap_offset, bytes(updated_bitmap)),
    )
    for offset, payload in writes:
        stream.seek(offset)
        if stream.write(payload) != len(payload):
            raise NtfsReplayResolutionError(
                "short write during MFT/$MFTMirr allocation"
            )
    stream.flush()
    with suppress(AttributeError, OSError):
        os.fsync(stream.fileno())
    for offset, expected in writes:
        stream.seek(offset)
        if stream.read(len(expected)) != expected:
            raise NtfsReplayResolutionError(
                "MFT/$MFTMirr/bitmap allocation read-back mismatch"
            )
    return (
        OverlayProvenance(
            step.lsn,
            step.transaction_id,
            step.kind,
            step.operation_name,
            step.target_attr,
            step.target_vcn,
            candidate.record_offset,
            len(raw_initialized),
        ),
    )


def _replay_staged(
    stream: BinaryIO,
    analysis: NtfsLogFileAnalysis,
    resolver: ReplayResolver,
    mft_candidate_provider: MftCandidateProvider | None = None,
) -> tuple[OverlayProvenance, ...]:
    targets, allocations = _resolve_targets(analysis, resolver, mft_candidate_provider)
    _fault("mutation:start")
    if allocations:
        return _replay_mft_allocation(stream, analysis, targets, allocations)
    provenance = replay_analysis_to_stream(
        analysis, stream, targets, sync=lambda: _fsync_stream(stream)
    )
    _fault("mutation:done")
    return provenance


def _fsync_stream(stream: BinaryIO) -> None:
    """Durability barrier for a real file stream; test doubles may omit fileno."""
    stream.flush()
    try:
        os.fsync(stream.fileno())
    except (AttributeError, OSError):
        # In-memory fixtures have no OS fd; read-back still remains mandatory.
        return


def publish_ntfs_replay(
    image_path: str,
    analysis: NtfsLogFileAnalysis,
    resolver: ReplayResolver,
    *,
    verify: Callable[[str], None] | None = None,
    mft_candidate_provider: MftCandidateProvider | None = None,
) -> NtfsReplayPublication:
    """Replay a validated analysis on a disposable copy and atomically publish it.

    ``resolver`` is mandatory. No implicit MFT arithmetic is performed. When
    ``mft_candidate_provider`` is supplied, each candidate is validated
    against both its USA-restored mirror and the log reference before the
    staged image is modified. ``verify`` should independently validate the
    staged NTFS metadata before publish.
    """
    source = os.path.abspath(os.fspath(image_path))
    try:
        before = _sha256_file(source)
    except OSError as exc:
        raise NtfsReplayExecutorError(f"cannot hash NTFS image: {exc}") from exc
    source_path, temporary = _image_copy(source)
    provenance: tuple[OverlayProvenance, ...] = ()
    try:
        with open(temporary, "r+b", buffering=0) as stream:
            provenance = _replay_staged(
                stream, analysis, resolver, mft_candidate_provider
            )
            if verify is not None:
                verify(temporary)
            _fsync_path(temporary)
        _require_source_unchanged(source_path, before)
        # ``_atomic_replace`` owns the publication boundary and emits both
        # publish fault points.  Do not emit them here as well: duplicate
        # boundaries make process-kill acceptance ambiguous and can kill a
        # caller twice at the same logical point.
        _atomic_replace(source_path, temporary)
        temporary = ""
    finally:
        if temporary:
            _unlink(temporary)
    try:
        after = _sha256_file(source_path)
    except OSError as exc:
        raise NtfsReplayExecutorError(
            f"cannot hash published NTFS image: {exc}"
        ) from exc
    return NtfsReplayPublication(
        source=source_path,
        sha256_before=before.hex(),
        sha256_after=after.hex(),
        provenance=provenance,
    )


__all__ = [
    "NtfsReplayPublication",
    "NtfsReplayResolutionError",
    "MftCandidateProvider",
    "ReplayResolver",
    "publish_ntfs_replay",
]
