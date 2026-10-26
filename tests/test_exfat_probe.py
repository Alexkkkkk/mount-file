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

VOLUME_SIZE = 2 * 1024 * 1024


def _region(sector_size=512):
    region = bytearray(12 * sector_size)
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
    checksum = exfat_boot_checksum(
        bytes(region[: 11 * sector_size]), sector_size=sector_size
    )
    for offset in range(0, sector_size, 4):
        struct.pack_into("<I", region, 11 * sector_size + offset, checksum)
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
