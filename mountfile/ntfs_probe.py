# SPDX-License-Identifier: MIT
"""Conservative, read-only NTFS boot-sector geometry probe.

A successful result validates only the primary 512-byte NTFS boot sector and
its declared geometry. It does not inspect $MFT records, $Volume state, $LogFile,
directory indexes, attributes, or recovery state, and is not an RW eligibility
signal.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, replace

_BOOT_SECTOR_SIZE = 512
_NTFS_OEM_ID = b"NTFS    "
_BOOT_SIGNATURE = b"\x55\xaa"
_ALLOWED_BYTES_PER_SECTOR = {512, 1024, 2048, 4096}
_MAX_CLUSTER_SIZE = 2 * 1024 * 1024
_MAX_RECORD_SIZE = 1024 * 1024


class NtfsProbeError(ValueError):
    """The NTFS boot sector or declared geometry is invalid or unsupported."""


@dataclass(frozen=True)
class NtfsBootProfile:
    """Validated NTFS boot-sector geometry, not a complete volume profile."""

    partition_offset: int
    partition_size: int
    filesystem_size: int
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    total_sectors: int
    cluster_count: int
    mft_lcn: int
    mft_mirror_lcn: int
    mft_offset: int
    mft_mirror_offset: int
    file_record_size: int
    index_record_size: int
    volume_serial: int
    boot_checksum: int
    backup_boot_sector_offset: int

    @property
    def backup_boot_sector_image_offset(self) -> int:
        """Absolute image offset of the declared volume's final sector."""
        return self.partition_offset + self.backup_boot_sector_offset


def _decode_record_size(
    code: int, cluster_size: int, bytes_per_sector: int, name: str
) -> int:
    if code == 0:
        raise NtfsProbeError(f"NTFS {name} size code is zero")
    if code > 0:
        size = code * cluster_size
    else:
        shift = -code
        if shift > 20:
            raise NtfsProbeError(f"NTFS {name} size exceeds the 1 MiB probe limit")
        size = 1 << shift
    if size < bytes_per_sector or size > _MAX_RECORD_SIZE or size & (size - 1):
        raise NtfsProbeError(f"NTFS {name} size is outside the supported range")
    return size


