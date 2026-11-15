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


def _resolve_targets(
    analysis: NtfsLogFileAnalysis, resolver: ReplayResolver
) -> dict[tuple[int, int], ReplayTarget]:
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
    for step in (*analysis.redo_plan, *analysis.undo_plan):
        if step.operation_name in {"Noop", "CommitTransaction", "ForgetTransaction"}:
            continue
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
    return targets


def _replay_staged(
    stream: BinaryIO,
    analysis: NtfsLogFileAnalysis,
    resolver: ReplayResolver,
) -> tuple[OverlayProvenance, ...]:
    targets = _resolve_targets(analysis, resolver)
    _fault("mutation:start")
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
) -> NtfsReplayPublication:
    """Replay a validated analysis on a disposable copy and atomically publish it.

    ``resolver`` is mandatory. No implicit MFT arithmetic is performed. A
    caller that cannot resolve every operation gets a typed refusal before the
    staged image is modified. ``verify`` should independently validate the
    staged NTFS metadata (for example with a platform checker) before publish.
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
            provenance = _replay_staged(stream, analysis, resolver)
            if verify is not None:
                verify(temporary)
            _fsync_path(temporary)
        _require_source_unchanged(source_path, before)
        _fault("publish:start")
        _atomic_replace(source_path, temporary)
        temporary = ""
        _fault("publish:done")
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
    "ReplayResolver",
    "publish_ntfs_replay",
]
