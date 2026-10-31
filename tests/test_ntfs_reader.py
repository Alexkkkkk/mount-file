# SPDX-License-Identifier: MIT
"""Synthetic tests for bounded NTFS metadata validation."""

import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mountfile import vfs
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsAttributeListEntry,
    NtfsAttributeSummary,
    NtfsIndexEntry,
    NtfsMetadataError,
    NtfsRun,
    _parse_ntfs_index_block,
    _parse_ntfs_upcase_table,
    _parse_runlist,
    parse_ntfs_attribute_list,
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


def _record_with_attributes(attributes, *, sequence=3, base_file_reference=0):
    raw = bytearray(_record())
    attribute_offset = 0x38
    attribute_bytes = b"".join(attributes) + struct.pack("<I", 0xFFFFFFFF)
    raw[attribute_offset : attribute_offset + len(attribute_bytes)] = attribute_bytes
    struct.pack_into("<I", raw, 0x18, attribute_offset + len(attribute_bytes))
    struct.pack_into("<H", raw, 0x10, sequence)
    struct.pack_into("<Q", raw, 0x20, base_file_reference)
    return bytes(raw)


def _resident_attribute(type_code, attribute_id, value, *, name=""):
    encoded_name = name.encode("utf-16le")
    value_offset = (24 + len(encoded_name) + 7) & ~7
    length = (value_offset + len(value) + 7) & ~7
    attribute = bytearray(length)
    struct.pack_into(
        "<IIBBHH", attribute, 0, type_code, length, 0, len(name), 24 if name else 0, 0
    )
    struct.pack_into("<H", attribute, 14, attribute_id)
    struct.pack_into("<IH", attribute, 16, len(value), value_offset)
    attribute[24 : 24 + len(encoded_name)] = encoded_name
    attribute[value_offset : value_offset + len(value)] = value
    return bytes(attribute)


def _nonresident_attribute(
    type_code,
    attribute_id,
    lowest_vcn,
    highest_vcn,
    lcn,
    data_size,
    *,
    name="",
    allocated_size=None,
    initialized_size=None,
):
    encoded_name = name.encode("utf-16le")
    runlist_offset = (64 + len(encoded_name) + 7) & ~7
    runlist = bytes((0x11, 0x01, lcn, 0x00))
    length = (runlist_offset + len(runlist) + 7) & ~7
    attribute = bytearray(length)
    struct.pack_into(
        "<IIBBHHH",
        attribute,
        0,
        type_code,
        length,
        1,
        len(name),
        64 if name else 0,
        0,
        attribute_id,
    )
    struct.pack_into("<qQ", attribute, 16, lowest_vcn, highest_vcn)
    struct.pack_into("<H", attribute, 32, runlist_offset)
    run_capacity = (highest_vcn - lowest_vcn + 1) * CLUSTER
    if allocated_size is None:
        allocated_size = max(data_size, run_capacity)
    if initialized_size is None:
        initialized_size = data_size
    struct.pack_into("<Q", attribute, 40, allocated_size)
    struct.pack_into("<Q", attribute, 48, data_size)
    struct.pack_into("<Q", attribute, 56, initialized_size)
    attribute[64 : 64 + len(encoded_name)] = encoded_name
    attribute[runlist_offset : runlist_offset + len(runlist)] = runlist
    return bytes(attribute)


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


def _attribute_list_entry(
    type_code, file_reference, *, lowest_vcn=0, attribute_id=1, name=""
):
    name_bytes = name.encode("utf-16le")
    entry_length = (26 + len(name_bytes) + 7) & ~7
    entry = bytearray(entry_length)
    struct.pack_into(
        "<IHBBqQH",
        entry,
        0,
        type_code,
        entry_length,
        len(name),
        26,
        lowest_vcn,
        file_reference,
        attribute_id,
    )
    entry[26 : 26 + len(name_bytes)] = name_bytes
    return bytes(entry)


def _filename_index_entry(name, record_number):
    encoded = name.encode("utf-16le")
    key = bytearray(66 + len(encoded))
    struct.pack_into("<Q", key, 0, (3 << 48) | 5)
    key[64] = len(encoded) // 2
    key[65] = 3
    key[66:] = encoded
    entry_length = (16 + len(key) + 7) & ~7
    entry = bytearray(entry_length)
    struct.pack_into(
        "<QHHI", entry, 0, (3 << 48) | record_number, entry_length, len(key), 0
    )
    entry[16 : 16 + len(key)] = key
    return bytes(entry)


