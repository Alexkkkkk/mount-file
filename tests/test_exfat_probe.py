# SPDX-License-Identifier: MIT
"""Synthetic tests for the bounded exFAT boot-region probe."""

import struct

import pytest

from mountfile import vfs
from mountfile.exfat_probe import (
    ExfatProbeError,
    exfat_boot_checksum,
    parse_exfat_boot_region,
)
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.universal_fs_rw import NativeExfatRW

VOLUME_SIZE = 2 * 1024 * 1024


def _region(sector_size=512):
    region = bytearray(24 * sector_size)
    boot = memoryview(region)[:sector_size]
    boot[3:11] = b"EXFAT   "
    struct.pack_into("<Q", boot, 0x40, 0)
    struct.pack_into("<Q", boot, 0x48, VOLUME_SIZE // sector_size)
    struct.pack_into("<I", boot, 0x50, 24)
    struct.pack_into("<I", boot, 0x54, 8)
    struct.pack_into("<I", boot, 0x58, 32)
    struct.pack_into("<I", boot, 0x5C, 100)
    struct.pack_into("<I", boot, 0x60, 2)
    struct.pack_into("<I", boot, 0x64, 0x12345678)
    struct.pack_into("<H", boot, 0x6A, 0)
    boot[0x6C] = {512: 9, 1024: 10, 2048: 11, 4096: 12}[sector_size]
    boot[0x6D] = 0
    boot[0x6E] = 1
    boot[0x70] = 10
    boot[510:512] = b"\x55\xaa"
    region[12 * sector_size : 23 * sector_size] = region[: 11 * sector_size]
    for start in (0, 12 * sector_size):
        checksum = exfat_boot_checksum(
            bytes(region[start : start + 11 * sector_size]),
            sector_size=sector_size,
        )
        for offset in range(0, sector_size, 4):
            struct.pack_into("<I", region, start + 11 * sector_size + offset, checksum)
    return bytes(region)


def test_parses_exfat_geometry_and_checksum():
    profile = parse_exfat_boot_region(_region(), volume_size=VOLUME_SIZE)
    assert profile.bytes_per_sector == 512
    assert profile.cluster_size == 512
    assert profile.fat_offset == 24
    assert profile.cluster_heap_offset == 32
    assert profile.volume_length == VOLUME_SIZE // 512
    assert profile.number_of_fats == 1
    assert profile.root_cluster == 2
    assert profile.percent_in_use == 10


@pytest.mark.parametrize("sector_size", [1024, 2048, 4096])
def test_parses_larger_exfat_sector_sizes(sector_size):
    profile = parse_exfat_boot_region(_region(sector_size), volume_size=VOLUME_SIZE)
    assert profile.bytes_per_sector == sector_size


def test_vfs_probe_uses_partition_offset(tmp_path):
    prefix = b"P" * 73
    path = tmp_path / "exfat.img"
    path.write_bytes(prefix + _region() + bytes(VOLUME_SIZE - len(_region())))
    profile = vfs.probe_exfat_profile(path, offset=len(prefix), volume_size=VOLUME_SIZE)
    assert profile.partition_offset == len(prefix)


def test_vfs_opens_validated_exfat_with_native_provider(tmp_path):
    region = _region()
    path = tmp_path / "exfat.img"
    path.write_bytes(region + bytes(VOLUME_SIZE - len(region)))
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    assert provider.backend_name == "native-exfat-ro"
    provider.close()


def test_native_exfat_reads_a_bounded_file_entry_set(tmp_path):
    region = bytearray(VOLUME_SIZE)
    region[: 24 * 512] = _region()
    cluster_heap = 32 * 512
    fat = 24 * 512
    struct.pack_into("<I", region, fat + 2 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, fat + 3 * 4, 0xFFFFFFFF)
    root = bytearray(512)
    root[0] = 0x85
    root[1] = 2
    struct.pack_into("<H", root, 4, 0)
    root[32] = 0xC0
    root[35] = 9
    struct.pack_into("<I", root, 32 + 20, 3)
    payload = b"hello exFAT"
    struct.pack_into("<Q", root, 32 + 24, len(payload))
    root[64] = 0xC1
    root[66 : 66 + 18] = "HELLO.TXT".encode("utf-16le")
    region[cluster_heap : cluster_heap + 512] = root
    region[cluster_heap + 512 : cluster_heap + 1024] = payload
    path = tmp_path / "exfat-tree.img"
    path.write_bytes(region + bytes(VOLUME_SIZE - len(region)))
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.list_dir("/")[0]["name"] == "HELLO.TXT"
        assert provider.file_size("/HELLO.TXT") == len(payload)
        assert provider.read_file_range("/HELLO.TXT", 2, 5) == payload[2:7]
    finally:
        provider.close()


def _tree_image(path):
    region = bytearray(VOLUME_SIZE)
    region[: 24 * 512] = _region()
    struct.pack_into("<I", region, 24 * 512 + 2 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 3 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 4 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 5 * 4, 0xFFFFFFFF)
    root = bytearray(512)
    root[0] = 0x81
    struct.pack_into("<I", root, 20, 4)
    struct.pack_into("<Q", root, 24, 512)
    root[32] = 0x82
    struct.pack_into("<I", root, 52, 5)
    struct.pack_into("<Q", root, 56, 512)
    root[64] = 0x85
    root[65] = 2
    root[96] = 0xC0
    root[99] = 9
    struct.pack_into("<I", root, 116, 3)
    payload = b"hello exFAT"
    struct.pack_into("<Q", root, 120, len(payload))
    root[128] = 0xC1
    root[130:148] = "HELLO.TXT".encode("utf-16le")
    region[32 * 512 : 33 * 512] = root
    region[33 * 512 : 33 * 512 + len(payload)] = payload
    region[34 * 512 : 35 * 512] = b"\x0f" + bytes(511)
    region[35 * 512 : 36 * 512] = bytes(512)
    path.write_bytes(region)
    return payload


def test_native_exfat_writer_replaces_and_renames_with_staging(tmp_path):
    path = tmp_path / "exfat-writer.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/HELLO.TXT", b"updated")
    writer.rename("/HELLO.TXT", "/RENAMED.TXT")
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/RENAMED.TXT") == b"updated"
    finally:
        provider.close()


