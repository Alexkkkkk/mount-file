# SPDX-License-Identifier: MIT
"""Bounded, read-only exFAT boot-region and geometry validation."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, replace

_SIGNATURE = b"EXFAT   "
_BOOT_REGION_SECTORS = 12
_MAX_SECTOR_SIZE = 4096
_MAX_CLUSTER_SIZE = 32 * 1024 * 1024


class ExfatProbeError(ValueError):
    """The exFAT boot region or declared geometry is invalid."""


@dataclass(frozen=True)
class ExfatBootProfile:
    partition_offset: int
    partition_size: int
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    volume_length: int
    fat_offset: int
    fat_length: int
    cluster_heap_offset: int
    cluster_count: int
    root_cluster: int
    volume_serial: int
    volume_flags: int
    percent_in_use: int
    boot_checksum: int


def exfat_boot_checksum(boot_region: bytes | bytearray) -> int:
    """Compute the exFAT rotating checksum for sectors 0..10."""
    if len(boot_region) != 11 * 512:
        raise ExfatProbeError("exFAT boot region must contain exactly 11 sectors")
    checksum = 0
    for index, value in enumerate(boot_region):
        if index in (106, 107, 112):
            continue
        checksum = ((checksum >> 1) | ((checksum & 1) << 31)) & 0xFFFFFFFF
        checksum = (checksum + value) & 0xFFFFFFFF
    return checksum


def parse_exfat_boot_region(region: bytes, *, volume_size: int) -> ExfatBootProfile:
    if not isinstance(volume_size, int) or isinstance(volume_size, bool) or volume_size <= 0:
        raise ExfatProbeError("a positive integer volume_size is required")
    if len(region) < 12 * 512:
        raise ExfatProbeError("truncated exFAT boot region")
    boot = region[:512]
    if boot[3:11] != _SIGNATURE:
        raise ExfatProbeError("exFAT signature is missing")
    if boot[510:512] != b"\x55\xaa":
        raise ExfatProbeError("exFAT boot signature is invalid")
    partition_offset = struct.unpack_from("<Q", boot, 0x40)[0]
    volume_length = struct.unpack_from("<Q", boot, 0x48)[0]
    fat_offset = struct.unpack_from("<I", boot, 0x50)[0]
    fat_length = struct.unpack_from("<I", boot, 0x54)[0]
    heap_offset = struct.unpack_from("<I", boot, 0x58)[0]
    cluster_count = struct.unpack_from("<I", boot, 0x5C)[0]
    root_cluster = struct.unpack_from("<I", boot, 0x60)[0]
    bytes_shift = boot[0x6C]
    sectors_shift = boot[0x6D]
    if not 9 <= bytes_shift <= 12:
        raise ExfatProbeError("unsupported exFAT bytes-per-sector shift")
    if sectors_shift > 25:
        raise ExfatProbeError("exFAT sectors-per-cluster shift is invalid")
    bytes_per_sector = 1 << bytes_shift
    sectors_per_cluster = 1 << sectors_shift
    cluster_size = bytes_per_sector * sectors_per_cluster
    if cluster_size > _MAX_CLUSTER_SIZE:
        raise ExfatProbeError("exFAT cluster size is too large")
    if volume_length == 0 or volume_length > volume_size:
        raise ExfatProbeError("exFAT volume length exceeds selected bounds")
    total_sectors = volume_length // bytes_per_sector
    if fat_offset < 24 or fat_length == 0 or fat_offset + fat_length > total_sectors:
        raise ExfatProbeError("exFAT FAT extent is invalid")
    if heap_offset < fat_offset + fat_length or heap_offset >= total_sectors:
        raise ExfatProbeError("exFAT cluster heap offset is invalid")
    if cluster_count == 0 or heap_offset + cluster_count * sectors_per_cluster > total_sectors:
        raise ExfatProbeError("exFAT cluster heap exceeds volume bounds")
    if not 2 <= root_cluster <= cluster_count + 1:
        raise ExfatProbeError("exFAT root directory cluster is invalid")
    percent = boot[0x70]
    if percent > 100:
        raise ExfatProbeError("exFAT percent-in-use is invalid")
    calculated = exfat_boot_checksum(region[: 11 * 512])
    stored_words = [struct.unpack_from("<I", region, 11 * 512 + n)[0] for n in range(0, 512, 4)]
    if not stored_words or any(word != calculated for word in stored_words):
        raise ExfatProbeError("exFAT boot-region checksum mismatch")
    return ExfatBootProfile(
        0,
        volume_size,
        bytes_per_sector,
        sectors_per_cluster,
        cluster_size,
        volume_length,
        fat_offset,
        fat_length,
        heap_offset,
        cluster_count,
        root_cluster,
        struct.unpack_from("<I", boot, 0x64)[0],
        struct.unpack_from("<H", boot, 0x6A)[0],
        percent,
        calculated,
    )


def probe_exfat_boot_region(image_path, *, offset: int = 0, volume_size: int | None = None) -> ExfatBootProfile:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ExfatProbeError("offset must be a non-negative integer")
    if not isinstance(volume_size, int) or isinstance(volume_size, bool) or volume_size <= 0:
        raise ExfatProbeError("a positive integer volume_size is required")
    try:
        with open(image_path, "rb") as stream:
            image_size = os.fstat(stream.fileno()).st_size
            if offset + volume_size > image_size:
                raise ExfatProbeError("selected volume extent exceeds image bounds")
            stream.seek(offset)
            region = stream.read(12 * 512)
    except OSError as exc:
        raise ExfatProbeError(f"cannot read exFAT boot region: {exc}") from exc
    profile = parse_exfat_boot_region(region, volume_size=volume_size)
    return replace(profile, partition_offset=offset)


__all__ = ["ExfatBootProfile", "ExfatProbeError", "exfat_boot_checksum", "parse_exfat_boot_region", "probe_exfat_boot_region"]
