# SPDX-License-Identifier: MIT
"""NTFS resident attribute-list and bounded extension-record tests."""

import struct
from types import SimpleNamespace

import pytest

from mountfile.ntfs_mft import NtfsFileRecordHeader
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsAttributeSummary,
    NtfsMetadataError,
    NtfsRun,
    _select_attribute_stream,
    parse_ntfs_attribute_list,
    parse_ntfs_attribute_stream,
)

RECORD_SIZE = 1024
CLUSTER_SIZE = 4096
FIRST_ATTRIBUTE = 0x38


def _aligned(value):
    return (value + 7) & ~7


def _list_entry(type_code, name, lowest_vcn, file_reference, attribute_id=0):
    encoded_name = name.encode("utf-16le")
    name_offset = 0x1A
    length = _aligned(name_offset + len(encoded_name))
    raw = bytearray(length)
    struct.pack_into(
        "<IHBBQQH",
        raw,
        0,
        type_code,
        length,
        len(encoded_name) // 2,
        name_offset,
        lowest_vcn,
        file_reference,
        attribute_id,
    )
    raw[name_offset : name_offset + len(encoded_name)] = encoded_name
    return bytes(raw)


def _resident_attribute(type_code, value, attribute_id, name=""):
    encoded_name = name.encode("utf-16le")
    name_offset = 24 if encoded_name else 0
    value_offset = _aligned(24 + len(encoded_name))
    length = _aligned(value_offset + len(value))
    raw = bytearray(length)
    struct.pack_into("<IIBB", raw, 0, type_code, length, 0, len(encoded_name) // 2)
    struct.pack_into("<H", raw, 10, name_offset)
    struct.pack_into("<HH", raw, 12, 0, attribute_id)
    struct.pack_into("<I", raw, 16, len(value))
    struct.pack_into("<H", raw, 20, value_offset)
    raw[24 : 24 + len(encoded_name)] = encoded_name
    raw[value_offset : value_offset + len(value)] = value
    return bytes(raw)


def _nonresident_data_attribute(lowest_vcn, highest_vcn, lcn, data_size, attribute_id):
    runlist = bytes((0x11, 1, lcn, 0))
    length = _aligned(64 + len(runlist))
    raw = bytearray(length)
    struct.pack_into("<IIBB", raw, 0, 0x80, length, 1, 0)
    struct.pack_into("<HH", raw, 12, 0, attribute_id)
    struct.pack_into("<QQ", raw, 16, lowest_vcn, highest_vcn)
    struct.pack_into("<H", raw, 32, 64)
    struct.pack_into("<Q", raw, 40, 2 * CLUSTER_SIZE if lowest_vcn == 0 else 0)
    struct.pack_into("<Q", raw, 48, data_size)
    struct.pack_into("<Q", raw, 56, data_size)
    raw[64 : 64 + len(runlist)] = runlist
    return bytes(raw)


def _record(attributes, sequence, base_file_reference=0):
    raw = bytearray(RECORD_SIZE)
    position = FIRST_ATTRIBUTE
    for attribute in attributes:
        raw[position : position + len(attribute)] = attribute
        position += len(attribute)
    raw[position : position + 4] = b"\xff\xff\xff\xff"
    return NtfsFileRecordHeader(
        fixed_record=bytes(raw),
        sequence_number=sequence,
        hard_link_count=1,
        first_attribute_offset=FIRST_ATTRIBUTE,
        flags=1,
        bytes_in_use=position + 4,
        bytes_allocated=RECORD_SIZE,
        base_file_reference=base_file_reference,
        next_attribute_id=16,
        update_sequence_number=0xA55A,
        update_sequence_offset=0x30,
        update_sequence_count=3,
    )


def _reader():
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    reader.profile = SimpleNamespace(
        boot=SimpleNamespace(cluster_count=128, cluster_size=CLUSTER_SIZE)
    )
    return reader


def test_parses_ordered_named_and_unnamed_attribute_list_entries():
    base_reference = (7 << 48) | 10
    extension_reference = (4 << 48) | 11
    raw = _list_entry(0x30, "", 0, base_reference, 2) + _list_entry(
        0x80, "stream", 0, extension_reference, 9
    )

    parsed = parse_ntfs_attribute_list(raw)

    assert [(item.type_code, item.name, item.lowest_vcn) for item in parsed] == [
        (0x30, "", 0),
        (0x80, "stream", 0),
    ]
    assert parsed[0].file_reference == base_reference
    assert parsed[1].attribute_id == 9


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"\x01\x00", "header is truncated"),
        (bytes(32), "invalid type"),
        (_list_entry(0x80, "", 0, 1 << 48)[:24], "header is truncated"),
    ],
)
def test_rejects_malformed_attribute_list_entries(raw, message):
    with pytest.raises(NtfsMetadataError, match=message):
        parse_ntfs_attribute_list(raw)


def test_rejects_duplicate_attribute_list_entries():
    entry = _list_entry(0x80, "", 0, (1 << 48) | 5, 3)
    with pytest.raises(NtfsMetadataError, match="duplicate"):
        parse_ntfs_attribute_list(entry + entry)


