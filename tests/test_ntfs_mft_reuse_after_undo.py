# SPDX-License-Identifier: MIT
"""Issue #510, C5: reuse of a freed MFT record slot with ``$MFTMirr`` in lockstep.

The bounded executor publishes an MFT allocation as three coordinated writes:
the ``$MFT`` record, the identical ``$MFTMirr`` record and the allocation
bitmap bit.  This test drives the runtime planner
(``plan_mft_record_reuse``) and the runtime executor
(``publish_ntfs_replay``) through a full

    free(seq 7) -> allocate(seq 8, in use) -> undo(restore) -> re-allocate

cycle and proves that

* the second transaction reuses the same slot with the successor sequence
  demanded by the new log reference;
* ``$MFT`` and ``$MFTMirr`` stay byte-identical after every pass;
* the allocation bitmap bit is only cleared by the undo pass, never as a
  side effect of the reuse itself;
* a sequence that would wrap past ``0xFFFF`` is refused instead of wrapping.

The undo pass of ``native_ntfs_recover`` restores previous record bytes.
The bounded executor deliberately refuses to mix allocation replay with
other replay operations, so the restore step is modeled by writing the
recorded pre-allocation bytes back to the staged stream -- exactly what the
undo plan does for resident record updates.
"""

from __future__ import annotations

import struct

import pytest

from mountfile.ntfs_logfile import NtfsLogFileAnalysis, ReplayStep, ReplayTarget
from mountfile.ntfs_mft_allocation import (
    MftAllocationCandidate,
    NtfsMftAllocationError,
    plan_mft_record_reuse,
)
from mountfile.ntfs_replay_executor import publish_ntfs_replay

RECORD_SIZE = 512
MIRROR_OFFSET = 1000  # free record (512) + 488 padding
BITMAP_OFFSET = 1512  # mirror record (512) + padding start
BITMAP_BYTES = bytes(16)
SLOT = 42


def _raw_record(sequence: int, *, in_use: bool = False) -> bytes:
    flags = 0x0001 if in_use else 0
    record = bytearray(RECORD_SIZE)
    record[0:4] = b"FILE"
    struct.pack_into("<HH", record, 0x04, 0x2A, 2)
    struct.pack_into("<H", record, 0x10, sequence)
    struct.pack_into("<H", record, 0x14, 0x30)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<II", record, 0x18, 0x38, RECORD_SIZE)
    struct.pack_into("<H", record, 0x2A, sequence)
    struct.pack_into("<H", record, 0x2C, 0x5A5A)
    struct.pack_into("<H", record, RECORD_SIZE - 2, sequence)
    return bytes(record)


def _initialized(sequence: int) -> bytes:
    record = bytearray(_raw_record(sequence, in_use=True))
    struct.pack_into("<Q", record, 0x08, 1)
    return bytes(record)


def _allocation_step(payload: bytes) -> ReplayStep:
    return ReplayStep(
        lsn=1,
        transaction_id=1,
        kind="redo",
        operation=0x02,  # InitializeFileRecordSegment
        length=len(payload),
        payload=payload,
        target_attr=128,
        target_vcn=0,
        record_off=0,
        attr_off=0,
        cluster_off=0,
    )


def _analysis(step: ReplayStep) -> NtfsLogFileAnalysis:
    return NtfsLogFileAnalysis(
        restart=None,  # type: ignore[arg-type] - synthetic bounded fixture
        page_size=4096,
        first_page=0,
        log_size=4096,
        scanned_bytes=4096,
        current_lsn=2,
        checkpoint_lsn=1,
        oldest_lsn=1,
        redo_plan=(step,),
        undo_plan=(),
        truncated=False,
        executable=True,  # bounded synthetic fixture: resolver+candidate are supplied
    )


def _resolver(step: ReplayStep) -> ReplayTarget:
    return ReplayTarget(step.target_attr, step.target_vcn, 0, step.length)


def _candidate(free: bytes, mirror: bytes, reference: int) -> MftAllocationCandidate:
    return MftAllocationCandidate(
        free,
        mirror,
        reference,
        SLOT,
        0,  # record_offset
        MIRROR_OFFSET,
        BITMAP_OFFSET,
        BITMAP_BYTES,
        42,  # bitmap_bit
    )


def _read(stream_bytes: bytes, offset: int, length: int) -> bytes:
    return stream_bytes[offset : offset + length]


def _publish(image, payload: bytes, free: bytes, mirror: bytes) -> None:
    candidate = _candidate(free, mirror, (8 << 48) | SLOT)
    publish_ntfs_replay(
        str(image),
        _analysis(_allocation_step(payload)),
        _resolver,
        verify=lambda _staged: True,  # synthetic fixture; not independent checker evidence
        mft_candidate_provider=lambda step: candidate,
    )


def test_freed_slot_is_reused_with_successor_sequence_and_locked_mirror(
    tmp_path,
) -> None:
    free = _raw_record(7)
    mirror = _raw_record(7)
    initialized = _initialized(8)
    image = tmp_path / "image.bin"
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    pristine = image.read_bytes()

    # Pass 1: the freed slot is allocated with the successor sequence.
    _publish(image, initialized, free, mirror)
    after_first = image.read_bytes()
    assert _read(after_first, 0, RECORD_SIZE) == initialized
    assert (
        _read(after_first, MIRROR_OFFSET, RECORD_SIZE) == initialized
    ), "$MFTMirr must follow $MFT byte-for-byte on allocation"
    bitmap = bytearray(_read(after_first, BITMAP_OFFSET, len(BITMAP_BYTES)))
    assert bitmap[42 // 8] & (1 << (42 % 8)), "allocation bit must be set"

    # Undo pass: the previous bytes are restored on the staged stream.
    image.write_bytes(pristine)
    assert image.read_bytes() == pristine

    # Pass 2: a later transaction reuses the same freed slot.  The runtime
    # planner must plan exactly the successor sequence the new reference
    # carries; the executor publishes record+mirror+bitmap in lockstep.
    plan = plan_mft_record_reuse(
        free, mirror, (8 << 48) | SLOT, candidate_record_number=SLOT
    )
    assert (plan.record_number, plan.previous_sequence, plan.next_sequence) == (
        SLOT,
        7,
        8,
    )
    _publish(image, initialized, free, mirror)
    after_second = image.read_bytes()
    assert _read(after_second, 0, RECORD_SIZE) == initialized
    assert _read(after_second, MIRROR_OFFSET, RECORD_SIZE) == initialized
    bitmap = bytearray(_read(after_second, BITMAP_OFFSET, len(BITMAP_BYTES)))
    assert bitmap[42 // 8] & (1 << (42 % 8))


def test_reuse_of_sequence_0xffff_slot_is_refused_not_wrapped() -> None:
    free = _raw_record(0xFFFF)
    mirror = _raw_record(0xFFFF)
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            free, mirror, (1 << 48) | SLOT, candidate_record_number=SLOT
        )
    assert excinfo.value.reason == "sequence_exhausted"


def test_reuse_refuses_a_slot_whose_mirror_drifted() -> None:
    free = _raw_record(7)
    drifted = _raw_record(8)
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            free, drifted, (8 << 48) | SLOT, candidate_record_number=SLOT
        )
    assert excinfo.value.reason == "mft_mirr_mismatch"
