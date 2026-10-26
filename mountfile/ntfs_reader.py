# SPDX-License-Identifier: MIT
"""Conservative, bounded NTFS metadata validation.

This module is deliberately read-only. It validates enough of the boot region,
MFT mirror and FILE-record attribute stream to establish a safe metadata profile,
but it does not expose a filesystem tree and never authorizes writes.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from mountfile.ntfs_mft import NtfsFileRecordHeader, NtfsRecordError, parse_ntfs_file_record_header
from mountfile.ntfs_probe import NtfsBootProfile, NtfsProbeError, parse_ntfs_boot_sector, probe_ntfs_boot_sector

_ATTRIBUTE_END = 0xFFFFFFFF
_MAX_ATTRIBUTES = 4096
_MAX_RUNLIST_BYTES = 1 << 20


class NtfsMetadataError(ValueError):
    """NTFS metadata is malformed, inconsistent, or outside the selected extent."""


@dataclass(frozen=True)
class NtfsAttributeSummary:
    type_code: int
    length: int
    nonresident: bool
    name: str
    lowest_vcn: int | None = None
    highest_vcn: int | None = None
    run_count: int = 0


@dataclass(frozen=True)
class NtfsMetadataProfile:
    boot: NtfsBootProfile
    mft_record: NtfsFileRecordHeader
    mirror_record: NtfsFileRecordHeader
    attributes: tuple[NtfsAttributeSummary, ...]

    @property
    def mirror_matches_mft(self) -> bool:
        return self.mft_record.fixed_record == self.mirror_record.fixed_record


def _read_exact(stream, absolute: int, size: int, image_size: int, end: int) -> bytes:
    if absolute < 0 or size < 0 or absolute + size < absolute or absolute + size > end:
        raise NtfsMetadataError("NTFS metadata read exceeds selected volume bounds")
    if absolute + size > image_size:
        raise NtfsMetadataError("NTFS metadata read exceeds image bounds")
    stream.seek(absolute)
    data = stream.read(size)
    if len(data) != size:
        raise NtfsMetadataError("short NTFS metadata read")
    return data


def _decode_name(raw: bytes, length: int, offset: int) -> str:
    if length == 0:
        return ""
    if offset < 0 or offset + length * 2 > len(raw):
        raise NtfsMetadataError("NTFS attribute name exceeds its record")
    try:
        return raw[offset : offset + length * 2].decode("utf-16le")
    except UnicodeDecodeError as exc:
        raise NtfsMetadataError("NTFS attribute name is not valid UTF-16LE") from exc


def _parse_runlist(raw: bytes, cluster_count: int) -> int:
    """Validate a nonresident runlist and return its extent count."""
    pos = 0
    previous_lcn = 0
    count = 0
    while True:
        if pos >= len(raw) or pos >= _MAX_RUNLIST_BYTES:
            raise NtfsMetadataError("unterminated NTFS runlist")
        header = raw[pos]
        pos += 1
        if header == 0:
            return count
        length_size = header & 0x0F
        offset_size = header >> 4
        if not length_size or not offset_size or length_size > 8 or offset_size > 8:
            raise NtfsMetadataError("invalid NTFS runlist element sizes")
        if pos + length_size + offset_size > len(raw):
            raise NtfsMetadataError("truncated NTFS runlist element")
        run_length = int.from_bytes(raw[pos : pos + length_size], "little")
        pos += length_size
        if run_length == 0:
            raise NtfsMetadataError("NTFS runlist contains a zero-length extent")
        encoded = raw[pos : pos + offset_size]
        pos += offset_size
        if offset_size and encoded[-1] & 0x80:
            delta = int.from_bytes(encoded, "little", signed=False) - (1 << (8 * offset_size))
        else:
            delta = int.from_bytes(encoded, "little", signed=False)
        previous_lcn += delta
        if previous_lcn < 0 or previous_lcn + run_length > cluster_count:
            raise NtfsMetadataError("NTFS runlist points outside the volume")
        count += 1
        if count > _MAX_ATTRIBUTES:
            raise NtfsMetadataError("NTFS runlist has too many extents")


def parse_ntfs_attribute_stream(record: NtfsFileRecordHeader) -> tuple[NtfsAttributeSummary, ...]:
    """Validate all attributes in one USA-restored FILE record."""
    raw = record.fixed_record
    pos = record.first_attribute_offset
    limit = record.bytes_in_use
    result: list[NtfsAttributeSummary] = []
    while pos + 4 <= limit:
        type_code = struct.unpack_from("<I", raw, pos)[0]
        if type_code == _ATTRIBUTE_END:
            if pos + 4 > limit:
                raise NtfsMetadataError("NTFS attribute end marker exceeds record")
            return tuple(result)
        if len(result) >= _MAX_ATTRIBUTES or pos + 16 > limit:
            raise NtfsMetadataError("NTFS attribute header exceeds FILE record")
        length = struct.unpack_from("<I", raw, pos + 4)[0]
        nonresident = bool(raw[pos + 8])
        name_length = raw[pos + 9]
        name_offset = struct.unpack_from("<H", raw, pos + 10)[0]
        if length < 24 or length & 7 or pos + length > limit:
            raise NtfsMetadataError("invalid NTFS attribute length or alignment")
        if raw[pos + 8] not in (0, 1):
            raise NtfsMetadataError("invalid NTFS resident flag")
        name = _decode_name(raw[pos : pos + length], name_length, name_offset)
        lowest_vcn = highest_vcn = None
        run_count = 0
        if nonresident:
            if length < 64:
                raise NtfsMetadataError("NTFS nonresident attribute header is truncated")
            lowest_vcn, highest_vcn = struct.unpack_from("<qQ", raw, pos + 16)
            run_offset = struct.unpack_from("<H", raw, pos + 32)[0]
            if lowest_vcn < 0 or highest_vcn < lowest_vcn:
                raise NtfsMetadataError("invalid NTFS attribute VCN range")
            if run_offset < 64 or run_offset >= length:
                raise NtfsMetadataError("NTFS runlist offset is outside the attribute")
            run_count = _parse_runlist(raw[pos + run_offset : pos + length], 1 << 62)
        else:
            if length < 24:
                raise NtfsMetadataError("NTFS resident attribute header is truncated")
            value_length = struct.unpack_from("<I", raw, pos + 16)[0]
            value_offset = struct.unpack_from("<H", raw, pos + 20)[0]
            if value_offset < 24 or value_offset + value_length > length:
                raise NtfsMetadataError("NTFS resident attribute value exceeds record")
        result.append(NtfsAttributeSummary(type_code, length, nonresident, name, lowest_vcn, highest_vcn, run_count))
        pos += length
    raise NtfsMetadataError("NTFS FILE record has no attribute end marker")


def probe_ntfs_metadata(image_path, *, offset: int = 0, volume_size: int | None = None) -> NtfsMetadataProfile:
    """Validate bounded NTFS boot, backup boot, MFT record 0 and its mirror."""
    try:
        boot = probe_ntfs_boot_sector(image_path, offset=offset, volume_size=volume_size)
        with open(image_path, "rb") as stream:
            image_size = os.fstat(stream.fileno()).st_size
            end = offset + boot.filesystem_size
            primary_boot = _read_exact(stream, offset, 512, image_size, offset + volume_size)
            backup_boot = _read_exact(stream, boot.backup_boot_sector_image_offset, 512, image_size, end)
            backup = parse_ntfs_boot_sector(backup_boot, volume_size=boot.filesystem_size)
            if (backup.bytes_per_sector, backup.sectors_per_cluster, backup.total_sectors, backup.mft_lcn, backup.mft_mirror_lcn) != (boot.bytes_per_sector, boot.sectors_per_cluster, boot.total_sectors, boot.mft_lcn, boot.mft_mirror_lcn):
                raise NtfsMetadataError("NTFS backup boot geometry disagrees with primary boot")
            record_offset = offset + boot.mft_offset
            mirror_offset = offset + boot.mft_mirror_offset
            raw_record = _read_exact(stream, record_offset, boot.file_record_size, image_size, end)
            raw_mirror = _read_exact(stream, mirror_offset, boot.file_record_size, image_size, end)
        mft_record = parse_ntfs_file_record_header(raw_record)
        mirror_record = parse_ntfs_file_record_header(raw_mirror)
        if mft_record.fixed_record != mirror_record.fixed_record:
            raise NtfsMetadataError("NTFS MFT mirror record does not match primary record")
        attributes = parse_ntfs_attribute_stream(mft_record)
        return NtfsMetadataProfile(boot, mft_record, mirror_record, attributes)
    except (OSError, NtfsProbeError, NtfsRecordError) as exc:
        raise NtfsMetadataError(str(exc)) from exc


__all__ = [
    "NtfsAttributeSummary",
    "NtfsMetadataError",
    "NtfsMetadataProfile",
    "parse_ntfs_attribute_stream",
    "probe_ntfs_metadata",
]
