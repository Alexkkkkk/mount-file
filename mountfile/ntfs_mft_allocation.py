# SPDX-License-Identifier: MIT
"""Bounded planning for safe MFT record reuse ($MFTMirr aware).

This module plans, but never performs, MFT record allocation: it decides
whether a crashed image may hand out a free FILE record slot with an
incremented sequence number.  Every refusal is typed; nothing here writes
to an image.  Reuse is planned only when:

* both the $MFT candidate record and its $MFTMirr copy parse with valid
  USA fixups;
* the candidate record is marked not-in-use (actually free);
* the log reference names the candidate slot and its sequence equals the
  candidate sequence + 1 (when the successor is supported);
* the $MFTMirr copy is byte-identical to the USA-restored $MFT record.

The NTFS replay executor can use this planner as an explicit pre-write gate
when a filesystem-aware candidate provider is supplied. The provider still
owns locating records and the executor still refuses actual allocation or
publication without an independent checker (tracker #510, item C5).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from mountfile.ntfs_mft import (
    NtfsRecordError,
    decode_ntfs_file_reference,
    parse_ntfs_file_record_header,
)


class NtfsMftAllocationError(ValueError):
    """Typed refusal to allocate or reuse an MFT record slot."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class MftAllocationPlan:
    """A validated plan to reuse one free MFT record slot."""

    record_number: int
    previous_sequence: int
    next_sequence: int
    mirror_consistent: bool


@dataclass(frozen=True)
class MftAllocationCandidate:
    """A provider-owned, bounded candidate that can be mutated on a stage.

    Offsets are physical image offsets.  Keeping them in the provider result
    prevents the replay executor from guessing MFT geometry or mirror layout.
    ``raw_record`` and ``raw_mirror`` are source identity guards and must be
    the exact on-disk bytes read by the provider.
    """

    raw_record: bytes
    raw_mirror: bytes
    reference: int
    record_number: int
    record_offset: int
    mirror_offset: int
    bitmap_offset: int = -1
    bitmap_bytes: bytes = b""
    bitmap_bit: int = -1


def plan_mft_record_reuse(
    raw_record: object,
    raw_mirror: object,
    reference: int,
    *,
    candidate_record_number: int,
) -> MftAllocationPlan:
    """Plan reuse of a free MFT record slot backed by a consistent mirror.

    ``raw_record`` and ``raw_mirror`` are the raw $MFT and $MFTMirr record
    bytes; ``reference`` is the NTFS file reference from the log entry that
    wants the slot. ``candidate_record_number`` identifies the MFT slot from
    which both record buffers were read and must match the reference's record
    number. Refusals are typed via :class:`NtfsMftAllocationError`.
    """
    try:
        record = parse_ntfs_file_record_header(raw_record, require_in_use=False)
    except NtfsRecordError as exc:
        raise NtfsMftAllocationError("candidate_record_invalid", str(exc)) from exc
    if record.in_use:
        raise NtfsMftAllocationError(
            "candidate_record_in_use", f"sequence {record.sequence_number}"
        )
    try:
        mirror = parse_ntfs_file_record_header(raw_mirror, require_in_use=False)
    except NtfsRecordError as exc:
        raise NtfsMftAllocationError("mirror_record_invalid", str(exc)) from exc
    if mirror.fixed_record != record.fixed_record:
        raise NtfsMftAllocationError(
            "mft_mirr_mismatch",
            f"$MFT sequence {record.sequence_number} != mirror content",
        )
    if record.sequence_number == 0xFFFF:
        # Sequence zero is rejected by decode_ntfs_file_reference().  Do not
        # manufacture an unusable next reference by wrapping modulo 2**16;
        # the correct NTFS reuse behavior at exhaustion needs independent
        # format evidence before this planner can safely proceed.
        raise NtfsMftAllocationError(
            "sequence_exhausted",
            "candidate sequence 0xFFFF has no supported successor",
        )
    try:
        record_number, sequence = decode_ntfs_file_reference(reference)
    except NtfsRecordError as exc:
        raise NtfsMftAllocationError("reference_invalid", str(exc)) from exc
    if (
        type(candidate_record_number) is not int
        or candidate_record_number < 0
        or candidate_record_number >= (1 << 48)
    ):
        raise NtfsMftAllocationError(
            "candidate_record_number_invalid",
            "candidate slot is outside the 48-bit MFT record range",
        )
    if record_number != candidate_record_number:
        raise NtfsMftAllocationError(
            "candidate_record_mismatch",
            f"log reference names {record_number}, candidate slot is {candidate_record_number}",
        )
    expected = record.sequence_number + 1
    if sequence != expected:
        raise NtfsMftAllocationError(
            "sequence_mismatch",
            f"reference sequence {sequence} != candidate {record.sequence_number} + 1",
        )
    return MftAllocationPlan(
        record_number=record_number,
        previous_sequence=record.sequence_number,
        next_sequence=sequence,
        mirror_consistent=True,
    )


def fixed_record_to_raw(fixed_record: object) -> bytes:
    """Encode a USA-restored FILE record for a bounded staged write."""
    try:
        raw = bytearray(bytes(fixed_record))
    except (TypeError, ValueError) as exc:
        raise NtfsMftAllocationError(
            "initialized_record_invalid", "not bytes-like"
        ) from exc
    if len(raw) < 512 or len(raw) % 512:
        raise NtfsMftAllocationError(
            "initialized_record_invalid", "invalid record size"
        )
    try:
        fixup_offset, fixup_count = struct.unpack_from("<HH", raw, 4)
        usn = struct.unpack_from("<H", raw, fixup_offset)[0]
        if fixup_count != len(raw) // 512 + 1:
            raise ValueError("USA count does not match record size")
        for index in range(fixup_count - 1):
            trailer = (index + 1) * 512 - 2
            original = struct.unpack_from("<H", raw, trailer)[0]
            struct.pack_into("<H", raw, fixup_offset + 2 * (index + 1), original)
            struct.pack_into("<H", raw, trailer, usn)
    except (IndexError, struct.error, ValueError) as exc:
        raise NtfsMftAllocationError("initialized_record_invalid", str(exc)) from exc
    return bytes(raw)


__all__ = [
    "MftAllocationCandidate",
    "MftAllocationPlan",
    "NtfsMftAllocationError",
    "fixed_record_to_raw",
    "plan_mft_record_reuse",
]
