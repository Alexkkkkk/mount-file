# SPDX-License-Identifier: MIT
"""Tests for bounded MFT record reuse planning ($MFTMirr aware)."""

import struct

import pytest

from mountfile.ntfs_mft_allocation import (
    MftAllocationPlan,
    NtfsMftAllocationError,
    plan_mft_record_reuse,
)


def _raw_record(
    sequence: int, in_use: bool = True, usa_mismatch: bool = False
) -> bytes:
    """Build one minimal valid 512-byte FILE record for planner tests."""
    flags = 0x0001 if in_use else 0x0000
    buf = bytearray(512)
    buf[0:4] = b"FILE"
    struct.pack_into("<HH", buf, 0x04, 0x2A, 2)  # USA offset, USA count
    struct.pack_into("<H", buf, 0x10, sequence)
    struct.pack_into("<H", buf, 0x14, 0x30)  # first attribute offset
    struct.pack_into("<H", buf, 0x16, flags)
    struct.pack_into("<I", buf, 0x18, 0x38)  # bytes in use
    struct.pack_into("<I", buf, 0x1C, 512)  # bytes allocated
    struct.pack_into("<H", buf, 0x2A, sequence)  # USA number
    struct.pack_into("<H", buf, 0x2C, 0x5A5A)  # USA replacement
    struct.pack_into("<H", buf, 510, sequence + (1 if usa_mismatch else 0))
    return bytes(buf)


def _reference(record_number: int, sequence: int) -> int:
    return (sequence << 48) | record_number


def test_free_record_with_matching_mirror_and_sequence_is_planned():
    plan = plan_mft_record_reuse(
        _raw_record(7, in_use=False),
        _raw_record(7, in_use=False),
        _reference(42, 8),
    )
    assert isinstance(plan, MftAllocationPlan)
    assert plan.record_number == 42
    assert plan.previous_sequence == 7
    assert plan.next_sequence == 8
    assert plan.mirror_consistent is True


def test_sequence_wrap_at_uint16_is_planned():
    # Zero is a reserved sequence, so wrap goes 0xFFFE -> 0xFFFF.
    plan = plan_mft_record_reuse(
        _raw_record(0xFFFE, in_use=False),
        _raw_record(0xFFFE, in_use=False),
        _reference(1, 0xFFFF),
    )
    assert plan.next_sequence == 0xFFFF


def test_in_use_candidate_is_refused():
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            _raw_record(7, in_use=True),
            _raw_record(7, in_use=True),
            _reference(42, 8),
        )
    assert excinfo.value.reason == "candidate_record_in_use"


def test_sequence_mismatch_is_refused():
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            _raw_record(7, in_use=False),
            _raw_record(7, in_use=False),
            _reference(42, 9),
        )
    assert excinfo.value.reason == "sequence_mismatch"


def test_mirror_content_mismatch_is_refused():
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            _raw_record(7, in_use=False),
            _raw_record(8, in_use=False),
            _reference(42, 8),
        )
    assert excinfo.value.reason == "mft_mirr_mismatch"


def test_invalid_candidate_usa_is_refused():
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            _raw_record(7, in_use=False, usa_mismatch=True),
            _raw_record(7, in_use=False),
            _reference(42, 8),
        )
    assert excinfo.value.reason == "candidate_record_invalid"


def test_zero_sequence_reference_is_refused():
    with pytest.raises(NtfsMftAllocationError) as excinfo:
        plan_mft_record_reuse(
            _raw_record(7, in_use=False),
            _raw_record(7, in_use=False),
            _reference(42, 0),
        )
    assert excinfo.value.reason == "reference_invalid"