def _index_root_value(entries):
    end_entry = struct.pack("<QHHI", 0, 16, 0, 2)
    entry_bytes = b"".join(entries)
    total_size = 16 + len(entry_bytes) + len(end_entry)
    value = bytearray(16 + total_size)
    struct.pack_into("<III", value, 0, 0x30, 1, 512)
    value[12] = 1
    struct.pack_into("<III", value, 16, 16, total_size, total_size)
    struct.pack_into("<I", value, 28, 0)
    value[32 : 32 + len(entry_bytes)] = entry_bytes
    value[32 + len(entry_bytes) :] = end_entry
    return bytes(value)


def _unicode_upcase_table():
    values = list(range(1 << 16))
    for codepoint in range(ord("a"), ord("z") + 1):
        values[codepoint] = codepoint - 32
    values[ord("é")] = ord("É")
    return tuple(values)


def _file_name_value(name, parent_reference, file_attributes=0):
    encoded = name.encode("utf-16le")
    value = bytearray(66 + len(encoded))
    struct.pack_into("<Q", value, 0, parent_reference)
    struct.pack_into("<I", value, 56, file_attributes)
    value[64] = len(encoded) // 2
    value[65] = 3
    value[66:] = encoded
    return bytes(value)


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


def test_ntfs_signature_outside_selected_extent_is_not_probed(tmp_path):
    image_path = tmp_path / "neighbor-signature.img"
    image = bytearray(512)
    image[3:11] = b"NTFS    "
    image_path.write_bytes(image)

    with pytest.raises(vfs.LegacyFallbackAvailableError):
        vfs.open_image_provider(
            image_path, offset=0, volume_size=3, allow_legacy_fallback=False
        )


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


def test_parses_bounded_attribute_list_entries_and_extension_references():
    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    raw = b"".join(
        (
            _attribute_list_entry(0x10, base_reference),
            _attribute_list_entry(0x80, base_reference, name="notes"),
            _attribute_list_entry(
                0x80,
                extension_reference,
                lowest_vcn=8,
                attribute_id=0,
                name="notes",
            ),
        )
    )
    entries = parse_ntfs_attribute_list(raw)

    assert entries == (
        NtfsAttributeListEntry(0x10, "", 0, base_reference, 1),
        NtfsAttributeListEntry(0x80, "notes", 0, base_reference, 1),
        NtfsAttributeListEntry(0x80, "notes", 8, extension_reference, 0),
    )


def test_attribute_list_uses_upcase_table_for_unicode_duplicate_keys():
    table = list(range(1 << 16))
    for codepoint in range(ord("a"), ord("z") + 1):
        table[codepoint] = codepoint - 32
    table[0x00E9] = 0x00C9
    table = tuple(table)
    raw = _attribute_list_entry(0x80, (1 << 48) | 1, name="É") + _attribute_list_entry(
        0x80, (1 << 48) | 2, name="é"
    )

    with pytest.raises(NtfsMetadataError, match="duplicate entry"):
        parse_ntfs_attribute_list(raw, upcase_table=table)


@pytest.mark.parametrize(
    "raw, message",
    [
        (bytes(24), "header is truncated"),
        (
            struct.pack("<IHBBqQH", 0x80, 30, 0, 26, 0, (1 << 48) | 1, 0) + bytes(6),
            "entry length is invalid",
        ),
        (_attribute_list_entry(0x80, 42), "zero sequence"),
        (_attribute_list_entry(0x80, (1 << 48) | 1, lowest_vcn=-1), "negative"),
        (
            _attribute_list_entry(0x80, (1 << 48) | 1, name="\x00"),
            "contains NUL",
        ),
        (
            _attribute_list_entry(0x80, (1 << 48) | 1)
            + _attribute_list_entry(0x10, (1 << 48) | 2),
            "type order",
        ),
        (
            _attribute_list_entry(0x80, (1 << 48) | 1, name="zeta")
            + _attribute_list_entry(0x80, (1 << 48) | 2, name="alpha"),
            "name/VCN order",
        ),
        (
            _attribute_list_entry(0x80, (1 << 48) | 1, lowest_vcn=8, name="notes")
            + _attribute_list_entry(0x80, (1 << 48) | 2, lowest_vcn=0, name="notes"),
            "VCN order",
        ),
        (
            _attribute_list_entry(0x80, (1 << 48) | 1, name="notes")
            + _attribute_list_entry(0x80, (1 << 48) | 2, name="notes"),
            "duplicate entry",
        ),
        (_attribute_list_entry(0x20, (1 << 48) | 1), "must not reference itself"),
        (bytearray((1 << 20) + 8), "invalid bounded length"),
    ],
)
def test_rejects_malformed_attribute_list_entries(raw, message):
    with pytest.raises(NtfsMetadataError, match=message):
        parse_ntfs_attribute_list(raw)


def test_rejects_invalid_upcase_table_for_attribute_list_collation():
    raw = _attribute_list_entry(0x10, (1 << 48) | 1)
    with pytest.raises(NtfsMetadataError, match=r"invalid \$UpCase table"):
        parse_ntfs_attribute_list(raw, upcase_table=(0,))


