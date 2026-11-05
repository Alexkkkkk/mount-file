# SPDX-License-Identifier: MIT
"""Deterministic bounded native FAT12/FAT16 writer coverage."""

import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.universal_fs_rw import Fat1216RW
from tests._independent_checkers import CheckerVerdict, check_fat_image
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


@pytest.mark.parametrize(
    ("fat_type", "image_size"),
    [(12, 1440 * 1024), (16, 32 * 1024 * 1024)],
)
def test_real_fat1216_mutations_pass_fsck_on_disposable_copy(
    tmp_path, fat_type, image_size
):
    mkfs = shutil.which("mkfs.fat")
    fsck = shutil.which("fsck.fat")
    if not mkfs or not fsck:
        pytest.skip("dosfstools mkfs.fat and fsck.fat are required")

    image = tmp_path / f"fat{fat_type}-real.img"
    with image.open("wb") as stream:
        stream.truncate(image_size)
    subprocess.run(
        [mkfs, "-F", str(fat_type), "-I", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    payload = bytes(range(256)) * 37 + b"real-image-checker-boundary"
    nested_payload = b"nested FAT writer payload"
    writer = Fat1216RW(image, volume_size=image_size)
    try:
        writer.write_file("/Evidence Long Name.txt", payload)
        writer.mkdir("/Nested")
        writer.write_file("/Nested/record.bin", nested_payload)
        writer.rename("/Evidence Long Name.txt", "/Renamed Long Name.txt")
        writer.delete("/Nested/record.bin")
        assert writer.read("/renamed long name.txt") == payload
    finally:
        writer.close()

    result = check_fat_image(image, timeout=60)
    assert result.available
    assert result.verdict is CheckerVerdict.CLEAN, result.diagnostics()
    assert result.source_unchanged
    assert result.copy_unchanged


@pytest.mark.parametrize(
    ("fat_type", "image_size"),
    [(12, 1440 * 1024), (16, 32 * 1024 * 1024)],
)
def test_real_fat1216_failed_write_rolls_back_and_checker_stays_clean(
    tmp_path, monkeypatch, fat_type, image_size
):
    mkfs = shutil.which("mkfs.fat")
    fsck = shutil.which("fsck.fat")
    if not mkfs or not fsck:
        pytest.skip("dosfstools mkfs.fat and fsck.fat are required")

    image = tmp_path / f"fat{fat_type}-rollback.img"
    with image.open("wb") as stream:
        stream.truncate(image_size)
    subprocess.run(
        [mkfs, "-F", str(fat_type), "-I", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    before = image.read_bytes()
    writer = Fat1216RW(image, volume_size=image_size)

    def fail_write(*_args, **_kwargs):
        raise OSError("injected real-image FAT write failure")

    monkeypatch.setattr(writer, "_write_cluster", fail_write)
    try:
        with pytest.raises(OSError, match="injected real-image FAT write failure"):
            writer.write_file("/NOT-PUBLISHED.BIN", b"must roll back")
    finally:
        writer.close()

    assert image.read_bytes() == before
    result = check_fat_image(image, timeout=60)
    assert result.available
    assert result.verdict is CheckerVerdict.CLEAN, result.diagnostics()
    assert result.source_unchanged
    assert result.copy_unchanged


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
