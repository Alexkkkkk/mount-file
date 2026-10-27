# SPDX-License-Identifier: MIT
"""Exact selected-extent checks for native FAT mutations."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fat32 import make_fat32_image

from mountfile.universal_fs_rw import Fat32RW


def test_native_fat_rejects_bpb_larger_than_selected_extent(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    exact_size = image.stat().st_size

    with pytest.raises(ValueError, match="BPB exceeds the selected volume extent"):
        Fat32RW(image, volume_size=exact_size - 512)


def test_native_fat_metadata_writes_cannot_cross_selected_extent(tmp_path):
    image = tmp_path / "partition.img"
    make_fat32_image(image)
    selected_size = image.stat().st_size
    sentinel = b"NEXT-PARTITION-SENTINEL".ljust(512, b"\xA5")
    with image.open("ab") as stream:
        stream.write(sentinel)

    backend = Fat32RW(image, offset=0, volume_size=selected_size)
    try:
        with pytest.raises(ValueError, match="FAT write exceeds selected volume bounds"):
            backend._write_at(selected_size - 1, b"XX")
    finally:
        backend.close()

    with image.open("rb") as stream:
        stream.seek(selected_size)
        assert stream.read(len(sentinel)) == sentinel


def test_native_fat_requires_explicit_extent_at_nonzero_offset(tmp_path):
    image = tmp_path / "fat32-partition.img"
    make_fat32_image(image)
    payload = image.read_bytes()
    image.write_bytes(b"P" * 4096 + payload)

    with pytest.raises(ValueError, match="explicit FAT volume_size"):
        Fat32RW(image, offset=4096)
