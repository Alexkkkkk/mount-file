# SPDX-License-Identifier: MIT
"""Bounded failure matrix for the native FAT32, ext2 and exFAT writers.

The cases intentionally use small deterministic payloads and mutate only copies
or synthetic images.  The assertions are limited to the public safety contract:
selected extents may change on success, adjacent partition sentinels must not,
and failed/cancelled writes must not publish a replacement.
"""
from __future__ import annotations

import pytest
from test_exfat_probe import VOLUME_SIZE, _tree_image
from test_ext2_native import MKE2FS, _make_ext2
from test_fat32 import make_fat32_image

from mountfile.exfat_reader import NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.ext2_native import NativeExt2ImageWriter
from mountfile.fat32 import Fat32, Fat32Error

PAYLOADS = (b"", b"native-matrix-" * 3, bytes(range(64)))


class _ShortWriteFile:
    """File proxy that performs one bounded short write, then behaves normally."""

    def __init__(self, stream):
        self._stream = stream
        self._short_once = True

    def write(self, data):
        if self._short_once and data:
            self._short_once = False
            self._stream.write(data[:-1])
            return len(data) - 1
        return self._stream.write(data)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _fat_image_with_sentinels(tmp_path):
    partition = tmp_path / "fat.partition"
    make_fat32_image(partition)
    prefix = b"FAT-PREFIX-" * 17
    suffix = b"FAT-SUFFIX-" * 19
    image = tmp_path / "fat.disk"
    image.write_bytes(prefix + partition.read_bytes() + suffix)
    return image, len(prefix), partition.stat().st_size, prefix, suffix


def test_fat32_short_write_and_cancellation_restore_selected_extent(
    tmp_path, monkeypatch
):
    image, offset, size, prefix, suffix = _fat_image_with_sentinels(tmp_path)
    with image.open("r+b") as stream:
        stream.seek(offset)
        fs = Fat32(stream, offset=offset)
        fs.f = _ShortWriteFile(stream)
        with pytest.raises(Fat32Error, match="short write"):
            fs.write_file("/", "short.bin", b"short-write")
        assert fs.list_dir() == []
        assert image.read_bytes()[:offset] == prefix
        assert image.read_bytes()[offset + size :] == suffix

        def cancel(*_args):
            raise KeyboardInterrupt()

        monkeypatch.setattr(fs, "_write_chain_data", cancel)
        with pytest.raises(KeyboardInterrupt):
            fs.write_file("/", "cancel.bin", b"cancelled")
    assert image.read_bytes()[:offset] == prefix
    assert image.read_bytes()[offset + size :] == suffix


def test_fat32_injected_exception_and_external_suffix_change_are_bounded(
    tmp_path, monkeypatch
):
    image, offset, size, prefix, suffix = _fat_image_with_sentinels(tmp_path)
    with image.open("r+b") as stream:
        fs = Fat32(stream, offset=offset)
        free_before = fs.free_space()
        original = fs._write_chain_data

        def fail_after_partial_write(first, data):
            original(first, data[:1])
            raise OSError("injected FAT32 data failure")

        monkeypatch.setattr(fs, "_write_chain_data", fail_after_partial_write)
        with pytest.raises(OSError, match="injected FAT32 data failure"):
            fs.write_file("/", "failed.bin", b"failure")
        assert fs.list_dir() == []
        assert fs.free_space() == free_before
        monkeypatch.undo()

        # A concurrent edit outside the selected volume is never part of the
        # native writer's write set and must remain visible after a retry.
        changed = b"FAT-CONCURRENT-SUFFIX"
        with image.open("r+b") as external:
            external.seek(offset + size)
            external.write(changed)
        fs.write_file("/", "bounded.bin", b"ok")
    assert image.read_bytes()[:offset] == prefix
    final_suffix = image.read_bytes()[offset + size :]
    assert final_suffix.startswith(changed)
    assert final_suffix[len(changed) :] == suffix[len(changed) :]


