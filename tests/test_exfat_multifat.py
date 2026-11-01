"""Regression coverage for the bounded exFAT multi-FAT profile."""

import struct

import pytest

from mountfile.exfat_probe import ExfatProbeError, exfat_boot_checksum, parse_exfat_boot_region


def _boot(number_of_fats=2, flags=1, backup_number_of_fats=None):
    sector = 512
    region = bytearray(24 * sector)
    for start in (0, 12 * sector):
        boot = memoryview(region)[start : start + sector]
        boot[3:11] = b"EXFAT   "
        struct.pack_into("<Q", boot, 0x48, 4096)
        struct.pack_into("<I", boot, 0x50, 24)
        struct.pack_into("<I", boot, 0x54, 8)
        struct.pack_into("<I", boot, 0x58, 40 if number_of_fats == 2 else 32)
        struct.pack_into("<I", boot, 0x5C, 100)
        struct.pack_into("<I", boot, 0x60, 2)
        struct.pack_into("<H", boot, 0x6A, flags)
        boot[0x6C] = 9
        boot[0x6E] = number_of_fats
        boot[510:512] = b"\x55\xaa"
    if backup_number_of_fats is not None:
        region[12 * sector + 0x6E] = backup_number_of_fats
    for start in (0, 12 * sector):
        checksum = exfat_boot_checksum(bytes(region[start : start + 11 * sector]), sector_size=sector)
        for offset in range(0, sector, 4):
            struct.pack_into("<I", region, start + 11 * sector + offset, checksum)
    return bytes(region)


@pytest.mark.parametrize("flags, active", [(0, 0), (1, 1), (2, 0), (0xA, 0)])
def test_active_fat_is_decoded_from_volume_flags(flags, active):
    profile = parse_exfat_boot_region(_boot(flags=flags), volume_size=4096 * 512)
    assert profile.number_of_fats == 2
    assert profile.active_fat == active
    assert profile.volume_flags == flags


def test_one_fat_cannot_select_fat_copy_two():
    with pytest.raises(ExfatProbeError, match="active FAT"):
        parse_exfat_boot_region(_boot(number_of_fats=1, flags=1), volume_size=4096 * 512)


def test_backup_number_of_fats_is_geometry_and_must_reconcile():
    with pytest.raises(ExfatProbeError, match="geometry mismatch"):
        parse_exfat_boot_region(
            _boot(number_of_fats=2, flags=1, backup_number_of_fats=1),
            volume_size=4096 * 512,
        )
