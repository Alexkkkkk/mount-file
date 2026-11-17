# SPDX-License-Identifier: MIT
"""Safety tests for the read-only positioned image source abstraction."""

import ctypes
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mountfile.image_source import (
    BoundedImageSource,
    FileImageSource,
    ImageSourceError,
    WindowsRawDeviceImageSource,
    _aligned_read_window,
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


def test_raw_read_alignment_refuses_padding_outside_selected_partition():
    assert _aligned_read_window(4101, 9, 4096, 4608, 512) == (4096, 512, 5)
    with pytest.raises(ImageSourceError, match="selected partition bound") as caught:
        _aligned_read_window(4096, 1, 4096, 4607, 512)
    assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS


class _FakeWinFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize("sector_size", [512, 4096])
def test_windows_raw_device_source_uses_aligned_reads_and_returns_exact_range(
    monkeypatch, sector_size
):
    payload = bytes(index % 251 for index in range(8192))
    state = {"position": 0, "calls": []}

    def create_file(*_args):
        return 7

    def set_file_pointer(_handle, distance, result, _origin):
        state["position"] = distance.value
        result._obj.value = distance.value
        return 1

    def read_file(_handle, buffer, length, result, _overlapped):
        position = state["position"]
        address = ctypes.cast(buffer, ctypes.c_void_p).value
        state["calls"].append((position, length, address % sector_size))
        if position % sector_size or length % sector_size or address % sector_size:
            return 0
        chunk = payload[position : position + length]
        if len(chunk) != length:
            return 0
        ctypes.memmove(buffer, chunk, length)
        result._obj.value = length
        state["position"] += length
        return 1

    api = SimpleNamespace(
        CreateFileW=_FakeWinFunction(create_file),
        SetFilePointerEx=_FakeWinFunction(set_file_pointer),
        ReadFile=_FakeWinFunction(read_file),
        CloseHandle=_FakeWinFunction(lambda _handle: 1),
    )
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: api, raising=False)

    with WindowsRawDeviceImageSource(
        r"\\.\PhysicalDrive7",
        size=8192,
        lower_bound=4096,
        sector_size=sector_size,
    ) as source:
        assert source.read_at(4101, 9) == payload[4101:4110]

    assert state["calls"] == [(4096, sector_size, 0)]


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