def test_attribute_stream_validates_resident_list_then_refuses_extension_resolution():
    reference = (3 << 48) | 5
    value = _attribute_list_entry(0x10, reference)
    raw = bytearray(_record(attr_type=0x20))
    attribute_offset = 0x38
    attribute_length = (24 + len(value) + 7) & ~7
    struct.pack_into("<I", raw, 0x18, attribute_offset + attribute_length + 4)
    struct.pack_into("<I", raw, attribute_offset + 4, attribute_length)
    struct.pack_into("<I", raw, attribute_offset + 16, len(value))
    struct.pack_into("<H", raw, attribute_offset + 20, 24)
    raw[attribute_offset + 24 : attribute_offset + 24 + len(value)] = value
    struct.pack_into("<I", raw, attribute_offset + attribute_length, 0xFFFFFFFF)

    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(bytes(raw))
    with pytest.raises(NtfsMetadataError, match="extension-record resolution"):
        parse_ntfs_attribute_stream(record)


def test_attribute_stream_only_exposes_attribute_list_when_explicitly_enabled():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    value = _attribute_list_entry(0x10, base_reference, attribute_id=1)
    record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, value),
            ),
            sequence=7,
        )
    )

    with pytest.raises(NtfsMetadataError, match="extension-record resolution"):
        parse_ntfs_attribute_stream(record)
    with pytest.raises(NtfsMetadataError, match="must be a boolean"):
        parse_ntfs_attribute_stream(record, allow_attribute_list=1)
    attributes = parse_ntfs_attribute_stream(record, allow_attribute_list=True)
    assert [
        (attribute.type_code, attribute.attribute_id) for attribute in attributes
    ] == [
        (0x10, 1),
        (0x20, 2),
    ]


def _attribute_list_reader(extension_record):
    from types import SimpleNamespace

    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(
        boot=SimpleNamespace(cluster_count=TOTAL, cluster_size=CLUSTER)
    )
    reader._upcase_table = None
    base_reference = (7 << 48) | 42

    def read_mft_record(record_number, *, reference=None):
        assert record_number == 43
        assert reference == (8 << 48) | 43
        return extension_record

    reader.read_mft_record = read_mft_record
    return reader, base_reference


def test_attribute_list_resolves_extension_record_and_validates_attribute_id():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x10, base_reference, attribute_id=1),
            _attribute_list_entry(0x30, extension_reference, attribute_id=4),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_resident_attribute(0x30, 4, b"child-name"),),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    attributes = reader._attributes_for_record(42, base_record)

    assert [
        (attribute.type_code, attribute.attribute_id) for attribute in attributes
    ] == [
        (0x20, 2),
        (0x10, 1),
        (0x30, 4),
    ]
    assert attributes[-1].resident_value == b"child-name"


@pytest.mark.parametrize(
    ("extension_type", "extension_attribute"),
    [
        (0x40, _resident_attribute(0x40, 4, b"unsupported")),
        (0x80, _resident_attribute(0x80, 4, b"named-data", name="stream")),
    ],
)
def test_attribute_list_rejects_unsupported_extension_attributes(
    extension_type, extension_attribute
):
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x10, base_reference, attribute_id=1),
            _attribute_list_entry(extension_type, extension_reference, attribute_id=4),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (extension_attribute,),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    with pytest.raises(
        NtfsMetadataError, match="unsupported named or non-DATA/FILE_NAME"
    ):
        reader._attributes_for_record(42, base_record)


def test_attribute_list_rejects_extension_with_wrong_base_reference():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x10, base_reference, attribute_id=1),
            _attribute_list_entry(0x30, extension_reference, attribute_id=4),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_resident_attribute(0x30, 4, b"name"),),
            sequence=8,
            base_file_reference=(6 << 48) | 41,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    with pytest.raises(NtfsMetadataError, match="different base FILE record"):
        reader._attributes_for_record(42, base_record)


def test_attribute_list_rejects_stale_base_sequence_and_wrong_attribute_id():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    stale_base_list = b"".join(
        (
            _attribute_list_entry(0x10, (6 << 48) | 42, attribute_id=1),
            _attribute_list_entry(0x30, extension_reference, attribute_id=4),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, stale_base_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_resident_attribute(0x30, 3, b"child-name"),),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    with pytest.raises(NtfsMetadataError, match="base-record sequence"):
        reader._attributes_for_record(42, base_record)

    valid_base_list = b"".join(
        (
            _attribute_list_entry(0x10, base_reference, attribute_id=1),
            _attribute_list_entry(0x30, extension_reference, attribute_id=4),
        )
    )
    valid_base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _resident_attribute(0x20, 2, valid_base_list),
            ),
            sequence=7,
        )
    )
    with pytest.raises(NtfsMetadataError, match="does not identify one FILE attribute"):
        reader._attributes_for_record(42, valid_base_record)


