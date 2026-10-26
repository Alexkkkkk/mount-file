# SPDX-License-Identifier: MIT
"""Synthetic tests for the bounded pure-Python NTFS boot-sector probe."""

import hashlib
import struct

import pytest

from mountfile import vfs
from mountfile.ntfs_probe import (
    NtfsProbeError,
    parse_ntfs_boot_sector,
    probe_ntfs_boot_sector,
)

_BYTES_PER_SECTOR = 512
_SECTORS_PER_CLUSTER = 8
_TOTAL_SECTORS = 2048
_VOLUME_SIZE = _BYTES_PER_SECTOR * _TOTAL_SECTORS


def _boot_sector(
    *,
    bytes_per_sector=_BYTES_PER_SECTOR,
    sectors_per_cluster=_SECTORS_PER_CLUSTER,
    total_sectors=_TOTAL_SECTORS,
    mft_lcn=4,
    mft_mirror_lcn=8,
    file_record_code=-10,
    index_record_code=1,
    oem=b"NTFS    ",
    signature=b"\x55\xaa",
):
    sector = bytearray(512)
    sector[:3] = b"\xeb\x52\x90"
    sector[3:11] = oem
    struct.pack_into("<H", sector, 0x0B, bytes_per_sector)
    sector[0x0D] = sectors_per_cluster
    struct.pack_into("<Q", sector, 0x28, total_sectors)
    struct.pack_into("<Q", sector, 0x30, mft_lcn)
    struct.pack_into("<Q", sector, 0x38, mft_mirror_lcn)
    struct.pack_into("<b", sector, 0x40, file_record_code)
    struct.pack_into("<b", sector, 0x44, index_record_code)
    struct.pack_into("<Q", sector, 0x48, 0x123456789ABCDEF0)
    struct.pack_into("<I", sector, 0x50, 0xAABBCCDD)
    sector[510:512] = signature
    return bytes(sector)


def test_parses_ntfs_geometry_and_signed_record_size_codes():
    profile = parse_ntfs_boot_sector(_boot_sector(), volume_size=_VOLUME_SIZE)

    assert profile.partition_offset == 0
    assert profile.partition_size == _VOLUME_SIZE
    assert profile.filesystem_size == _VOLUME_SIZE
    assert profile.bytes_per_sector == 512
    assert profile.sectors_per_cluster == 8
    assert profile.cluster_size == 4096
    assert profile.cluster_count == 256
    assert profile.mft_offset == 4 * 4096
    assert profile.mft_mirror_offset == 8 * 4096
    assert profile.file_record_size == 1024
    assert profile.index_record_size == 4096
    assert profile.volume_serial == 0x123456789ABCDEF0
    assert profile.boot_checksum == 0xAABBCCDD
    assert profile.backup_boot_sector_offset == _VOLUME_SIZE - 512


def test_positive_record_size_codes_are_cluster_counts():
    profile = parse_ntfs_boot_sector(
        _boot_sector(file_record_code=2, index_record_code=4),
        volume_size=_VOLUME_SIZE,
    )

    assert profile.file_record_size == 2 * profile.cluster_size
    assert profile.index_record_size == 4 * profile.cluster_size


@pytest.mark.parametrize(
    ("bytes_per_sector", "sectors_per_cluster", "record_code", "expected"),
    [
        (1024, 4, -11, 2048),
        (4096, 1, -12, 4096),
        (512, 1, 1, 512),
    ],
)
def test_supports_bounded_sector_cluster_profiles(
    bytes_per_sector, sectors_per_cluster, record_code, expected
):
    sector = _boot_sector(
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        file_record_code=record_code,
        total_sectors=_VOLUME_SIZE // bytes_per_sector,
    )
    profile = parse_ntfs_boot_sector(sector, volume_size=_VOLUME_SIZE)
    assert profile.file_record_size == expected
    assert profile.cluster_size == bytes_per_sector * sectors_per_cluster


@pytest.mark.parametrize(
    ("sector", "volume_size", "message"),
    [
        (_boot_sector(oem=b"NOTNTFS!"), _VOLUME_SIZE, "OEM"),
        (_boot_sector(signature=b"\x00\x00"), _VOLUME_SIZE, "signature"),
        (_boot_sector(bytes_per_sector=256), _VOLUME_SIZE, "bytes-per-sector"),
        (_boot_sector(sectors_per_cluster=0), _VOLUME_SIZE, "power of two"),
        (_boot_sector(sectors_per_cluster=3), _VOLUME_SIZE, "power of two"),
        (_boot_sector(total_sectors=0), _VOLUME_SIZE, "zero total sectors"),
        (_boot_sector(mft_lcn=256), _VOLUME_SIZE, "MFT location"),
        (_boot_sector(mft_mirror_lcn=256), _VOLUME_SIZE, "MFT location"),
        (_boot_sector(file_record_code=0), _VOLUME_SIZE, "size code is zero"),
        (_boot_sector(file_record_code=-21), _VOLUME_SIZE, "1 MiB"),
        (_boot_sector(index_record_code=0), _VOLUME_SIZE, "size code is zero"),
        (_boot_sector(file_record_code=3), _VOLUME_SIZE, "outside the supported range"),
        (_boot_sector(), _VOLUME_SIZE // 2, "exceeds the selected volume bounds"),
    ],
)
def test_rejects_invalid_or_unsupported_boot_profiles(sector, volume_size, message):
    with pytest.raises(NtfsProbeError, match=message):
        parse_ntfs_boot_sector(sector, volume_size=volume_size)


def test_rejects_truncated_boot_sector():
    with pytest.raises(NtfsProbeError, match="truncated"):
        parse_ntfs_boot_sector(_boot_sector()[:511], volume_size=_VOLUME_SIZE)


def test_requires_explicit_extent_and_rejects_out_of_image_range(tmp_path):
    image_path = tmp_path / "ntfs.img"
    image_path.write_bytes(_boot_sector())

    with pytest.raises(NtfsProbeError, match="explicit integer volume_size"):
        probe_ntfs_boot_sector(image_path)
    with pytest.raises(NtfsProbeError, match="exceeds image bounds"):
        probe_ntfs_boot_sector(image_path, offset=1, volume_size=_VOLUME_SIZE)


def test_probe_uses_partition_offset_and_does_not_modify_image(tmp_path):
    prefix = b"P" * 73
    suffix = b"sentinel" * 8
    image_path = tmp_path / "partitioned-ntfs.img"
    image_path.write_bytes(prefix + _boot_sector() + bytes(_VOLUME_SIZE - 512) + suffix)
    before = hashlib.sha256(image_path.read_bytes()).digest()

    profile = probe_ntfs_boot_sector(
        image_path, offset=len(prefix), volume_size=_VOLUME_SIZE
    )

    assert profile.partition_offset == len(prefix)
    assert profile.backup_boot_sector_image_offset == len(prefix) + _VOLUME_SIZE - 512
    assert hashlib.sha256(image_path.read_bytes()).digest() == before


def test_vfs_ntfs_probe_is_available_without_pytsk3(tmp_path, monkeypatch):
    image_path = tmp_path / "ntfs-vfs.img"
    image_path.write_bytes(_boot_sector() + bytes(_VOLUME_SIZE - 512))
    monkeypatch.setattr(vfs, "pytsk3", None)

    profile = vfs.probe_ntfs_profile(image_path, volume_size=_VOLUME_SIZE)

    assert profile.filesystem_size == _VOLUME_SIZE
    assert profile.file_record_size == 1024
