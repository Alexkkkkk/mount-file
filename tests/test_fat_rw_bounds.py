# SPDX-License-Identifier: MIT
"""Exact selected-extent checks for native FAT mutations."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fat32 import make_fat32_image
from test_fat_reader import _make_fat1216_image

from mountfile.universal_fs_rw import Fat32RW


@pytest.mark.parametrize("fat_type", [12, 16])
def test_native_fat12_and_fat16_writes_are_explicitly_unsupported(tmp_path, fat_type):
    image = tmp_path / f"fat{fat_type}.img"
    _make_fat1216_image(image, fat_type)

    with pytest.raises(ValueError, match="native FAT12/16 writes are unsupported"):
        Fat32RW(image, volume_size=image.stat().st_size)


def test_native_fat_rejects_non_mirrored_fat32_mode(tmp_path):
    image = tmp_path / "non-mirrored.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[40:42] = (0x0080).to_bytes(2, "little")
    backup = 6 * 512
    raw[backup + 40 : backup + 42] = (0x0080).to_bytes(2, "little")
    image.write_bytes(raw)

    with pytest.raises(ValueError, match="non-mirrored"):
        Fat32RW(image, volume_size=image.stat().st_size)


def test_native_fat_rejects_inconsistent_backup_boot_sector(tmp_path):
    image = tmp_path / "bad-backup.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[6 * 512 + 3] ^= 1
    image.write_bytes(raw)

    with pytest.raises(ValueError, match="backup boot sector is inconsistent"):
        Fat32RW(image, volume_size=image.stat().st_size)


def test_native_fat_rejects_short_metadata_write(tmp_path, monkeypatch):
    image = tmp_path / "short-write.img"
    make_fat32_image(image)
    backend = Fat32RW(image, volume_size=image.stat().st_size)
    original_open = Path.open

    class ShortWriteHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self.handle.__exit__(*exc)

        def seek(self, *args):
            return self.handle.seek(*args)

        def write(self, data):
            return max(0, len(data) - 1)

    def short_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if Path(path) == image and mode == "r+b":
            return ShortWriteHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", short_open)
    try:
        with pytest.raises(OSError, match="short write"):
            backend._write_at(512, b"metadata")
    finally:
        backend.close()


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


def test_staged_fat_mutation_does_not_overwrite_concurrent_source_change(
    tmp_path, monkeypatch
):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    backend = Fat32RW(image)
    write_chain = backend._write_chain
    concurrent_bytes = b"concurrent external modification"

    def modify_source_then_write(clusters, data):
        with image.open("ab") as stream:
            stream.write(concurrent_bytes)
        return write_chain(clusters, data)

    monkeypatch.setattr(backend, "_write_chain", modify_source_then_write)
    try:
        with pytest.raises(RuntimeError, match="source image changed"):
            backend.write_file("/CONCURRENT.TXT", b"staged data")
        assert image.read_bytes().endswith(concurrent_bytes)
    finally:
        backend.close()


def test_failed_fat_rollback_quarantines_image_and_retains_snapshot(
    tmp_path, monkeypatch
):
    import mountfile.universal_fs_rw as fs_rw

    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    before = image.read_bytes()
    backend = Fat32RW(image)
    verify = backend._verify_staged_mutation
    replace = fs_rw.os.replace

    def fail_after_commit(target, operation, arguments):
        if Path(target) == image:
            raise OSError("simulated post-commit read-back failure")
        return verify(target, operation, arguments)

    def fail_rollback(source, destination):
        if Path(source).name.endswith(".mountfile-rollback"):
            raise OSError("simulated rollback replace failure")
        return replace(source, destination)

    monkeypatch.setattr(backend, "_verify_staged_mutation", fail_after_commit)
    monkeypatch.setattr(fs_rw.os, "replace", fail_rollback)
    try:
        with pytest.raises(RuntimeError, match="image is quarantined"):
            backend.write_file("/QUARANTINE.TXT", b"not trusted")
        assert backend._quarantine_marker.exists()
        rollback_line = next(
            line
            for line in backend._quarantine_marker.read_text().splitlines()
            if line.startswith("rollback=")
        )
        rollback = Path(rollback_line.split("=", 1)[1])
        assert rollback.read_bytes() == before
        with pytest.raises(RuntimeError, match="is quarantined"):
            backend.write_file("/BLOCKED.TXT", b"must be denied")
    finally:
        backend.close()