def parse_ntfs_boot_sector(boot_sector, *, volume_size: int) -> NtfsBootProfile:
    """Parse a primary NTFS boot sector within an explicit containing extent."""
    if not isinstance(volume_size, int) or isinstance(volume_size, bool):
        raise NtfsProbeError("an explicit integer volume_size is required")
    if volume_size <= 0:
        raise NtfsProbeError("volume_size must be positive")
    try:
        view = memoryview(boot_sector)
    except TypeError as exc:
        raise NtfsProbeError("boot sector must be bytes-like") from exc
    if len(view) < _BOOT_SECTOR_SIZE:
        raise NtfsProbeError("truncated NTFS boot sector")
    sector = bytes(view[:_BOOT_SECTOR_SIZE])

    if sector[3:11] != _NTFS_OEM_ID:
        raise NtfsProbeError("NTFS OEM identifier is missing")
    if sector[510:512] != _BOOT_SIGNATURE:
        raise NtfsProbeError("NTFS boot signature is invalid")

    bytes_per_sector = struct.unpack_from("<H", sector, 0x0B)[0]
    sectors_per_cluster = sector[0x0D]
    if bytes_per_sector not in _ALLOWED_BYTES_PER_SECTOR:
        raise NtfsProbeError("unsupported NTFS bytes-per-sector value")
    if sectors_per_cluster == 0 or sectors_per_cluster & (sectors_per_cluster - 1):
        raise NtfsProbeError("NTFS sectors-per-cluster must be a power of two")
    cluster_size = bytes_per_sector * sectors_per_cluster
    if cluster_size < bytes_per_sector or cluster_size > _MAX_CLUSTER_SIZE:
        raise NtfsProbeError("NTFS cluster size is outside the supported range")

    total_sectors = struct.unpack_from("<Q", sector, 0x28)[0]
    if total_sectors == 0:
        raise NtfsProbeError("NTFS volume has zero total sectors")
    filesystem_size = total_sectors * bytes_per_sector
    if filesystem_size > volume_size:
        raise NtfsProbeError("NTFS declared size exceeds the selected volume bounds")
    if filesystem_size < 2 * bytes_per_sector:
        raise NtfsProbeError(
            "NTFS volume is too small for primary and backup boot sectors"
        )

    cluster_count = total_sectors // sectors_per_cluster
    if cluster_count == 0:
        raise NtfsProbeError("NTFS volume contains no complete clusters")
    mft_lcn = struct.unpack_from("<Q", sector, 0x30)[0]
    mft_mirror_lcn = struct.unpack_from("<Q", sector, 0x38)[0]
    if mft_lcn >= cluster_count or mft_mirror_lcn >= cluster_count:
        raise NtfsProbeError("NTFS MFT location lies outside the declared volume")

    file_record_code = struct.unpack_from("<b", sector, 0x40)[0]
    index_record_code = struct.unpack_from("<b", sector, 0x44)[0]
    file_record_size = _decode_record_size(
        file_record_code, cluster_size, bytes_per_sector, "MFT record"
    )
    index_record_size = _decode_record_size(
        index_record_code, cluster_size, bytes_per_sector, "index record"
    )
    mft_offset = mft_lcn * cluster_size
    mft_mirror_offset = mft_mirror_lcn * cluster_size
    if mft_offset + file_record_size > filesystem_size:
        raise NtfsProbeError("first NTFS MFT record exceeds the declared volume")
    if mft_mirror_offset + file_record_size > filesystem_size:
        raise NtfsProbeError("first NTFS MFT mirror record exceeds the declared volume")

    return NtfsBootProfile(
        partition_offset=0,
        partition_size=volume_size,
        filesystem_size=filesystem_size,
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        cluster_size=cluster_size,
        total_sectors=total_sectors,
        cluster_count=cluster_count,
        mft_lcn=mft_lcn,
        mft_mirror_lcn=mft_mirror_lcn,
        mft_offset=mft_offset,
        mft_mirror_offset=mft_mirror_offset,
        file_record_size=file_record_size,
        index_record_size=index_record_size,
        volume_serial=struct.unpack_from("<Q", sector, 0x48)[0],
        boot_checksum=struct.unpack_from("<I", sector, 0x50)[0],
        backup_boot_sector_offset=filesystem_size - bytes_per_sector,
    )


def probe_ntfs_boot_sector(
    image_path: str | os.PathLike[str],
    *,
    offset: int = 0,
    volume_size: int | None = None,
) -> NtfsBootProfile:
    """Read and validate the primary NTFS boot sector within exact outer bounds."""
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise NtfsProbeError("offset must be an integer")
    if offset < 0:
        raise NtfsProbeError("offset must not be negative")
    if not isinstance(volume_size, int) or isinstance(volume_size, bool):
        raise NtfsProbeError("an explicit integer volume_size is required")
    if volume_size <= 0:
        raise NtfsProbeError("volume_size must be positive")
    try:
        with open(image_path, "rb") as stream:
            image_size = os.fstat(stream.fileno()).st_size
            if offset + volume_size > image_size:
                raise NtfsProbeError("selected volume extent exceeds image bounds")
            if volume_size < _BOOT_SECTOR_SIZE:
                raise NtfsProbeError(
                    "selected volume is smaller than an NTFS boot sector"
                )
            stream.seek(offset)
            boot_sector = stream.read(_BOOT_SECTOR_SIZE)
    except OSError as exc:
        raise NtfsProbeError(f"cannot read NTFS boot sector: {exc}") from exc

    profile = parse_ntfs_boot_sector(boot_sector, volume_size=volume_size)
    return replace(profile, partition_offset=offset)


__all__ = [
    "NtfsBootProfile",
    "NtfsProbeError",
    "parse_ntfs_boot_sector",
    "probe_ntfs_boot_sector",
]