def test_attribute_list_reads_bounded_nonresident_value():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    value = _attribute_list_entry(0x10, base_reference, attribute_id=1)
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _resident_attribute(0x10, 1, b""),
                _nonresident_attribute(0x20, 2, 0, 0, 10, len(value)),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(_record())
    reader, _base_reference = _attribute_list_reader(extension_record)
    reads = []

    def read_runs(runs, offset, length):
        reads.append((runs, offset, length))
        return value

    reader._read_runs = read_runs
    attributes = reader._attributes_for_record(42, base_record)

    assert attributes[-1].type_code == 0x10
    assert reads[0][1:] == (0, len(value))


def test_attribute_list_merges_adjacent_nonresident_data_extents():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x80, base_reference, lowest_vcn=0, attribute_id=5),
            _attribute_list_entry(
                0x80, extension_reference, lowest_vcn=1, attribute_id=0
            ),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _nonresident_attribute(0x80, 5, 0, 0, 10, 2 * CLUSTER),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_nonresident_attribute(0x80, 9, 1, 1, 11, 2 * CLUSTER),),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    attributes = reader._attributes_for_record(42, base_record)
    data = next(attribute for attribute in attributes if attribute.type_code == 0x80)

    assert data.lowest_vcn == 0
    assert data.highest_vcn == 1
    assert data.data_size == 2 * CLUSTER
    assert [(run.vcn, run.lcn, run.length) for run in data.runs] == [
        (0, 10, 1),
        (1, 11, 1),
    ]
    reader._resolve = lambda _path: (42, base_record)
    reader._read_runs = lambda _runs, _offset, length: b"x" * length
    assert reader.file_size("/large.bin") == 2 * CLUSTER
    assert reader.read_file_range("/large.bin", 0, 16) == b"x" * 16


@pytest.mark.parametrize(
    ("size_field", "extension_size"),
    [
        ("allocated_size", 3 * CLUSTER),
        ("initialized_size", CLUSTER),
    ],
)
def test_attribute_list_rejects_extents_with_conflicting_stream_sizes(
    size_field, extension_size
):
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x80, base_reference, lowest_vcn=0, attribute_id=5),
            _attribute_list_entry(
                0x80, extension_reference, lowest_vcn=1, attribute_id=0
            ),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _nonresident_attribute(0x80, 5, 0, 0, 10, 2 * CLUSTER),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _nonresident_attribute(
                    0x80,
                    0,
                    1,
                    1,
                    11,
                    2 * CLUSTER,
                    **{size_field: extension_size},
                ),
            ),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    with pytest.raises(NtfsMetadataError, match="disagree on allocated/initialized"):
        reader._attributes_for_record(42, base_record)


def test_attribute_list_rejects_data_extents_with_vcn_gap():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    base_reference = (7 << 48) | 42
    extension_reference = (8 << 48) | 43
    attribute_list = b"".join(
        (
            _attribute_list_entry(0x80, base_reference, lowest_vcn=0, attribute_id=5),
            _attribute_list_entry(
                0x80, extension_reference, lowest_vcn=2, attribute_id=0
            ),
        )
    )
    base_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (
                _nonresident_attribute(0x80, 5, 0, 0, 10, 3 * CLUSTER),
                _resident_attribute(0x20, 2, attribute_list),
            ),
            sequence=7,
        )
    )
    extension_record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_nonresident_attribute(0x80, 0, 2, 2, 11, 3 * CLUSTER),),
            sequence=8,
            base_file_reference=base_reference,
        )
    )
    reader, _base_reference = _attribute_list_reader(extension_record)

    with pytest.raises(NtfsMetadataError, match="VCN gap or overlap"):
        reader._attributes_for_record(42, base_record)


@pytest.mark.parametrize(
    ("nonresident", "flags", "message"),
    [
        (False, 0x0001, "compressed, encrypted or sparse"),
        (False, 0x0004, "unknown flag bits"),
        (True, 0x4000, "compressed, encrypted or sparse"),
        (True, 0x0004, "unknown flag bits"),
    ],
)
def test_attribute_stream_rejects_unsupported_and_unknown_flags(
    nonresident, flags, message
):
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    raw = bytearray(
        _record(attr_type=0x80 if nonresident else 0x30, nonresident=nonresident)
    )
    attribute_length = 72 if nonresident else 24
    if nonresident:
        struct.pack_into("<I", raw, 0x3C, attribute_length)
    struct.pack_into("<H", raw, 0x38 + 12, flags)
    used = 0x38 + attribute_length + 4
    struct.pack_into("<I", raw, 0x18, used)
    struct.pack_into("<I", raw, 0x38 + attribute_length, 0xFFFFFFFF)

    record = parse_ntfs_file_record_header(bytes(raw))
    with pytest.raises(NtfsMetadataError, match=message):
        parse_ntfs_attribute_stream(record)


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


