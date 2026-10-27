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


def test_native_fat_inferred_extent_must_match_whole_image(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    backend = Fat32RW(image)
    backend.close()

    with image.open("ab") as stream:
        stream.write(b"trailing bytes")
    with pytest.raises(ValueError, match="must cover the whole image"):
        Fat32RW(image)


def test_native_fat_metadata_writes_cannot_cross_selected_extent(tmp_path):
    image = tmp_path / "partition.img"
    make_fat32_image(image)
    selected_size = image.stat().st_size
    sentinel = b"NEXT-PARTITION-SENTINEL".ljust(512, b"\xa5")
    with image.open("ab") as stream:
        stream.write(sentinel)

    backend = Fat32RW(image, offset=0, volume_size=selected_size)
    try:
        with pytest.raises(
            ValueError, match="FAT write exceeds selected volume bounds"
        ):
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


def test_staged_fat_mutation_preserves_bytes_outside_selected_partition(tmp_path):
    partition = tmp_path / "fat32-volume.img"
    make_fat32_image(partition)
    volume = partition.read_bytes()
    prefix = b"PRECEDING-PARTITION".ljust(4096, b"P")
    suffix = b"FOLLOWING-PARTITION".ljust(4096, b"S")
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + volume + suffix)

    backend = Fat32RW(image, offset=len(prefix), volume_size=len(volume))
    try:
        backend.write_file("/STAGED.TXT", b"transactional payload")
    finally:
        backend.close()

    updated = image.read_bytes()
    assert updated[: len(prefix)] == prefix
    assert updated[len(prefix) + len(volume) :] == suffix
    reopened = Fat32RW(image, offset=len(prefix), volume_size=len(volume))
    try:
        assert reopened.read("/STAGED.TXT") == b"transactional payload"
    finally:
        reopened.close()


def test_staged_fat_readback_failure_restores_original_image(tmp_path, monkeypatch):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    before = image.read_bytes()
    backend = Fat32RW(image)
    verify = backend._verify_staged_mutation

    def fail_after_atomic_replace(target, operation, arguments):
        if Path(target) == image:
            raise OSError("simulated post-commit read-back failure")
        return verify(target, operation, arguments)

    monkeypatch.setattr(backend, "_verify_staged_mutation", fail_after_atomic_replace)
    try:
        with pytest.raises(OSError, match="post-commit read-back failure"):
            backend.write_file("/ROLLBACK.TXT", b"must not persist")
        assert image.read_bytes() == before
        with pytest.raises(FileNotFoundError):
            backend.read("/ROLLBACK.TXT")
    finally:
        backend.close()
