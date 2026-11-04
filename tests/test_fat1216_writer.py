# SPDX-License-Identifier: MIT
"""Deterministic bounded native FAT12/FAT16 writer coverage."""

from pathlib import Path

import pytest

from mountfile.universal_fs_rw import Fat1216RW
from tests.test_fat_reader import _make_fat1216_image


@pytest.mark.parametrize("fat_type", [12, 16])
def test_fat1216_full_mutation_matrix_and_partition_sentinels(tmp_path, fat_type):
    volume = tmp_path / f"fat{fat_type}.partition"
    _make_fat1216_image(volume, fat_type)
    prefix = b"PREFIX-SENTINEL" * 31
    suffix = b"SUFFIX-SENTINEL" * 29
    image = tmp_path / f"fat{fat_type}.disk"
    image.write_bytes(prefix + volume.read_bytes() + suffix)
    offset = len(prefix)
    size = volume.stat().st_size

    writer = Fat1216RW(image, offset=offset, volume_size=size)
    writer.write_file("/zero.bin", b"")
    writer.write_file("/one.bin", b"x")
    writer.write_file("/boundary.bin", b"b" * 512)
    unicode_name = "Long Unicode é.txt"
    writer.write_file("/" + unicode_name, b"unicode payload")
    writer.mkdir("/Folder")
    writer.write_file("/folder/child.bin", b"c" * 513)
    writer.rename("/" + unicode_name, "/Renamed " + unicode_name)
    assert writer.read("/renamed " + unicode_name) == b"unicode payload"
    writer.delete("/ONE.BIN")
    writer.close()

    updated = image.read_bytes()
    assert updated[:offset] == prefix
    assert updated[offset + size :] == suffix


def test_fat1216_short_write_rolls_back_and_preserves_sentinels(tmp_path, monkeypatch):
    volume = tmp_path / "fat12.partition"
    _make_fat1216_image(volume, 12)
    prefix, suffix = b"P" * 97, b"S" * 113
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + volume.read_bytes() + suffix)
    before = image.read_bytes()
    writer = Fat1216RW(image, offset=len(prefix), volume_size=volume.stat().st_size)

    def fail(*_args, **_kwargs):
        raise OSError("injected short write")

    monkeypatch.setattr(writer, "_write_cluster", fail)
    with pytest.raises(OSError, match="injected short write"):
        writer.write_file("/failed.bin", b"must rollback")
    writer.close()
    assert image.read_bytes() == before


def test_fat1216_mirrors_packed_fat_updates(tmp_path):
    image = tmp_path / "fat16.img"
    _make_fat1216_image(image, 16)
    raw = bytearray(image.read_bytes())
    boot = raw[:512]
    boot[16] = 2
    raw[:512] = boot
    fat_size = int.from_bytes(boot[22:24], "little") * 512
    first = 512
    raw[first + fat_size : first + 2 * fat_size] = raw[first : first + fat_size]
    image.write_bytes(raw)
    writer = Fat1216RW(image, volume_size=image.stat().st_size)
    writer.write_file("/mirrored.bin", b"mirrored")
    writer.close()
    updated = image.read_bytes()
    assert (
        updated[first : first + fat_size]
        == updated[first + fat_size : first + 2 * fat_size]
    )
