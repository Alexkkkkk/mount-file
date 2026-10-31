# SPDX-License-Identifier: MIT
"""Property tests for bounded NTFS attribute-list parsing."""

import struct

from hypothesis import given, settings
from hypothesis import strategies as st

from mountfile.ntfs_reader import (
    NtfsAttributeListEntry,
    NtfsMetadataError,
    parse_ntfs_attribute_list,
)


def _attribute_list_entry(
    type_code: int,
    file_reference: int,
    *,
    lowest_vcn: int = 0,
    attribute_id: int = 1,
    name: str = "",
) -> bytes:
    name_bytes = name.encode("utf-16le")
    name_units = len(name_bytes) // 2
    entry_length = (26 + len(name_bytes) + 7) & ~7
    raw = bytearray(entry_length)
    struct.pack_into(
        "<IHBB", raw, 0, type_code, entry_length, name_units, 26 if name else 0
    )
    struct.pack_into("<qQH", raw, 8, lowest_vcn, file_reference, attribute_id)
    raw[26 : 26 + len(name_bytes)] = name_bytes
    return bytes(raw)


@settings(max_examples=300, deadline=None)
@given(
    name=st.text(
        alphabet=st.characters(min_codepoint=1, blacklist_categories=("Cs",)),
        max_size=16,
    ),
    record_number=st.integers(min_value=1, max_value=(1 << 48) - 1),
    sequence=st.integers(min_value=1, max_value=(1 << 16) - 1),
    lowest_vcn=st.integers(min_value=0, max_value=(1 << 40) - 1),
    attribute_id=st.integers(min_value=0, max_value=(1 << 16) - 1),
)
def test_generated_attribute_list_entries_round_trip(
    name, record_number, sequence, lowest_vcn, attribute_id
):
    file_reference = (sequence << 48) | record_number
    raw = _attribute_list_entry(
        0x80,
        file_reference,
        lowest_vcn=lowest_vcn,
        attribute_id=attribute_id,
        name=name,
    )

    (entry,) = parse_ntfs_attribute_list(raw)

    assert entry == NtfsAttributeListEntry(
        0x80, name, lowest_vcn, file_reference, attribute_id
    )


@settings(max_examples=500, deadline=None)
@given(raw=st.binary(max_size=512))
def test_arbitrary_attribute_list_bytes_fail_closed_or_return_bounded_entries(raw):
    try:
        entries = parse_ntfs_attribute_list(raw)
    except NtfsMetadataError:
        return

    assert raw and len(raw) % 8 == 0
    assert entries
    assert len(entries) <= len(raw) // 26
    assert all(entry.type_code not in (0, 0x20, 0xFFFFFFFF) for entry in entries)
    assert all(entry.file_reference >> 48 for entry in entries)
    assert all(entry.lowest_vcn >= 0 and "\x00" not in entry.name for entry in entries)
    assert [entry.type_code for entry in entries] == sorted(
        entry.type_code for entry in entries
    )
    identities = {(entry.type_code, entry.name, entry.lowest_vcn) for entry in entries}
    assert len(identities) == len(entries)
