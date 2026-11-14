"""Kernel-shaped tests for isolated JBD2 csum v2/v3 primitives."""

from __future__ import annotations

import struct

import pytest

from mountfile.ext4_jbd2_csum import (
    DescriptorLayout,
    JBD2ChecksumError,
    commit_checksum,
    descriptor_checksum,
    tag_checksum,
    tag_checksum_matches,
    tid_after_or_equal,
    uuid_seed,
    verify_commit_checksum,
    verify_descriptor_checksum,
)

UUID = bytes.fromhex("00112233445566778899aabbccddeeff")
SEED = uuid_seed(UUID)


def test_uuid_seed_is_stable_and_requires_raw_uuid() -> None:
    assert uuid_seed(UUID) == SEED
    with pytest.raises(JBD2ChecksumError):
        uuid_seed(b"short")


def test_descriptor_checksum_zeroes_only_final_tail() -> None:
    block = bytearray(4096)
    block[:12] = struct.pack(">III", 0xC03B3998, 1, 7)
    expected = descriptor_checksum(bytes(block), SEED)
    struct.pack_into(">I", block, -4, expected)
    assert verify_descriptor_checksum(bytes(block), SEED)
    block[100] ^= 1
    assert not verify_descriptor_checksum(bytes(block), SEED)


def test_data_tag_hashes_sequence_then_complete_block_and_supports_v2_width() -> None:
    data = bytes(range(256)) * 16
    full = tag_checksum(0x12345678, data, SEED)
    assert tag_checksum_matches(0x12345678, data, full, SEED)
    assert tag_checksum_matches(0x12345678, data, full & 0xFFFF, SEED, width=2)
    assert not tag_checksum_matches(0x12345679, data, full, SEED)


def test_deleted_tag_is_explicitly_accepted_without_fabricating_data() -> None:
    assert tag_checksum_matches(1, b"", 0, SEED, deleted=True)


def test_commit_checksum_covers_full_block_and_zeroes_header_fields() -> None:
    block = bytearray(4096)
    struct.pack_into(">III", block, 0, 0xC03B3998, 2, 9)
    block[12:14] = b"\x02\x04"
    checksum = commit_checksum(bytes(block), SEED)
    struct.pack_into(">I", block, 16, checksum)
    assert verify_commit_checksum(bytes(block), SEED)
    block[-1] ^= 1
    assert not verify_commit_checksum(bytes(block), SEED)


def test_sequence_wrap_uses_jbd2_signed_difference_rule() -> None:
    assert tid_after_or_equal(0, 0xFFFFFFFF)
    assert tid_after_or_equal(1, 0xFFFFFFFF)
    assert not tid_after_or_equal(0x80000000, 0)


def test_layout_refuses_ambiguous_geometry() -> None:
    with pytest.raises(JBD2ChecksumError):
        DescriptorLayout(4096, 12)
    with pytest.raises(JBD2ChecksumError):
        DescriptorLayout(20, 16, has_uuid=True)
    assert DescriptorLayout(4096, 16).first_tag_offset() == 28