def test_rejects_ntfs_resident_attribute_name_overlapping_value():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    attribute = bytearray(_resident_attribute(0x30, 1, b"vv", name="A"))
    struct.pack_into("<H", attribute, 20, 24)
    record = parse_ntfs_file_record_header(_record_with_attributes((bytes(attribute),)))

    with pytest.raises(NtfsMetadataError, match="name overlaps its resident value"):
        parse_ntfs_attribute_stream(record)


def test_record_with_attributes_updates_bytes_in_use_after_serialization():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    attribute = _resident_attribute(0x30, 1, b"payload")
    raw = _record_with_attributes((attribute,))
    expected_used_size = 0x38 + len(attribute) + 4
    assert struct.unpack_from("<I", raw, 0x18)[0] == expected_used_size

    record = parse_ntfs_file_record_header(raw)
    assert record.bytes_in_use == expected_used_size
    assert [item.type_code for item in parse_ntfs_attribute_stream(record)] == [0x30]


def test_rejects_ntfs_nonresident_attribute_name_overlapping_runlist():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    attribute = bytearray(_nonresident_attribute(0x80, 1, 0, 0, 10, 8, name="x"))
    struct.pack_into("<H", attribute, 32, 64)
    record = parse_ntfs_file_record_header(_record_with_attributes((bytes(attribute),)))

    with pytest.raises(NtfsMetadataError, match="name overlaps its runlist"):
        parse_ntfs_attribute_stream(record, cluster_count=TOTAL // SPC)


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


@settings(max_examples=300, deadline=None)
@given(
    raw=st.binary(max_size=64), cluster_count=st.integers(min_value=1, max_value=4096)
)
def test_arbitrary_ntfs_runlists_are_rejected_or_return_bounded_extents(
    raw, cluster_count
):
    try:
        runs = _parse_runlist(raw, cluster_count)
    except NtfsMetadataError:
        return

    assert runs
    expected_vcn = 0
    for run in runs:
        assert run.vcn == expected_vcn
        assert run.length > 0
        assert run.lcn is not None
        assert 0 <= run.lcn < cluster_count
        assert run.lcn + run.length <= cluster_count
        expected_vcn += run.length


def test_nonresident_attribute_preserves_allocated_and_initialized_sizes():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    raw = _record_with_attributes(
        (
            _nonresident_attribute(
                0x80,
                1,
                0,
                0,
                10,
                8,
                allocated_size=CLUSTER,
                initialized_size=4,
            ),
        )
    )
    record = parse_ntfs_file_record_header(raw)
    (attribute,) = parse_ntfs_attribute_stream(record, cluster_count=TOTAL // SPC)

    assert attribute.allocated_size == CLUSTER
    assert attribute.data_size == 8
    assert attribute.initialized_size == 4


@pytest.mark.parametrize(
    ("allocated_size", "data_size", "initialized_size"),
    [(7, 8, 4), (4096, 8, 9)],
)
def test_nonresident_attribute_rejects_inconsistent_sizes(
    allocated_size, data_size, initialized_size
):
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    raw = _record_with_attributes(
        (
            _nonresident_attribute(
                0x80,
                1,
                0,
                0,
                10,
                data_size,
                allocated_size=allocated_size,
                initialized_size=initialized_size,
            ),
        )
    )
    record = parse_ntfs_file_record_header(raw)

    with pytest.raises(NtfsMetadataError, match="sizes are inconsistent"):
        parse_ntfs_attribute_stream(record, cluster_count=TOTAL // SPC)


def test_nonresident_stream_rejects_allocated_size_larger_than_runlist_capacity():
    from mountfile.ntfs_mft import parse_ntfs_file_record_header
    from mountfile.ntfs_reader import _validate_nonresident_data_stream

    raw = _record_with_attributes(
        (
            _nonresident_attribute(
                0x80,
                1,
                0,
                0,
                10,
                100,
                allocated_size=2 * CLUSTER,
                initialized_size=100,
            ),
        )
    )
    record = parse_ntfs_file_record_header(raw)
    (attribute,) = parse_ntfs_attribute_stream(record, cluster_count=TOTAL // SPC)

    with pytest.raises(NtfsMetadataError, match="allocated size exceeds its runlist"):
        _validate_nonresident_data_stream(attribute, CLUSTER, name="test")


def test_ntfs_reads_zero_fill_bytes_beyond_initialized_size(monkeypatch):
    from types import SimpleNamespace

    attribute = NtfsAttributeSummary(
        type_code=0x80,
        length=72,
        nonresident=True,
        name="",
        lowest_vcn=0,
        highest_vcn=0,
        run_count=1,
        runs=(NtfsRun(0, 0, 1),),
        data_size=8,
        attribute_id=1,
        allocated_size=CLUSTER,
        initialized_size=4,
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_count=10000))
    reader._resolve = lambda _path: (1, SimpleNamespace(is_directory=False))
    reader._attributes_for_record = lambda _number, _record: (attribute,)
    reader._read_runs = lambda _runs, _offset, length: b"S" * length

    assert reader.read_file_range("/file", 0, 8) == b"S" * 4 + bytes(4)
    assert reader.read_file_range("/file", 4, 4) == bytes(4)
    assert reader.read_file_range("/file", 2, 4) == b"SS" + bytes(2)


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
    struct.pack_into("<III", value, 0, 0x30, 1, 512)
    value[12] = 1
    struct.pack_into("<III", value, 16, 16, 128, 128)
    struct.pack_into("<I", value, 28, 0)
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


def test_ntfs_index_root_uses_upcase_table_for_unicode_collation():
    table = _unicode_upcase_table()
    sorted_index = _index_root_value(
        [_filename_index_entry("Zebra", 42), _filename_index_entry("Éclair", 43)]
    )
    entries = parse_ntfs_index_root(sorted_index, upcase_table=table)
    assert [entry.name for entry in entries] == ["Zebra", "Éclair"]

    unsorted_index = _index_root_value(
        [_filename_index_entry("Éclair", 42), _filename_index_entry("Zebra", 43)]
    )
    with pytest.raises(NtfsMetadataError, match="duplicate or unsorted"):
        parse_ntfs_index_root(unsorted_index, upcase_table=table)


def test_ntfs_index_root_rejects_casefold_duplicate_unicode_names():
    duplicate_index = _index_root_value(
        [_filename_index_entry("Éclair", 42), _filename_index_entry("éclair", 43)]
    )
    with pytest.raises(NtfsMetadataError, match="duplicate or unsorted"):
        parse_ntfs_index_root(duplicate_index, upcase_table=_unicode_upcase_table())


def test_ntfs_index_root_rejects_malformed_upcase_table():
    with pytest.raises(NtfsMetadataError, match="invalid \\$UpCase table"):
        parse_ntfs_index_root(_index_root_value([]), upcase_table=(0,))


def test_native_ntfs_directory_reader_uses_upcase_collation(monkeypatch):
    from types import SimpleNamespace

    root_value = _index_root_value(
        [_filename_index_entry("Éclair", 42), _filename_index_entry("Zebra", 43)]
    )
    root_attribute = NtfsAttributeSummary(
        type_code=0x90,
        length=len(root_value),
        nonresident=False,
        name="$I30",
        resident_value=root_value,
    )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda *_args, **_kwargs: (root_attribute,),
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_count=128))
    reader._upcase_table = _unicode_upcase_table()
    root_record = SimpleNamespace(sequence_number=3)

    with pytest.raises(NtfsMetadataError, match="duplicate or unsorted"):
        reader.list_dir_by_record(5, root_record)


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


def test_native_ntfs_index_entry_matches_child_filename_metadata(monkeypatch):
    from types import SimpleNamespace

    parent_reference = (3 << 48) | 5
    filename = NtfsAttributeSummary(
        type_code=0x30,
        length=80,
        nonresident=False,
        name="",
        resident_value=_file_name_value("éclair", parent_reference),
    )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda *_args, **_kwargs: (filename,),
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_count=128))
    reader._upcase_table = _unicode_upcase_table()
    entry = NtfsIndexEntry((3 << 48) | 42, "Éclair", False)

    assert reader._validate_index_entry_metadata(
        entry, SimpleNamespace(is_directory=False), parent_reference
    ) == (filename,)