@pytest.mark.skipif(not MKE2FS, reason="mke2fs is required for ext2 fixtures")
@pytest.mark.parametrize("payload", PAYLOADS)
def test_ext2_bounded_success_preserves_partition_sentinels(tmp_path, payload):
    partition = tmp_path / "ext2.partition"
    _make_ext2(partition, size=8 * 1024 * 1024)
    prefix = b"EXT2-PREFIX" * 13
    suffix = b"EXT2-SUFFIX" * 11
    image = tmp_path / "ext2.disk"
    image.write_bytes(prefix + partition.read_bytes() + suffix)
    writer = NativeExt2ImageWriter(
        image, offset=len(prefix), volume_size=partition.stat().st_size
    )
    writer.write_file("/matrix.bin", payload)
    assert image.read_bytes()[: len(prefix)] == prefix
    assert image.read_bytes()[-len(suffix) :] == suffix
    assert writer.read_file("/matrix.bin") == payload


@pytest.mark.skipif(not MKE2FS, reason="mke2fs is required for ext2 fixtures")
def test_ext2_cancellation_leaves_original_and_repeated_recovery_is_idempotent(
    tmp_path, monkeypatch
):
    image = tmp_path / "ext2-cancel.img"
    _make_ext2(image, size=8 * 1024 * 1024)
    writer = NativeExt2ImageWriter(image)
    before = image.read_bytes()

    def cancel_commit(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(writer, "_commit", cancel_commit)
    with pytest.raises(KeyboardInterrupt):
        writer.write_file("/cancelled", b"not published")
    assert image.read_bytes() == before
    monkeypatch.undo()
    recovered = NativeExt2ImageWriter(image)
    # Re-opening and retrying repeatedly must not manufacture a recovery sidecar
    # or alter the original when the staged operation is rejected.
    for index in range(2):
        recovered.write_file(f"/retry-{index}", b"ok")
        assert recovered.read_file(f"/retry-{index}") == b"ok"


def test_exfat_short_write_exception_and_cancellation_never_publish(
    tmp_path, monkeypatch
):
    image = tmp_path / "exfat-matrix.img"
    _tree_image(image)
    prefix = b"EXFAT-PREFIX" * 7
    suffix = b"EXFAT-SUFFIX" * 9
    bounded = tmp_path / "exfat-bounded.img"
    bounded.write_bytes(prefix + image.read_bytes() + suffix)
    before = bounded.read_bytes()
    writer = NativeExfatWriter(bounded, offset=len(prefix), volume_size=VOLUME_SIZE)

    def fail_write(stream, relative, data):
        if data:
            stream.seek(len(prefix) + relative)
            stream.write(data[:1])
        raise ExfatWriteError("injected exFAT short write")

    monkeypatch.setattr(writer, "_write_rel", fail_write)
    with pytest.raises(ExfatWriteError, match="short write"):
        writer.write_file("/FAILED.TXT", b"failure")
    assert bounded.read_bytes() == before

    monkeypatch.undo()

    def cancel(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(writer, "_commit_stage", cancel)
    with pytest.raises(KeyboardInterrupt):
        writer.write_file("/CANCEL.TXT", b"cancelled")
    assert bounded.read_bytes() == before
    assert bounded.read_bytes()[: len(prefix)] == prefix
    assert bounded.read_bytes()[-len(suffix) :] == suffix


def test_exfat_success_and_repeated_recovery_safe_retries_preserve_sentinels(tmp_path):
    partition = tmp_path / "exfat.partition"
    _tree_image(partition)
    prefix = b"P" * 73
    suffix = b"S" * 91
    image = tmp_path / "exfat.disk"
    image.write_bytes(prefix + partition.read_bytes() + suffix)
    writer = NativeExfatWriter(image, offset=len(prefix), volume_size=VOLUME_SIZE)
    for index in range(2):
        writer.write_file(f"/RETRY-{index}.TXT", bytes([index]))
    reader = NativeExfatImage(image, offset=len(prefix), volume_size=VOLUME_SIZE)
    try:
        assert reader.read_file("/RETRY-0.TXT") == b"\0"
        assert reader.read_file("/RETRY-1.TXT") == b"\1"
    finally:
        reader.close()
    assert image.read_bytes()[: len(prefix)] == prefix
    assert image.read_bytes()[-len(suffix) :] == suffix
