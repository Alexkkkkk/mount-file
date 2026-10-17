# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Synthetic tests for safe raw NAND geometry and bad-block scanning."""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.nand import (
    GeometryError,
    NandBoundsError,
    NandGeometry,
    NandPage,
    iter_nand_pages,
    parse_nand_page,
    read_nand_page,
    scan_bad_block_markers,
)


GEOMETRY = NandGeometry(page_size=4, oob_size=2, block_size=8)


def _image(blocks=3):
    """Build interleaved pages with erased OOB bytes and distinct main data."""
    image = bytearray()
    for block in range(blocks):
        for page in range(GEOMETRY.pages_per_block):
            image.extend(bytes([block, page, 0xA5, 0x5A]))
            image.extend(b"\xff\xff")
    return image


def _set_marker(image, block, page, value, offset=0):
    page_index = block * GEOMETRY.pages_per_block + page
    raw_offset = page_index * GEOMETRY.raw_page_size + GEOMETRY.page_size + offset
    image[raw_offset] = value


def test_geometry_and_exact_page_split():
    assert GEOMETRY.pages_per_block == 2
    assert GEOMETRY.raw_page_size == 6
    assert GEOMETRY.raw_block_size == 12
    page = parse_nand_page(b"data\x01\x02", GEOMETRY, page_index=3)
    assert page == NandPage(page_index=3, data=b"data", oob=b"\x01\x02")
    assert page.raw == b"data\x01\x02"


def test_geometry_rejects_ambiguous_or_inconsistent_values():
    with pytest.raises(GeometryError):
        NandGeometry(page_size=5, oob_size=2, block_size=8)
    with pytest.raises(GeometryError):
        NandGeometry(page_size=4, oob_size=0, block_size=8)
    with pytest.raises(GeometryError):
        NandGeometry(page_size=True, oob_size=2, block_size=8)


def test_page_parser_rejects_short_or_long_input():
    with pytest.raises(NandBoundsError):
        parse_nand_page(b"short", GEOMETRY)
    with pytest.raises(NandBoundsError):
        parse_nand_page(b"data\x01\x02\x03", GEOMETRY)
    with pytest.raises(NandBoundsError):
        parse_nand_page(b"data\x01\x02", GEOMETRY, page_index=-1)


def test_decoder_is_explicit_pluggable_hook_only():
    class UpperDecoder:
        def decode(self, page):
            return NandPage(page.page_index, page.data.upper(), page.oob)

    decoder = UpperDecoder()
    page = parse_nand_page(b"abcd\x01\x02", GEOMETRY, decoder=decoder)
    assert page.data == b"ABCD"
    assert page.oob == b"\x01\x02"

    class InvalidDecoder:
        def decode(self, page):
            return NandPage(page.page_index, b"too short", page.oob)

    with pytest.raises(NandBoundsError):
        parse_nand_page(b"abcd\x01\x02", GEOMETRY, decoder=InvalidDecoder())


def test_bad_block_table_scans_both_configured_marker_pages():
    image = _image()
    _set_marker(image, block=1, page=1, value=0x00)
    table = scan_bad_block_markers(image, GEOMETRY)

    assert table.block_count == 3
    assert table.bad_blocks == (1,)
    assert table.good_blocks == (0, 2)
    assert table.is_bad(0) is False
    assert table.is_bad(1) is True
    status = table.status(1)
    assert status.markers[0].value == b"\xff"
    assert status.markers[1].value == b"\x00"
    assert status.markers[1].page_index == 3
    assert 1 in table


def test_marker_range_and_block_subset_are_explicit():
    image = _image()
    _set_marker(image, block=2, page=0, value=0x00, offset=1)
    table = scan_bad_block_markers(
        image,
        GEOMETRY,
        start_block=1,
        block_count=2,
        marker_offset=1,
        marker_pages=(0,),
    )
    assert table.bad_blocks == (2,)
    assert [entry.block_index for entry in table.entries] == [1, 2]
    assert table.status(2).markers[0].oob_offset == 1


def test_scan_rejects_partial_or_out_of_range_input():
    with pytest.raises(NandBoundsError):
        scan_bad_block_markers(_image()[:-1], GEOMETRY)
    with pytest.raises(NandBoundsError):
        scan_bad_block_markers(_image(), GEOMETRY, start_block=3, block_count=1)
    with pytest.raises(GeometryError):
        scan_bad_block_markers(_image(), GEOMETRY, marker_offset=2, marker_length=1)
    with pytest.raises(GeometryError):
        scan_bad_block_markers(_image(), GEOMETRY, marker_pages=(2,))


def test_page_reads_and_iteration_preserve_stream_position():
    image = bytes(_image())
    stream = io.BytesIO(image)
    stream.seek(1)
    page = read_nand_page(stream, GEOMETRY, page_index=2)
    assert page.data == bytes([1, 0, 0xA5, 0x5A])
    assert stream.tell() == 1

    pages = list(iter_nand_pages(image, GEOMETRY, start_page=1, page_count=2))
    assert [page.page_index for page in pages] == [1, 2]
    assert [page.data[:2] for page in pages] == [b"\x00\x01", b"\x01\x00"]
    with pytest.raises(NandBoundsError):
        list(iter_nand_pages(image + b"x", GEOMETRY))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
