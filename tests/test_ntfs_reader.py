# SPDX-License-Identifier: MIT
"""Synthetic tests for bounded NTFS metadata validation."""

import struct

import pytest

from mountfile import vfs
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsAttributeSummary,
    NtfsMetadataError,
    NtfsRun,
    _parse_runlist,
    _parse_ntfs_index_block,
    parse_ntfs_attribute_stream,
    parse_ntfs_file_name,
    parse_ntfs_index_block,
    parse_ntfs_index_root,
    parse_ntfs_logfile_restart_area,
    parse_ntfs_standard_information,
    parse_ntfs_volume_state,
    probe_ntfs_metadata,
    validate_ntfs_logfile_restart_pages,
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


def test_rejects_attribute_name_that_overlaps_its_header():
    raw = bytearray(_record(attr_type=0x30))
    struct.pack_into("<I", raw, 0x18, 92)
    struct.pack_into("<I", raw, 0x3C, 32)
    raw[0x41] = 1
    struct.pack_into("<H", raw, 0x42, 16)
    struct.pack_into("<I", raw, 0x3C + 32, 0xFFFFFFFF)
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(bytes(raw))
    with pytest.raises(NtfsMetadataError, match="name offset"):
        parse_ntfs_attribute_stream(record)


def test_validates_nested_indx_child_pointer_but_public_parser_remains_fail_closed():
    block = bytearray(4096)
    block[:4] = b"INDX"
    struct.pack_into("<HH", block, 4, 0x28, 9)
    struct.pack_into("<H", block, 0x28, 0xA55A)
    for index in range(8):
        struct.pack_into("<H", block, 0x2A + index * 2, 0x1000 + index)
        struct.pack_into("<H", block, (index + 1) * 512 - 2, 0xA55A)
    struct.pack_into("<IIII", block, 24, 24, 152, 152, 0)
    key = bytearray(74)
    struct.pack_into("<Q", key, 0, (3 << 48) | 5)
    key[0x40] = 4
    key[0x41] = 1
    key[0x42:0x4A] = "file".encode("utf-16le")
    struct.pack_into("<QHHI", block, 48, (3 << 48) | 9, 104, 74, 1)
    block[64:138] = key
    struct.pack_into("<Q", block, 48 + 104 - 8, 7)
    struct.pack_into("<QHHI", block, 152, 0, 24, 0, 3)
    struct.pack_into("<Q", block, 152 + 24 - 8, 8)

    entries, children = _parse_ntfs_index_block(
        bytes(block),
        parent_reference=(3 << 48) | 5,
        expected_vcn=0,
        allow_children=True,
    )
    assert entries[0].name == "file"
    assert children == (7, 8)
    with pytest.raises(NtfsMetadataError, match="INDEX_ALLOCATION"):
        from mountfile.ntfs_reader import parse_ntfs_index_block

        parse_ntfs_index_block(bytes(block), parent_reference=(3 << 48) | 5)


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


def test_native_ntfs_reopens_stream_before_reading_validated_runs(tmp_path):
    from types import SimpleNamespace

    image = tmp_path / "ntfs-runs.img"
    image.write_bytes(bytes(range(256)) * 16)
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.image_path = str(image)
    reader.offset = 0
    reader._image_size = image.stat().st_size
    reader._end = reader._image_size
    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_size=256))
    reader._stream = image.open("rb")
    reader._stream.close()

    assert reader._read_runs((NtfsRun(1, 2, 2),), 256, 512) == bytes(range(256)) * 2
    reader.close()


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


def test_validates_indx_vcn_against_requested_allocation_block():
    block = bytearray(4096)
    block[:4] = b"INDX"
    struct.pack_into("<HH", block, 4, 0x28, 9)
    struct.pack_into("<q", block, 16, 3)
    struct.pack_into("<H", block, 0x28, 0xA55A)
    for index in range(8):
        struct.pack_into("<H", block, 0x2A + index * 2, 0x1000 + index)
        struct.pack_into("<H", block, (index + 1) * 512 - 2, 0xA55A)
    struct.pack_into("<IIII", block, 24, 24, 40, 40, 0)
    struct.pack_into("<QHHI", block, 48, 0, 16, 0, 2)
    assert parse_ntfs_index_block(bytes(block), expected_vcn=3) == ()
    with pytest.raises(NtfsMetadataError, match="VCN"):
        parse_ntfs_index_block(bytes(block), expected_vcn=4)


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


def test_parses_resident_filename_and_standard_information_bounds():
    value = bytearray(76)
    struct.pack_into("<Q", value, 0, (3 << 48) | 5)
    struct.pack_into("<I", value, 56, 0x20)
    value[64] = 5
    value[65] = 3
    value[66:76] = "hello".encode("utf-16le")
    parsed = parse_ntfs_file_name(bytes(value))
    assert parsed.name == "hello"
    assert parsed.file_attributes == 0x20

    standard = bytearray(48)
    struct.pack_into("<I", standard, 32, 0x20)
    struct.pack_into("<II", standard, 40, 2, 1)
    info = parse_ntfs_standard_information(bytes(standard))
    assert (info.file_attributes, info.version, info.class_id) == (0x20, 2, 1)
    with pytest.raises(NtfsMetadataError, match="FILE_NAME"):
        parse_ntfs_file_name(bytes(value[:65]))
    with pytest.raises(NtfsMetadataError, match="STANDARD_INFORMATION"):
        parse_ntfs_standard_information(bytes(47))