@pytest.mark.parametrize(
    ("filename", "parent_reference", "file_attributes"),
    [
        ("other.txt", (3 << 48) | 5, 0),
        ("Éclair", (4 << 48) | 5, 0),
        ("Éclair", (3 << 48) | 5, 0x10000000),
    ],
)
def test_native_ntfs_index_entry_rejects_inconsistent_child_filename(
    monkeypatch, filename, parent_reference, file_attributes
):
    from types import SimpleNamespace

    attribute = NtfsAttributeSummary(
        type_code=0x30,
        length=80,
        nonresident=False,
        name="",
        resident_value=_file_name_value(filename, parent_reference, file_attributes),
    )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda *_args, **_kwargs: (attribute,),
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_count=128))
    reader._upcase_table = _unicode_upcase_table()
    entry = NtfsIndexEntry((3 << 48) | 42, "Éclair", False)

    with pytest.raises(NtfsMetadataError, match="disagrees with child FILE_NAME"):
        reader._validate_index_entry_metadata(
            entry, SimpleNamespace(is_directory=False), (3 << 48) | 5
        )


def test_native_ntfs_index_entry_rejects_mismatched_mft_directory_flag():
    from types import SimpleNamespace

    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    entry = NtfsIndexEntry((3 << 48) | 42, "folder", False)

    with pytest.raises(NtfsMetadataError, match="directory flag disagrees"):
        reader._validate_index_entry_metadata(
            entry, SimpleNamespace(is_directory=True), (3 << 48) | 5
        )


