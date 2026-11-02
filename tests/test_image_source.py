# SPDX-License-Identifier: MIT
"""Safety tests for the read-only positioned image source abstraction."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from mountfile.image_source import (
    BoundedImageSource,
    FileImageSource,
    ImageSourceError,
)
from mountfile.safety import RefusalCode


def test_file_image_source_reads_exact_bytes_without_write_api(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"0123456789")

    with FileImageSource(image) as source:
        assert source.size == 10
        assert source.read_at(2, 4) == b"2345"
        assert source.read_at(10, 0) == b""
        assert not hasattr(source, "write")

    assert source.closed
    with pytest.raises(ImageSourceError, match="closed"):
        source.read_at(0, 1)


@pytest.mark.parametrize(
    ("offset", "length"),
    [(-1, 1), (0, -1), (9, 2), (True, 1), (0, False)],
)
def test_file_image_source_rejects_invalid_ranges(tmp_path, offset, length):
    image = tmp_path / "disk.img"
    image.write_bytes(b"0123456789")
    source = FileImageSource(image)
    try:
        with pytest.raises(ImageSourceError) as caught:
            source.read_at(offset, length)
        assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS
    finally:
        source.close()


def test_bounded_image_source_never_reads_adjacent_extent_bytes(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"PREFIX|partition|SUFFIX")
    parent = FileImageSource(image)
    window = BoundedImageSource(parent, 7, len(b"partition"))
    try:
        assert window.read_at(0, 9) == b"partition"
        with pytest.raises(ImageSourceError, match="selected extent") as caught:
            window.read_at(8, 2)
        assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS
        assert parent.read_at(0, parent.size) == b"PREFIX|partition|SUFFIX"
    finally:
        window.close()
        parent.close()


def test_bounded_image_source_rejects_invalid_window_before_any_reads(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"0123456789")
    parent = FileImageSource(image)
    try:
        with pytest.raises(ImageSourceError, match="parent bounds"):
            BoundedImageSource(parent, 8, 3)
        with pytest.raises(ImageSourceError, match="parent bounds"):
            BoundedImageSource(parent, -1, 1)
    finally:
        parent.close()


def test_file_image_source_concurrent_positioned_reads_do_not_share_a_cursor(
    tmp_path,
):
    payload = bytes(range(256)) * 128
    image = tmp_path / "disk.img"
    image.write_bytes(payload)
    source = FileImageSource(image)
    ranges = [(index * 113, 257) for index in range(200)]
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            actual = list(executor.map(lambda item: source.read_at(*item), ranges))
        assert actual == [
            payload[offset : offset + length] for offset, length in ranges
        ]
    finally:
        source.close()


def test_file_image_source_refuses_short_read_after_image_truncation(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"original data")
    source = FileImageSource(image)
    try:
        image.write_bytes(b"short")
        with pytest.raises(
            ImageSourceError, match="changed or was truncated"
        ) as caught:
            source.read_at(0, source.size)
        assert caught.value.code is RefusalCode.CONCURRENT_CHANGE
    finally:
        source.close()


def test_bounded_image_source_close_does_not_close_parent_by_default(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"0123456789")
    parent = FileImageSource(image)
    window = BoundedImageSource(parent, 2, 4)
    window.close()
    try:
        assert parent.read_at(0, 2) == b"01"
        with pytest.raises(ImageSourceError, match="window is closed"):
            window.read_at(0, 1)
    finally:
        parent.close()