def test_rejects_unknown_ntfs_volume_flags():
    value = bytearray(12)
    value[8:10] = b"\x03\x01"
    struct.pack_into("<H", value, 10, 0x0080)
    attr = NtfsAttributeSummary(0x70, 24, False, "", resident_value=bytes(value))
    with pytest.raises(NtfsMetadataError, match="unknown volume flags"):
        parse_ntfs_volume_state((attr,))


def _restart_page(
    *,
    flags=0x0002,
    sequence=0xA55A,
    revision=7,
    log_page_size=4096,
    minor_version=1,
    major_version=2,
):
    page = bytearray(4096)
    page[:4] = b"RSTR"
    struct.pack_into("<HH", page, 4, 0x1E, 9)
    struct.pack_into("<II", page, 0x10, 4096, log_page_size)
    struct.pack_into("<hh", page, 0x1A, minor_version, major_version)
    struct.pack_into("<H", page, 0x18, 0x30)
    struct.pack_into("<QHHHHI", page, 0x30, 42, 0, 0xFFFF, 0xFFFF, flags, 0)
    struct.pack_into("<HH", page, 0x30 + 0x14, 0x30, 0x30)
    struct.pack_into("<I", page, 0x30 + 0x28, revision)
    struct.pack_into("<H", page, 0x1E, sequence)
    for index in range(8):
        struct.pack_into("<H", page, 0x20 + index * 2, 0x1100 + index)
        struct.pack_into("<H", page, (index + 1) * 512 - 2, sequence)
    return bytes(page)


def test_accepts_consistent_clean_ntfs_logfile_restart_pair():
    first = _restart_page(revision=3)
    second = _restart_page(revision=8)
    parsed = validate_ntfs_logfile_restart_pages(first + second)
    assert parsed.clean_dismount is True
    assert parsed.revision == 8
    assert parsed.version == (2, 1)
    assert parse_ntfs_logfile_restart_area(first).log_page_size == 4096


def test_rejects_truncated_ntfs_logfile_restart_stream():
    with pytest.raises(NtfsMetadataError, match="truncated"):
        validate_ntfs_logfile_restart_pages(bytes(4096))


def test_rejects_valid_first_and_malformed_second_ntfs_logfile_restart_page():
    second = bytearray(_restart_page())
    second[:4] = b"XXXX"
    with pytest.raises(NtfsMetadataError, match="signature"):
        validate_ntfs_logfile_restart_pages(_restart_page() + bytes(second))


@pytest.mark.parametrize(
    "second, message",
    [
        (_restart_page(log_page_size=8192), "geometry"),
        (_restart_page(minor_version=2), "version"),
        (_restart_page(flags=0), "clean state"),
    ],
)
def test_rejects_inconsistent_ntfs_logfile_restart_pair(second, message):
    with pytest.raises(NtfsMetadataError, match=message):
        validate_ntfs_logfile_restart_pages(_restart_page() + second)


def test_rejects_logfile_with_less_than_two_logical_restart_pages(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    image = tmp_path / "short-logfile.img"
    image.write_bytes(b"")
    mft_data = NtfsAttributeSummary(
        0x80,
        64,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=0,
        run_count=1,
        runs=(NtfsRun(0, 0, 1),),
        data_size=4096,
    )
    logfile_data = NtfsAttributeSummary(
        0x80,
        64,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        run_count=1,
        runs=(NtfsRun(0, 0, 2),),
        data_size=2 * 4096 - 1,
    )
    volume_record = object()
    logfile_record = object()
    profile = SimpleNamespace(
        boot=SimpleNamespace(
            cluster_size=4096,
            cluster_count=2,
            filesystem_size=4096,
        ),
        attributes=(mft_data,),
    )

    monkeypatch.setattr(
        "mountfile.ntfs_reader.probe_ntfs_metadata", lambda *args, **kwargs: profile
    )
    monkeypatch.setattr(
        NativeNtfsMetadataImage,
        "read_mft_record",
        lambda _self, record_number, **_kwargs: (
            volume_record if record_number == 3 else logfile_record
        ),
    )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda record, **_kwargs: (() if record is volume_record else (logfile_data,)),
    )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_volume_state",
        lambda _attrs: SimpleNamespace(clean=True),
    )
    monkeypatch.setattr(
        NativeNtfsMetadataImage,
        "_read_runs",
        lambda *_args: pytest.fail("short logical $LogFile must not be read"),
    )

    with pytest.raises(NtfsMetadataError, match="shorter than two restart pages"):
        NativeNtfsMetadataImage(image, volume_size=4096)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda page: page.__setitem__(slice(0, 4), b"XXXX"), "signature"),
        (lambda page: page.__setitem__(511, page[511] ^ 1), "USA mismatch"),
        (
            lambda page: struct.pack_into("<H", page, 0x30 + 0x14, 0x20),
            "client array bounds",
        ),
        (lambda page: struct.pack_into("<H", page, 0x30 + 0x0E, 0x20), "unknown state"),
    ],
)
def test_rejects_malformed_ntfs_logfile_restart_page(mutate, message):
    page = bytearray(_restart_page())
    mutate(page)
    with pytest.raises(NtfsMetadataError, match=message):
        parse_ntfs_logfile_restart_area(bytes(page))