@pytest.mark.parametrize("name", ["café.txt", "file-📁.txt"])
def test_parses_unicode_ntfs_filename_and_directory_index_key(name):
    encoded_name = name.encode("utf-16le")
    name_units = len(encoded_name) // 2
    filename = bytearray(66 + len(encoded_name))
    struct.pack_into("<Q", filename, 0, (3 << 48) | 5)
    filename[64] = name_units
    filename[65] = 3
    filename[66:] = encoded_name
    assert parse_ntfs_file_name(bytes(filename)).name == name

    key = bytearray(66 + len(encoded_name))
    struct.pack_into("<Q", key, 0, (3 << 48) | 5)
    key[64] = name_units
    key[65] = 3
    key[66:] = encoded_name
    entry_length = (16 + len(key) + 7) & ~7
    value = bytearray(32 + entry_length + 16)
    total_size = 16 + entry_length + 16
    struct.pack_into("<III", value, 16, 16, total_size, total_size)
    struct.pack_into("<QHHI", value, 32, (3 << 48) | 42, entry_length, len(key), 0)
    value[48 : 48 + len(key)] = key
    struct.pack_into("<QHHI", value, 32 + entry_length, 0, 16, 0, 2)
    entries = parse_ntfs_index_root(bytes(value), parent_reference=(3 << 48) | 5)
    assert [entry.name for entry in entries] == [name]


def test_ntfs_paths_use_exact_unicode_and_case_insensitive_ascii_lookup():
    from types import SimpleNamespace

    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reference = (3 << 48) | 42
    root = SimpleNamespace(is_directory=True, sequence_number=3)
    child = SimpleNamespace(is_directory=False, sequence_number=3)
    reader.read_mft_record = lambda number, **_kwargs: root if number == 5 else child
    reader._validate_index_entry_metadata = lambda *_args: ()
    reader.list_dir_by_record = lambda *_args: (
        NtfsIndexEntry(reference, "café.txt", False),
        NtfsIndexEntry(reference + 1, "Readme.TXT", False),
    )

    assert reader._resolve("/café.txt")[0] == 42
    assert reader._resolve("/README.txt")[0] == 43
    with pytest.raises(NtfsMetadataError, match="component not found"):
        reader._resolve("/CAFÉ.txt")

    reader.list_dir = lambda _path: [
        {"name": "café.txt", "is_dir": False, "size": 1},
        {"name": "Readme.TXT", "is_dir": False, "size": 2},
    ]
    assert reader.stat("/café.txt")["size"] == 1
    assert reader.stat("/README.txt")["name"] == "Readme.TXT"
    with pytest.raises(FileNotFoundError):
        reader.stat("/CAFÉ.txt")


def test_ntfs_upcase_table_enables_unicode_case_insensitive_lookup(monkeypatch):
    from types import SimpleNamespace

    values = list(range(1 << 16))
    for codepoint in range(ord("a"), ord("z") + 1):
        values[codepoint] = codepoint - 32
    values[ord("é")] = ord("É")
    values[ord("t")] = ord("T")
    values[ord("x")] = ord("X")
    raw = struct.pack("<65536H", *values)

    table = _parse_ntfs_upcase_table(raw)
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader._upcase_table = table
    assert reader._path_names_equal("CAFÉ.TXT", "café.txt")
    assert reader._path_names_equal("file-📁.txt", "FILE-📁.TXT")
    with pytest.raises(NtfsMetadataError, match="valid UTF-16"):
        reader._path_names_equal("\ud800", "name")
    with pytest.raises(NtfsMetadataError, match="255 UTF-16 code units"):
        reader._path_names_equal("n" * 256, "name")

    reader.profile = SimpleNamespace(boot=SimpleNamespace(cluster_count=128))
    reader.read_mft_record = lambda number: object()
    reader._read_runs = lambda runs, offset, length: raw
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda *_args, **_kwargs: (
            NtfsAttributeSummary(
                type_code=0x80,
                length=64,
                nonresident=True,
                name="",
                lowest_vcn=0,
                highest_vcn=31,
                run_count=1,
                runs=(NtfsRun(0, 1, 32),),
                data_size=len(raw),
            ),
        ),
    )
    reader.profile.boot.cluster_size = 4096
    assert reader._load_upcase_table() == table

    reference = (3 << 48) | 42
    root = SimpleNamespace(is_directory=True, sequence_number=3)
    child = SimpleNamespace(is_directory=False, sequence_number=3)
    reader.read_mft_record = lambda number, **_kwargs: root if number == 5 else child
    reader._validate_index_entry_metadata = lambda *_args: ()
    reader.list_dir_by_record = lambda *_args: (
        NtfsIndexEntry(reference, "café.txt", False),
    )
    assert reader._resolve("/CAFÉ.txt")[0] == 42

    reader.list_dir = lambda _path: [{"name": "café.txt", "is_dir": False, "size": 1}]
    assert reader.stat("/CAFÉ.txt")["name"] == "café.txt"


