# SPDX-License-Identifier: MIT
"""Tests for the independent read-only unallocated signature scanner."""

from io import BytesIO
from pathlib import Path

import pytest

from mountfile.dfir_unallocated_scan import (
    FileSignature,
    InvalidRange,
    UnallocatedScanner,
    scan_unallocated,
)


def test_yara_signature_spanning_chunk_boundary_has_exact_absolute_offset():
    from types import SimpleNamespace

    from mountfile.yara_scan import scan_unallocated_from_fs

    signature = b"BOUNDARY" * (8 * 1024 + 1)
    image = b"x" * 6 + signature + b"y" * 12

    class FakeFs:
        _image_path = None
        info = SimpleNamespace(block_size=512)

        def open_dir(self, _path):
            return []

        def read_random(self, offset, length):
            return image[offset : offset + length]

    class FakeRules:
        temp_path = None

        def match(self, *, filepath):
            self.temp_path = filepath
            with open(filepath, "rb") as stream:
                data = stream.read()
            result = []
            start = 0
            while True:
                offset = data.find(signature, start)
                if offset < 0:
                    break
                result.append(
                    SimpleNamespace(
                        rule="boundary",
                        namespace="default",
                        tags=(),
                        strings=[
                            SimpleNamespace(
                                identifier="$a",
                                instances=[SimpleNamespace(offset=offset)],
                            )
                        ],
                    )
                )
                start = offset + 1
            return result

    rules = FakeRules()
    hits = scan_unallocated_from_fs(
        FakeFs(), rules, len(image), chunk=8 * 1024
    )

    assert [(hit.rule, hit.offset, hit.target) for hit in hits] == [
        ("boundary", 6, "unallocated")
    ]
    assert rules.temp_path and not Path(rules.temp_path).exists()


def test_reports_absolute_offsets_for_common_signatures():
    image = bytearray(b"x" * 80)
    image[7:15] = b"\x89PNG\r\n\x1a\n"
    image[43:48] = b"%PDF-"

    hits = list(scan_unallocated(bytes(image), [(4, 50)]))

    assert [(hit.offset, hit.signature) for hit in hits] == [(7, "png"), (43, "pdf")]
    assert hits[0].absolute_offset == 7
    assert hits[0].as_dict()["offset"] == 7


def test_matches_signatures_across_bounded_read_chunks_without_crossing_range_end():
    image = b"a" * 5 + b"PK\x03\x04" + b"b" * 20

    hits = list(scan_unallocated(image, [(0, len(image))], chunk_size=3))
    assert [(hit.offset, hit.signature) for hit in hits] == [(5, "zip")]

    # The prefix at the edge is deliberately incomplete within this supplied
    # range; the scanner must not assume bytes in the following range belong to
    # the same unallocated extent.
    assert list(scan_unallocated(image, [(0, 7)], chunk_size=2)) == []


def test_overlapping_ranges_are_coalesced_without_duplicate_hits():
    image = b"_" * 10 + b"GIF89a" + b"_" * 10

    hits = list(scan_unallocated(image, [(0, 16), (8, 12)]))
    assert [(hit.offset, hit.signature) for hit in hits] == [(10, "gif")]


def test_rejects_ranges_outside_image_before_reading():
    source = BytesIO(b"0123456789")

    with pytest.raises(InvalidRange):
        scan_unallocated(source, [(9, 2)])
    assert source.tell() == 0

    with pytest.raises(InvalidRange):
        scan_unallocated(b"0123456789", [(-1, 1)])


def test_requires_explicit_size_for_callable_and_bounds_it():
    image = b"prefix" + b"MZ" + b"suffix"
    calls = []

    def read_at(offset, length):
        calls.append((offset, length))
        return image[offset : offset + length]

    hits = list(
        scan_unallocated(
            read_at, [(0, len(image))], image_size=len(image), chunk_size=4
        )
    )
    assert [(hit.offset, hit.signature) for hit in hits] == [(6, "pe")]
    assert all(length <= 4 for _offset, length in calls)

    with pytest.raises(ValueError):
        scan_unallocated(read_at, [(0, len(image))])


def test_custom_signatures_and_reusable_scanner_are_read_only():
    image = bytearray(b"." * 32)
    image[12:16] = b"TEST"
    before = bytes(image)
    scanner = UnallocatedScanner(
        image,
        signatures=(FileSignature("fixture", b"TEST", "synthetic test file"),),
        chunk_size=2,
    )

    hits = list(scanner.scan([(10, 8)]))

    assert [(hit.offset, hit.signature, hit.description) for hit in hits] == [
        (12, "fixture", "synthetic test file")
    ]
    assert bytes(image) == before


def test_image_size_mismatch_and_read_limits_are_rejected():
    with pytest.raises(ValueError):
        scan_unallocated(b"abc", [(0, 3)], image_size=4)
    with pytest.raises(ValueError):
        scan_unallocated(b"abc", [(0, 3)], chunk_size=17 * 1024 * 1024)
