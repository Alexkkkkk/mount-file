# SPDX-License-Identifier: MIT
"""Regression tests for the bounded pure-Python FAT reader."""

from __future__ import annotations

import struct

import pytest

from mountfile import vfs
from mountfile.fat32 import Fat32
from mountfile.fat_reader import FatReadError, NativeFatImage
from tests.test_fat32 import make_fat32_image


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    offset = (cluster * 3) // 2
    current = struct.unpack_from("<H", fat, offset)[0]
    if cluster & 1:
        current = (current & 0x000F) | ((value & 0x0FFF) << 4)
    else:
        current = (current & 0xF000) | (value & 0x0FFF)
    struct.pack_into("<H", fat, offset, current)


def _make_small_fat(path, fat_type: int) -> bytes:
    bps = 512
    if fat_type == 12:
        total_sectors, reserved, fat_sectors, root_entries = 3000, 1, 10, 224
    elif fat_type == 16:
        total_sectors, reserved, fat_sectors, root_entries = 7000, 1, 32, 512
    else:  # pragma: no cover - callers use the existing complete FAT32 fixture
        raise AssertionError(fat_type)
    root_sectors = (root_entries * 32 + bps - 1) // bps
    data_sectors = total_sectors - reserved - fat_sectors - root_sectors
    with path.open("wb") as stream:
        stream.truncate(total_sectors * bps)
        boot = bytearray(bps)
        boot[:3] = b"\xeb\x3c\x90"
        boot[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", boot, 11, bps)
        boot[13] = 1
        struct.pack_into("<H", boot, 14, reserved)
        boot[16] = 1
        struct.pack_into("<H", boot, 17, root_entries)
        struct.pack_into("<H", boot, 19, total_sectors)
        boot[21] = 0xF8
        struct.pack_into("<H", boot, 22, fat_sectors)
        boot[510:512] = b"\x55\xaa"
        label_offset = 54 if fat_type in (12, 16) else 82
        boot[label_offset : label_offset + 8] = f"FAT{fat_type}   ".encode()
        stream.write(boot)
        fat = bytearray(fat_sectors * bps)
        if fat_type == 12:
            _set_fat12_entry(fat, 0, 0xFF8)
            _set_fat12_entry(fat, 1, 0xFFF)
            _set_fat12_entry(fat, 2, 0xFFF)
        else:
            struct.pack_into("<HHH", fat, 0, 0xFFF8, 0xFFFF, 0xFFFF)
        stream.seek(reserved * bps)
        stream.write(fat)
        entry = bytearray(32)
        entry[:11] = b"HELLO   TXT"
        entry[11] = 0x20
        struct.pack_into("<H", entry, 26, 2)
        payload = f"hello FAT{fat_type}\n".encode()
        struct.pack_into("<I", entry, 28, len(payload))
        stream.seek((reserved + fat_sectors) * bps)
        stream.write(entry)
        data_start = (reserved + fat_sectors + root_sectors) * bps
        stream.seek(data_start)
        stream.write(payload.ljust(bps, b"\x00"))
    return payload


@pytest.mark.parametrize("fat_type", [12, 16])
def test_native_fat12_and_fat16_read_file(tmp_path, fat_type):
    image = tmp_path / f"fat{fat_type}.img"
    payload = _make_small_fat(image, fat_type)
    provider = NativeFatImage(image)
    try:
        assert provider.fat_type == fat_type
        assert provider.backend_name == f"native-fat{fat_type}-ro"
        assert provider.read_file("/hello.txt") == payload
        assert provider.read_file_range("/HELLO.TXT", 2, 5) == payload[2:7]
        assert provider.list_dir("/")[0]["name"] == "HELLO.TXT"
    finally:
        provider.close()


def test_native_fat32_reads_lfn_and_uses_native_backend(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    payload = b"native FAT32\n"
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        writer.write_file("/", "A long native name.txt", payload)
    provider = NativeFatImage(image)
    try:
        assert provider.backend_name == "native-fat32-ro"
        assert provider.read_file("/A long native name.txt") == payload
    finally:
        provider.close()


def test_native_fat32_rejects_inconsistent_backup_boot_sector(tmp_path):
    image = tmp_path / "fat32-bad-backup.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[6 * 512 + 3] ^= 1
    image.write_bytes(raw)

    with pytest.raises(FatReadError, match="backup boot sector"):
        NativeFatImage(image, volume_size=image.stat().st_size)


def test_native_fat32_rejects_inconsistent_mirrored_fat(tmp_path):
    image = tmp_path / "fat32-bad-mirror.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[16] = 2
    raw[6 * 512 + 16] = 2
    raw[(32 + 600) * 512 + 8] ^= 1
    image.write_bytes(raw)

    with pytest.raises(FatReadError, match="mirrored FAT copies"):
        NativeFatImage(image, volume_size=image.stat().st_size)


def test_native_fat_rejects_short_chain_and_never_falls_back(tmp_path):
    image = tmp_path / "fat12.img"
    _make_small_fat(image, 12)
    with image.open("r+b") as stream:
        stream.seek(1 * 512 + 3)  # FAT12 entry for cluster 2
        raw = bytearray(stream.read(2))
        raw[0] &= 0x0F
        raw[1] = 0
        stream.seek(1 * 512 + 3)
        stream.write(raw)
    provider = NativeFatImage(image)
    try:
        with pytest.raises(FatReadError, match="shorter|free|invalid"):
            provider.read_file("/HELLO.TXT")
    finally:
        provider.close()


def test_vfs_selects_native_fat_without_pytsk3(tmp_path, monkeypatch):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    monkeypatch.setattr(vfs, "pytsk3", None)
    provider = vfs.open_image_provider(str(image), volume_size=image.stat().st_size)
    try:
        assert provider.backend_name == "native-fat32-ro"
        assert provider.list_dir("/") == []
    finally:
        provider.close()
