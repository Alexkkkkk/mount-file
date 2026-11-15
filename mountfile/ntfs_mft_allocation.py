# SPDX-License-Identifier: MIT
"""Bounded planning for safe MFT record reuse ($MFTMirr aware).

This module plans, but never performs, MFT record allocation: it decides
whether a crashed image may hand out a free FILE record slot with an
incremented sequence number.  Every refusal is typed; nothing here writes
to an image.  Reuse is planned only when:

* both the $MFT candidate record and its $MFTMirr copy parse with valid
  USA fixups;
* the candidate record is marked not-in-use (actually free);
* the log reference sequence equals the candidate sequence + 1 (mod 2**16);
* the $MFTMirr copy is byte-identical to the USA-restored $MFT record.

Wiring this planner into a write path is intentionally out of scope here;
until that lands with an independent checker, replay stays refused for
allocation questions (tracker #510, item C5).
"""

from __future__ import annotations

from dataclasses import dataclass

from mountfile.ntfs_mft import (
    NtfsRecordError,
    decode_ntfs_file_reference,
    parse_ntfs_file_record_header,
)

_SEQUENCE_MODULUS = 1 << 16


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


def plan_mft_record_reuse(
    raw_record: object, raw_mirror: object, reference: int
) -> MftAllocationPlan:
    """Plan reuse of a free MFT record slot backed by a consistent mirror.

    ``raw_record`` and ``raw_mirror`` are the raw $MFT and $MFTMirr record
    bytes; ``reference`` is the NTFS file reference from the log entry that
    wants the slot.  Refusals are typed via :class:`NtfsMftAllocationError`.
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
    try:
        record_number, sequence = decode_ntfs_file_reference(reference)
    except NtfsRecordError as exc:
        raise NtfsMftAllocationError("reference_invalid", str(exc)) from exc
    expected = (record.sequence_number + 1) % _SEQUENCE_MODULUS
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


__all__ = [
    "MftAllocationPlan",
    "NtfsMftAllocationError",
    "plan_mft_record_reuse",
]
