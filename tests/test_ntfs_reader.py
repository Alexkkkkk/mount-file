# SPDX-License-Identifier: MIT
"""Synthetic tests for bounded NTFS metadata validation."""

import struct

import pytest

from mountfile import vfs
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsAttributeSummary,
    NtfsMetadataError,
    _parse_runlist,
    parse_ntfs_index_block,
    parse_ntfs_index_root,
    parse_ntfs_volume_state,
    probe_ntfs_metadata,
)

BPS = 512
SPC = 8
TOTAL = 2048
VOLUME_SIZE = BPS * TOTAL
CLUSTER = BPS * SPC
MFT = 4 * CLUSTER
MIRROR = 8 * CLUSTER
RECORD = 1024


def _boot():
    raw = bytearray(512)
    raw[3:11] = b"NTFS    "
    struct.pack_into("<H", raw, 0x0B, BPS)
    raw[0x0D] = SPC
    struct.pack_into("<Q", raw, 0x28, TOTAL)
    struct.pack_into("<Q", raw, 0x30, 4)
    struct.pack_into("<Q", raw, 0x38, 8)
    struct.pack_into("<b", raw, 0x40, -10)
    struct.pack_into("<b", raw, 0x44, 1)
    raw[510:512] = b"\x55\xaa"
    return bytes(raw)


def _record(*, attr_type=0xFFFFFFFF, nonresident=False):
    raw = bytearray(RECORD)
    raw[:4] = b"FILE"
    usa = 0x30
    struct.pack_into("<HH", raw, 4, usa, 3)
    struct.pack_into("<H", raw, 0x10, 3)
    struct.pack_into("<H", raw, 0x14, 0x38)
    struct.pack_into("<H", raw, 0x16, 1)
    struct.pack_into("<I", raw, 0x18, 0x3C)
    struct.pack_into("<I", raw, 0x1C, RECORD)
    struct.pack_into("<H", raw, usa, 0xA55A)
    for index in range(2):
        struct.pack_into("<H", raw, usa + 2 + index * 2, 0x1100 + index)
        struct.pack_into("<H", raw, (index + 1) * 512 - 2, 0xA55A)
    struct.pack_into("<I", raw, 0x38, attr_type)
    if attr_type != 0xFFFFFFFF:
        struct.pack_into("<I", raw, 0x3C, 64 if nonresident else 24)
        raw[0x40] = 1 if nonresident else 0
        if nonresident:
            struct.pack_into("<qQ", raw, 0x48, 0, 0)
            struct.pack_into("<H", raw, 0x58, 64)
            raw[0x78] = 0
        else:
            struct.pack_into("<I", raw, 0x48, 0)
            struct.pack_into("<H", raw, 0x4C, 24)
        struct.pack_into("<I", raw, 0x3C + (64 if nonresident else 24), 0xFFFFFFFF)
    return bytes(raw)


def _image(tmp_path, record=None):
    image = bytearray(VOLUME_SIZE)
    image[:512] = _boot()
    image[-512:] = _boot()
    record = record or _record()
    image[MFT : MFT + RECORD] = record
    image[MIRROR : MIRROR + RECORD] = record
    path = tmp_path / "ntfs.img"
    path.write_bytes(image)
    return path


def test_validates_backup_mft_mirror_and_attribute_end_marker(tmp_path):
    profile = probe_ntfs_metadata(_image(tmp_path), volume_size=VOLUME_SIZE)
    assert profile.mirror_matches_mft is True
    assert profile.attributes == ()
    assert profile.mft_record.sequence_number == 3


def test_vfs_exposes_bounded_ntfs_metadata_probe_without_pytsk3(tmp_path, monkeypatch):
    monkeypatch.setattr(vfs, "pytsk3", None)
    profile = vfs.probe_ntfs_metadata(_image(tmp_path), volume_size=VOLUME_SIZE)
    assert profile.boot.mft_offset == MFT


def test_vfs_does_not_silently_fallback_for_ntfs(tmp_path, monkeypatch):
    monkeypatch.setattr(vfs, "pytsk3", None)
    with pytest.raises(vfs.TskError, match="native NTFS reader declined"):
        vfs.open_image_provider(_image(tmp_path), volume_size=VOLUME_SIZE)


def test_ntfs_without_explicit_extent_does_not_fall_back_to_pytsk3(
    tmp_path, monkeypatch
):
    image_path = _image(tmp_path)
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *args, **kwargs: pytest.fail("unbounded NTFS reached pytsk3"),
    )
    with pytest.raises(vfs.RecognizedFilesystemError, match="explicit bounded extent"):
        vfs.open_image_provider(image_path)


def test_rejects_backup_geometry_mismatch(tmp_path):
    image = bytearray(_image(tmp_path).read_bytes())
    struct.pack_into("<Q", image, VOLUME_SIZE - 512 + 0x28, TOTAL - 1)
    path = tmp_path / "bad-backup.img"
    path.write_bytes(image)
    with pytest.raises(NtfsMetadataError, match="backup boot profile"):
        probe_ntfs_metadata(path, volume_size=VOLUME_SIZE)