def test_resolves_extension_record_and_joins_gap_free_data_extents():
    base_number, base_sequence = 10, 7
    extension_number, extension_sequence = 11, 4
    base_reference = (base_sequence << 48) | base_number
    extension_reference = (extension_sequence << 48) | extension_number
    attribute_list = _list_entry(0x80, "", 0, base_reference, 3) + _list_entry(
        0x80, "", 1, extension_reference, 4
    )
    base_record = _record(
        (
            _resident_attribute(0x20, attribute_list, 1),
            _nonresident_data_attribute(0, 0, 2, 2 * CLUSTER_SIZE, 3),
        ),
        base_sequence,
    )
    extension_record = _record(
        (_nonresident_data_attribute(1, 1, 3, 0, 4),),
        extension_sequence,
        base_reference,
    )
    reader = _reader()
    reader.read_mft_record = lambda number, *, reference: extension_record

    attributes = reader._attributes_for_record(base_number, base_record)
    data = _select_attribute_stream(
        attributes,
        0x80,
        "",
        cluster_size=CLUSTER_SIZE,
        display_name="test DATA",
    )

    assert data is not None
    assert data.data_size == 2 * CLUSTER_SIZE
    assert [(run.vcn, run.lcn, run.length) for run in data.runs] == [
        (0, 2, 1),
        (1, 3, 1),
    ]


def test_rejects_extension_record_with_wrong_base_reference():
    base_reference = (7 << 48) | 10
    extension_reference = (4 << 48) | 11
    attribute_list = _list_entry(0x80, "", 0, extension_reference, 4)
    base_record = _record((_resident_attribute(0x20, attribute_list, 1),), 7)
    extension_record = _record(
        (_nonresident_data_attribute(0, 0, 3, CLUSTER_SIZE, 4),),
        4,
        base_reference + 1,
    )
    reader = _reader()
    reader.read_mft_record = lambda number, *, reference: extension_record

    with pytest.raises(NtfsMetadataError, match="wrong base reference"):
        reader._attributes_for_record(10, base_record)


def test_rejects_attribute_list_attribute_id_mismatch():
    base_reference = (7 << 48) | 10
    attribute_list = _list_entry(0x80, "", 0, base_reference, 99)
    base_record = _record(
        (
            _resident_attribute(0x20, attribute_list, 1),
            _nonresident_data_attribute(0, 0, 2, CLUSTER_SIZE, 3),
        ),
        7,
    )
    reader = _reader()

    with pytest.raises(NtfsMetadataError, match="attribute ID"):
        reader._attributes_for_record(10, base_record)


def test_rejects_nonresident_attribute_list_and_mft_bootstrap_list():
    runlist = bytes((0x11, 1, 2, 0))
    nonresident_list = bytearray(_nonresident_data_attribute(0, 0, 2, 64, 1))
    struct.pack_into("<I", nonresident_list, 0, 0x20)
    record = _record((bytes(nonresident_list),), 7)
    reader = _reader()

    with pytest.raises(NtfsMetadataError, match="nonresident NTFS ATTRIBUTE_LIST"):
        reader._attributes_for_record(10, record)

    with pytest.raises(NtfsMetadataError, match="bootstrap is unsupported"):
        reader._attributes_for_record(
            0, _record((_resident_attribute(0x20, runlist, 1),), 1)
        )


def test_rejects_gapped_nonresident_extents():
    extents = (
        NtfsAttributeSummary(
            0x80, 64, True, "", 0, 0, 1, (NtfsRun(0, 2, 1),), data_size=2 * CLUSTER_SIZE
        ),
        NtfsAttributeSummary(
            0x80, 64, True, "", 2, 2, 1, (NtfsRun(2, 4, 1),), data_size=0
        ),
    )
    with pytest.raises(NtfsMetadataError, match="gap or overlap"):
        _select_attribute_stream(
            extents,
            0x80,
            "",
            cluster_size=CLUSTER_SIZE,
            display_name="test DATA",
        )


def test_rejects_allocated_size_that_disagrees_with_joined_runlist():
    extent = NtfsAttributeSummary(
        0x80,
        64,
        True,
        "",
        0,
        0,
        1,
        (NtfsRun(0, 2, 1),),
        data_size=CLUSTER_SIZE,
        allocated_size=2 * CLUSTER_SIZE,
        initialized_size=CLUSTER_SIZE,
    )
    with pytest.raises(NtfsMetadataError, match="allocated size disagrees"):
        _select_attribute_stream(
            (extent,),
            0x80,
            "",
            cluster_size=CLUSTER_SIZE,
            display_name="test DATA",
        )


def test_ntfs_attribute_range_zero_fills_uninitialized_tail():
    reader = _reader()
    reader._read_runs = lambda _runs, offset, length: b"D" * length
    attribute = NtfsAttributeSummary(
        0x80,
        64,
        True,
        "",
        0,
        0,
        1,
        (NtfsRun(0, 2, 1),),
        data_size=10,
        allocated_size=CLUSTER_SIZE,
        initialized_size=5,
    )

    assert reader._read_attribute_range(attribute, 0, 10) == b"D" * 5 + b"\0" * 5
    assert reader._read_attribute_range(attribute, 7, 8) == b"\0" * 3


def test_attribute_stream_parser_preserves_attribute_instance_id():
    raw_attribute = _nonresident_data_attribute(0, 0, 2, CLUSTER_SIZE, 7)
    record = _record((raw_attribute,), 3)

    parsed = parse_ntfs_attribute_stream(record, cluster_count=128)

    assert parsed[0].attribute_id == 7
    assert parsed[0].allocated_size == 2 * CLUSTER_SIZE
    assert parsed[0].initialized_size == CLUSTER_SIZE