def test_native_exfat_writer_rejects_growth_and_allocation_mutations(tmp_path):
    path = tmp_path / "exfat-writer-limits.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    with pytest.raises(ExfatWriteError, match="stream growth"):
        writer.write_file("/HELLO.TXT", b"x" * 513)
    with pytest.raises(ExfatWriteError, match="does not allocate"):
        writer.mkdir("/NEW")
    with pytest.raises(ExfatWriteError, match="allocation bitmap"):
        writer.delete("/HELLO.TXT")


def test_universal_rw_exposes_limited_exfat_strategy(tmp_path):
    path = tmp_path / "exfat-rw.img"
    _tree_image(path)
    backend = NativeExfatRW(path, volume_size=VOLUME_SIZE)
    try:
        assert backend.info().write_strategy == "native-exfat-phase1"
        backend.write_file("/HELLO.TXT", b"adapter")
        assert backend.read("/HELLO.TXT") == b"adapter"
    finally:
        backend.close()


def test_exfat_writer_fails_closed_without_system_metadata(tmp_path):
    path = tmp_path / "exfat-no-metadata.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[32 * 512] = 0
    path.write_bytes(raw)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    with pytest.raises(ExfatWriteError, match="allocation bitmap entry is missing"):
        writer.write_file("/HELLO.TXT", b"blocked")


@pytest.mark.parametrize("mutate", ["signature", "checksum", "heap"])
def test_rejects_corrupt_or_unsafe_boot_region(mutate):
    region = bytearray(_region())
    if mutate == "signature":
        region[3:11] = b"NOTEXFAT"
    elif mutate == "checksum":
        region[-1] ^= 1
    else:
        struct.pack_into("<I", region, 0x58, 10)
    with pytest.raises(ExfatProbeError):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)


def test_rejects_corrupt_exfat_backup_boot_checksum():
    region = bytearray(_region())
    region[23 * 512] ^= 1
    with pytest.raises(ExfatProbeError, match="backup boot-region checksum"):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)


def test_backup_boot_flags_and_percent_in_use_may_be_stale():
    region = bytearray(_region())
    backup = 12 * 512
    struct.pack_into("<H", region, backup + 0x6A, 3)
    region[backup + 0x70] = 70
    profile = parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)
    assert profile.volume_flags == 0
    assert profile.percent_in_use == 10


@pytest.mark.parametrize("mutate", ["volume_length", "fat_count", "fat_size"])
def test_rejects_exfat_geometry_that_only_fit_under_wrong_units(mutate):
    region = bytearray(_region())
    if mutate == "volume_length":
        struct.pack_into("<Q", region, 0x48, VOLUME_SIZE)
    elif mutate == "fat_count":
        region[0x6E] = 0
    else:
        struct.pack_into("<I", region, 0x54, 1)
        struct.pack_into("<I", region, 0x5C, 200)
    checksum = exfat_boot_checksum(bytes(region[: 11 * 512]), sector_size=512)
    for offset in range(0, 512, 4):
        struct.pack_into("<I", region, 11 * 512 + offset, checksum)
    with pytest.raises(ExfatProbeError):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)