def test_ntfs_upcase_parser_rejects_noncanonical_table_sizes():
    with pytest.raises(NtfsMetadataError, match="invalid size"):
        _parse_ntfs_upcase_table(bytes(1024))


def test_ntfs_upcase_parser_rejects_non_idempotent_mapping():
    values = list(range(1 << 16))
    for codepoint in range(ord("a"), ord("z") + 1):
        values[codepoint] = codepoint - 32
    values[0x100] = 0x101
    values[0x101] = 0x102
    with pytest.raises(NtfsMetadataError, match="non-idempotent"):
        _parse_ntfs_upcase_table(struct.pack("<65536H", *values))


@pytest.mark.parametrize(
    "failure", ["missing-record", "missing-stream", "wrong-size", "invalid-table"]
)
def test_ntfs_upcase_load_failure_falls_back_to_safe_name_matching(
    monkeypatch, failure
):
    from types import SimpleNamespace

    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(
        boot=SimpleNamespace(cluster_count=128, cluster_size=4096)
    )
    reader._upcase_table = None
    raw = bytes(131072)
    if failure == "invalid-table":
        values = list(range(1 << 16))
        for codepoint in range(ord("a"), ord("z") + 1):
            values[codepoint] = codepoint - 32
        values[0x100] = 0x101
        values[0x101] = 0x102
        raw = struct.pack("<65536H", *values)

    if failure == "missing-record":

        def _missing_record(_number):
            raise NtfsMetadataError("MFT record not available")

        reader.read_mft_record = _missing_record
    else:
        reader.read_mft_record = lambda _number: object()

    if failure == "missing-stream":
        attributes = ()
    else:
        data_size = len(raw) // 2 if failure == "wrong-size" else len(raw)
        attributes = (
            NtfsAttributeSummary(
                type_code=0x80,
                length=64,
                nonresident=True,
                name="",
                lowest_vcn=0,
                highest_vcn=31,
                run_count=1,
                runs=(NtfsRun(0, 1, 32),),
                data_size=data_size,
            ),
        )
    monkeypatch.setattr(
        "mountfile.ntfs_reader.parse_ntfs_attribute_stream",
        lambda *_args, **_kwargs: attributes,
    )
    reader._read_runs = lambda *_args: raw

    assert reader._load_upcase_table() is None
    assert reader._path_names_equal("README.TXT", "Readme.txt")
    assert not reader._path_names_equal("CAFÉ.txt", "café.txt")


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


def test_ntfs_read_file_returns_complete_large_file_in_bounded_ranges():
    from types import SimpleNamespace

    payload = bytes(range(251)) * ((1 << 20) // 251 + 8)
    cluster_count = (len(payload) + CLUSTER - 1) // CLUSTER
    record = SimpleNamespace(is_directory=False)
    data = NtfsAttributeSummary(
        type_code=0x80,
        length=64,
        nonresident=True,
        name="",
        lowest_vcn=0,
        highest_vcn=cluster_count - 1,
        run_count=1,
        runs=(NtfsRun(0, 0, cluster_count),),
        data_size=len(payload),
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(
        boot=SimpleNamespace(cluster_count=10000, cluster_size=CLUSTER)
    )
    reader._resolve = lambda _path: (1, record)
    reader._attributes_for_record = lambda _number, _record: (data,)
    calls = []

    def read_runs(_runs, offset, length):
        calls.append((offset, length))
        return payload[offset : offset + length]

    reader._read_runs = read_runs

    result = reader.read_file("/large.bin")

    assert result == payload
    assert calls == [(0, 1 << 20), (1 << 20, len(payload) - (1 << 20))]


@pytest.mark.parametrize(
    ("offset", "length", "message"),
    [
        (-1, 1, "negative NTFS read range"),
        (0, -1, "negative NTFS read range"),
        (True, 1, "invalid NTFS metadata read request"),
        (0, False, "invalid NTFS metadata read request"),
        (0, (1 << 20) + 1, "exceeds the 1 MiB limit"),
    ],
)
def test_ntfs_read_file_range_rejects_invalid_or_excessive_requests(
    offset, length, message
):
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    with pytest.raises(NtfsMetadataError, match=message):
        reader.read_file_range("/not-opened", offset, length)
