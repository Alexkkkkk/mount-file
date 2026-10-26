# SPDX-License-Identifier: MIT
"""Synthetic tests for NTFS FILE-record header and USA fixups."""

import struct

import pytest

from mountfile import vfs
from mountfile.ntfs_mft import (
    NtfsRecordError,
    parse_ntfs_file_record_header,
    validate_ntfs_file_reference,
)

_SECTOR_SIZE = 512


def _file_record(*, size=1024, usa_offset=0x30, flags=0x0003, sequence=7):
    sector_count = size // _SECTOR_SIZE
    usa_count = sector_count + 1
    usa_end = usa_offset + usa_count * 2
    attribute_offset = max(0x30, (usa_end + 7) & ~7)
    record = bytearray(size)
    record[:4] = b"FILE"
    struct.pack_into("<HH", record, 0x04, usa_offset, usa_count)
    struct.pack_into("<H", record, 0x10, sequence)
    struct.pack_into("<H", record, 0x12, 2)
    struct.pack_into("<H", record, 0x14, attribute_offset)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<I", record, 0x18, attribute_offset + 4)
    struct.pack_into("<I", record, 0x1C, size)
    struct.pack_into("<Q", record, 0x20, 0x0001000000000002)
    struct.pack_into("<H", record, 0x28, 9)

    update_sequence_number = 0xA55A
    struct.pack_into("<H", record, usa_offset, update_sequence_number)
    original_trailers = []
    for index in range(sector_count):
        original = 0x1100 + index
        original_trailers.append(original)
        struct.pack_into("<H", record, usa_offset + 2 * (index + 1), original)
        struct.pack_into(
            "<H", record, (index + 1) * _SECTOR_SIZE - 2, update_sequence_number
        )
    struct.pack_into("<I", record, attribute_offset, 0xFFFFFFFF)
    return bytes(record), tuple(original_trailers), attribute_offset


def test_validates_header_and_restores_all_sector_trailers():
    raw_record, originals, attribute_offset = _file_record()

    parsed = parse_ntfs_file_record_header(raw_record)

    assert parsed.sequence_number == 7
    assert parsed.hard_link_count == 2
    assert parsed.first_attribute_offset == attribute_offset
    assert parsed.flags == 0x0003
    assert parsed.in_use is True
    assert parsed.is_directory is True
    assert parsed.bytes_in_use == attribute_offset + 4
    assert parsed.bytes_allocated == len(raw_record)
    assert parsed.base_file_reference == 0x0001000000000002
    assert parsed.next_attribute_id == 9
    assert parsed.update_sequence_number == 0xA55A
    assert parsed.update_sequence_offset == 0x30
    assert parsed.update_sequence_count == 3
    assert parsed.record_size == 1024
    assert raw_record[(512 - 2) : 512] == b"\x5a\xa5"
    for index, original in enumerate(originals):
        restored = struct.unpack_from(
            "<H", parsed.fixed_record, (index + 1) * _SECTOR_SIZE - 2
        )[0]
        assert restored == original


def test_validates_file_reference_sequence_and_returns_record_number():
    raw_record, _originals, _attribute_offset = _file_record()
    parsed = parse_ntfs_file_record_header(raw_record)
    assert validate_ntfs_file_reference((7 << 48) | 42, parsed) == 42
    with pytest.raises(NtfsRecordError, match="sequence"):
        validate_ntfs_file_reference((8 << 48) | 42, parsed)


def test_accepts_legacy_usa_offset_0x2a():
    raw_record, _originals, attribute_offset = _file_record(usa_offset=0x2A)

    parsed = parse_ntfs_file_record_header(raw_record)

    assert parsed.update_sequence_offset == 0x2A
    assert parsed.first_attribute_offset == attribute_offset


def test_rejects_undocumented_usa_offset():
    raw_record, _originals, _attribute_offset = _file_record()
    malformed = bytearray(raw_record)
    struct.pack_into("<H", malformed, 0x04, 0x2C)

    with pytest.raises(NtfsRecordError, match="not a supported FILE record layout"):
        parse_ntfs_file_record_header(malformed)


def test_validates_all_fixups_in_a_4k_file_record():
    raw_record, originals, _attribute_offset = _file_record(size=4096)

    parsed = parse_ntfs_file_record_header(raw_record)

    assert parsed.update_sequence_count == 9
    for index, original in enumerate(originals):
        assert (
            struct.unpack_from(
                "<H", parsed.fixed_record, (index + 1) * _SECTOR_SIZE - 2
            )[0]
            == original
        )


def test_rejects_trailer_mismatch_without_returning_partial_record():
    raw_record, _originals, _attribute_offset = _file_record()
    corrupt = bytearray(raw_record)
    corrupt[2 * _SECTOR_SIZE - 2] ^= 0x01
    before = bytes(corrupt)

    with pytest.raises(NtfsRecordError, match="USA mismatch at protected sector 1"):
        parse_ntfs_file_record_header(corrupt)

    assert bytes(corrupt) == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.__setitem__(slice(0, 4), b"INDX"),
        lambda record: struct.pack_into("<H", record, 0x04, 0x31),
        lambda record: struct.pack_into("<H", record, 0x06, 2),
        lambda record: struct.pack_into("<H", record, 0x14, 0x30),
        lambda record: struct.pack_into("<I", record, 0x18, 0x30),
        lambda record: struct.pack_into("<I", record, 0x1C, 512),
        lambda record: struct.pack_into("<H", record, 0x16, 0x10),
        lambda record: struct.pack_into("<H", record, 0x10, 0),
    ],
)
def test_rejects_malformed_header_fields(mutate):
    raw_record, _originals, _attribute_offset = _file_record()
    malformed = bytearray(raw_record)
    mutate(malformed)

    with pytest.raises(NtfsRecordError):
        parse_ntfs_file_record_header(malformed)


def test_rejects_usa_that_does_not_fit_in_first_512_bytes():
    raw_record, _originals, _attribute_offset = _file_record(size=131072)

    with pytest.raises(NtfsRecordError, match="exceeds its first 512-byte sector"):
        parse_ntfs_file_record_header(raw_record)


@pytest.mark.parametrize("size", [0, 256, 768, 2 * 1024 * 1024])
def test_rejects_record_length_outside_supported_limits(size):
    with pytest.raises(NtfsRecordError, match="size is outside the supported range"):
        parse_ntfs_file_record_header(bytes(size))


def test_vfs_file_record_parser_works_without_pytsk3(monkeypatch):
    raw_record, _originals, _attribute_offset = _file_record()
    monkeypatch.setattr(vfs, "pytsk3", None)

    parsed = vfs.parse_ntfs_file_record_header(raw_record)

    assert parsed.in_use is True
    assert parsed.record_size == 1024
