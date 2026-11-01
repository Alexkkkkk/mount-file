"""Regression coverage for the bounded exFAT multi-FAT profile."""

import struct
from types import SimpleNamespace

import pytest

from mountfile.exfat_probe import (
    ExfatProbeError,
    exfat_boot_checksum,
    parse_exfat_boot_region,
)
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter


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
        checksum = exfat_boot_checksum(
            bytes(region[start : start + 11 * sector]), sector_size=sector
        )
        for offset in range(0, sector, 4):
            struct.pack_into("<I", region, start + 11 * sector + offset, checksum)
    return bytes(region)


@pytest.mark.parametrize("flags, active", [(0, 0), (1, 1), (2, 0), (0x8, 0), (0xA, 0)])
def test_active_fat_is_decoded_from_volume_flags(flags, active):
    profile = parse_exfat_boot_region(_boot(flags=flags), volume_size=4096 * 512)
    assert profile.number_of_fats == 2
    assert profile.active_fat == active
    assert profile.volume_flags == flags


def test_one_fat_cannot_select_fat_copy_two():
    with pytest.raises(ExfatProbeError, match="active FAT"):
        parse_exfat_boot_region(
            _boot(number_of_fats=1, flags=1), volume_size=4096 * 512
        )


def test_backup_number_of_fats_is_geometry_and_must_reconcile():
    with pytest.raises(ExfatProbeError, match="geometry mismatch"):
        parse_exfat_boot_region(
            _boot(number_of_fats=2, flags=1, backup_number_of_fats=1),
            volume_size=4096 * 512,
        )


def test_reserved_high_volume_flags_are_rejected():
    with pytest.raises(ExfatProbeError, match="reserved bits"):
        parse_exfat_boot_region(_boot(flags=0x10), volume_size=4096 * 512)


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (0x0002, "volume is dirty"),
        (0x0004, "media failure"),
        (0x0008, "ClearToZero"),
        (0x0010, "reserved bits"),
    ],
)
def test_writer_refuses_unsafe_volume_flags_before_staging(
    tmp_path, monkeypatch, flags, message
):
    image = tmp_path / "volume.img"
    image.write_bytes(b"source")
    profile = SimpleNamespace(
        volume_flags=flags,
        volume_length=1,
        bytes_per_sector=512,
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer._image_identity",
        lambda *_args, **_kwargs: (6, b"x" * 32),
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer.probe_exfat_boot_region",
        lambda *_args, **_kwargs: profile,
    )

    with pytest.raises(ExfatWriteError, match=message):
        NativeExfatWriter(image, volume_size=512)

    assert image.read_bytes() == b"source"
    assert list(tmp_path.glob("*.stage")) == []


def test_writer_mirrors_fat_entry_updates_to_all_declared_copies():
    writer = NativeExfatWriter.__new__(NativeExfatWriter)
    writes = []
    writer._write_rel = lambda _stream, offset, data: writes.append((offset, data))
    reader = SimpleNamespace(
        profile=SimpleNamespace(
            number_of_fats=2,
            fat_offset=24,
            fat_length=8,
            bytes_per_sector=512,
        )
    )

    writer._fat_write(object(), reader, 3, 0x12345678)

    assert writes == [
        (24 * 512 + 3 * 4, struct.pack("<I", 0x12345678)),
        (32 * 512 + 3 * 4, struct.pack("<I", 0x12345678)),
    ]