def test_rejects_mismatched_mft_mirror(tmp_path):
    image = bytearray(_image(tmp_path).read_bytes())
    image[MIRROR + 0x10] ^= 1
    path = tmp_path / "bad-mirror.img"
    path.write_bytes(image)
    with pytest.raises(NtfsMetadataError, match="MFT mirror"):
        probe_ntfs_metadata(path, volume_size=VOLUME_SIZE)


def test_decodes_bounded_signed_ntfs_runs():
    runs = _parse_runlist(bytes([0x11, 0x03, 0x05, 0x11, 0x02, 0xFF, 0x00]), 32)
    assert [(run.vcn, run.lcn, run.length) for run in runs] == [
        (0, 5, 3),
        (3, 4, 2),
    ]


def test_rejects_sparse_ntfs_run():
    with pytest.raises(NtfsMetadataError, match="sparse"):
        _parse_runlist(bytes([0x01, 0x03, 0x00]), 32)


def test_rejects_ntfs_runlist_that_does_not_cover_declared_vcn_range():
    with pytest.raises(NtfsMetadataError, match="does not cover"):
        _parse_runlist(
            bytes([0x11, 0x01, 0x05, 0x00]),
            32,
            lowest_vcn=0,
            highest_vcn=2,
        )


def test_mft_record_loader_validates_record_number_and_sequence():
    from types import SimpleNamespace

    raw = _record()
    fake = SimpleNamespace(
        profile=SimpleNamespace(boot=SimpleNamespace(file_record_size=RECORD)),
        read_file_range=lambda _path, _offset, _length: raw,
    )
    parsed = NativeNtfsMetadataImage.read_mft_record(fake, 42, reference=(3 << 48) | 42)
    assert parsed.sequence_number == 3
    with pytest.raises(NtfsMetadataError, match="record number"):
        NativeNtfsMetadataImage.read_mft_record(fake, 41, reference=(3 << 48) | 42)


def test_parses_bounded_index_root_file_name_entry():
    value = bytearray(144)
    struct.pack_into("<III", value, 16, 16, 128, 128)
    entry = 32
    key = bytearray(74)
    struct.pack_into("<Q", key, 0, (3 << 48) | 9)
    key[0x40] = 4
    key[0x42:0x4A] = "note".encode("utf-16le")
    struct.pack_into("<QHHI", value, entry, (3 << 48) | 9, 96, len(key), 0)
    value[entry + 16 : entry + 16 + len(key)] = key
    struct.pack_into("<QHHI", value, entry + 96, 0, 16, 0, 2)
    entries = parse_ntfs_index_root(bytes(value), parent_reference=(3 << 48) | 9)
    assert entries[0].name == "note"
    assert entries[0].reference == (3 << 48) | 9
    with pytest.raises(NtfsMetadataError, match="parent reference mismatch"):
        parse_ntfs_index_root(bytes(value), parent_reference=(3 << 48) | 10)
    struct.pack_into("<I", value, 28, 1)
    with pytest.raises(NtfsMetadataError, match="INDEX_ALLOCATION"):
        parse_ntfs_index_root(bytes(value))

    struct.pack_into("<I", value, 28, 0)
    struct.pack_into("<I", value, entry + 96 + 12, 1)
    with pytest.raises(NtfsMetadataError, match="INDEX_ALLOCATION"):
        parse_ntfs_index_root(bytes(value))


def test_validates_indx_usa_before_parsing_entries():
    block = bytearray(4096)
    block[:4] = b"INDX"
    struct.pack_into("<HH", block, 4, 0x28, 9)
    struct.pack_into("<H", block, 0x28, 0xA55A)
    for index in range(8):
        struct.pack_into("<H", block, 0x2A + index * 2, 0x1000 + index)
        struct.pack_into("<H", block, (index + 1) * 512 - 2, 0xA55A)
    struct.pack_into("<IIII", block, 24, 24, 136, 136, 0)
    key = bytearray(74)
    struct.pack_into("<Q", key, 0, (3 << 48) | 9)
    key[0x40] = 4
    key[0x42:0x4A] = "file".encode("utf-16le")
    struct.pack_into("<QHHI", block, 48, (3 << 48) | 9, 96, 74, 0)
    block[64:138] = key
    struct.pack_into("<QHHI", block, 144, 0, 16, 0, 2)
    assert parse_ntfs_index_block(bytes(block))[0].name == "file"
    block[1022] ^= 1
    with pytest.raises(NtfsMetadataError, match="USA mismatch"):
        parse_ntfs_index_block(bytes(block))


def test_parses_ntfs_volume_flags_and_rejects_missing_information():
    value = bytearray(12)
    value[8:10] = b"\x03\x01"
    clean = NtfsAttributeSummary(0x70, 24, False, "", resident_value=bytes(value))
    assert parse_ntfs_volume_state((clean,)).clean
    struct.pack_into("<H", value, 10, 0x0021)
    dirty = NtfsAttributeSummary(0x70, 24, False, "", resident_value=bytes(value))
    assert not parse_ntfs_volume_state((dirty,)).clean
    with pytest.raises(NtfsMetadataError, match="ambiguous volume information"):
        parse_ntfs_volume_state(())
