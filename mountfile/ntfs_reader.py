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
from pathlib import Path

from mountfile.ntfs_mft import (
    NtfsFileRecordHeader,
    NtfsRecordError,
    decode_ntfs_file_reference,
    parse_ntfs_file_record_header,
    validate_ntfs_file_reference,
)
from mountfile.ntfs_probe import (
    NtfsBootProfile,
    NtfsProbeError,
    parse_ntfs_boot_sector,
    probe_ntfs_boot_sector,
)

_ATTRIBUTE_END = 0xFFFFFFFF
_ATTRIBUTE_LIST = 0x20
_MAX_ATTRIBUTES = 4096
_MAX_RUNLIST_BYTES = 1 << 20
_ATTRIBUTE_FLAG_COMPRESSED = 0x0001
_ATTRIBUTE_FLAG_ENCRYPTED = 0x4000
_ATTRIBUTE_FLAG_SPARSE = 0x8000


class NtfsMetadataError(ValueError):
    """NTFS metadata is malformed, inconsistent, or outside the selected extent."""


@dataclass(frozen=True)
class NtfsRun:
    """One validated nonresident NTFS run; ``lcn`` is None for sparse data."""

    vcn: int
    lcn: int | None
    length: int


@dataclass(frozen=True)
class NtfsIndexEntry:
    reference: int
    name: str
    is_directory: bool


@dataclass(frozen=True)
class NtfsVolumeState:
    flags: int

    @property
    def clean(self) -> bool:
        return self.flags == 0


@dataclass(frozen=True)
class NtfsAttributeSummary:
    type_code: int
    length: int
    nonresident: bool
    name: str
    lowest_vcn: int | None = None
    highest_vcn: int | None = None
    run_count: int = 0
    runs: tuple[NtfsRun, ...] = ()
    resident_value: bytes | None = None
    data_size: int = 0


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


def _parse_runlist(
    raw: bytes,
    cluster_count: int,
    *,
    lowest_vcn: int = 0,
    highest_vcn: int | None = None,
) -> tuple[NtfsRun, ...]:
    """Decode and validate a nonresident runlist within the volume."""
    pos = 0
    previous_lcn = 0
    vcn = lowest_vcn
    if vcn < 0 or (highest_vcn is not None and highest_vcn < vcn):
        raise NtfsMetadataError("invalid NTFS runlist VCN range")
    result: list[NtfsRun] = []
    count = 0
    while True:
        if pos >= len(raw) or pos >= _MAX_RUNLIST_BYTES:
            raise NtfsMetadataError("unterminated NTFS runlist")
        header = raw[pos]
        pos += 1
        if header == 0:
            if not result:
                raise NtfsMetadataError("NTFS runlist has no extents")
            if highest_vcn is not None and vcn != highest_vcn + 1:
                raise NtfsMetadataError(
                    "NTFS runlist does not cover its declared VCN range"
                )
            return tuple(result)
        length_size = header & 0x0F
        offset_size = header >> 4
        if not length_size or length_size > 8 or offset_size > 8:
            raise NtfsMetadataError("invalid NTFS runlist element sizes")
        if pos + length_size + offset_size > len(raw):
            raise NtfsMetadataError("truncated NTFS runlist element")
        run_length = int.from_bytes(raw[pos : pos + length_size], "little")
        pos += length_size
        if run_length == 0:
            raise NtfsMetadataError("NTFS runlist contains a zero-length extent")
        encoded = raw[pos : pos + offset_size]
        pos += offset_size
        if offset_size == 0:
            raise NtfsMetadataError("sparse NTFS run is unsupported")
        if encoded[-1] & 0x80:
            delta = int.from_bytes(encoded, "little", signed=False) - (
                1 << (8 * offset_size)
            )
            previous_lcn += delta
            lcn = previous_lcn
        else:
            delta = int.from_bytes(encoded, "little", signed=False)
            previous_lcn += delta
            lcn = previous_lcn
        if lcn is not None and (lcn <= 0 or lcn + run_length > cluster_count):
            raise NtfsMetadataError("NTFS runlist points outside the volume")
        if vcn + run_length < vcn or (
            highest_vcn is not None and vcn + run_length > highest_vcn + 1
        ):
            raise NtfsMetadataError("NTFS runlist VCN range overflows")
        result.append(NtfsRun(vcn, lcn, run_length))
        vcn += run_length
        count += 1
        if count > _MAX_ATTRIBUTES:
            raise NtfsMetadataError("NTFS runlist has too many extents")


def parse_ntfs_attribute_stream(
    record: NtfsFileRecordHeader, *, cluster_count: int | None = None
) -> tuple[NtfsAttributeSummary, ...]:
    """Validate all attributes in one USA-restored FILE record."""
    raw = record.fixed_record
    if cluster_count is None:
        cluster_count = 1 << 62
    if cluster_count <= 0:
        raise NtfsMetadataError("NTFS cluster count must be positive")
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
        if type_code == _ATTRIBUTE_LIST:
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST streams are unsupported")
        name = _decode_name(raw[pos : pos + length], name_length, name_offset)
        lowest_vcn = highest_vcn = None
        run_count = 0
        runs: tuple[NtfsRun, ...] = ()
        resident_value = None
        data_size = 0
        flags = struct.unpack_from("<H", raw, pos + 12)[0]
        if nonresident:
            if length < 64:
                raise NtfsMetadataError(
                    "NTFS nonresident attribute header is truncated"
                )
            lowest_vcn, highest_vcn = struct.unpack_from("<qQ", raw, pos + 16)
            run_offset = struct.unpack_from("<H", raw, pos + 32)[0]
            if lowest_vcn < 0 or highest_vcn < lowest_vcn:
                raise NtfsMetadataError("invalid NTFS attribute VCN range")
            if run_offset < 64 or run_offset >= length:
                raise NtfsMetadataError("NTFS runlist offset is outside the attribute")
            if flags & (
                _ATTRIBUTE_FLAG_COMPRESSED
                | _ATTRIBUTE_FLAG_ENCRYPTED
                | _ATTRIBUTE_FLAG_SPARSE
            ):
                raise NtfsMetadataError(
                    "compressed, encrypted or sparse NTFS attributes are unsupported"
                )
            runs = _parse_runlist(
                raw[pos + run_offset : pos + length],
                cluster_count,
                lowest_vcn=lowest_vcn,
                highest_vcn=highest_vcn,
            )
            run_count = len(runs)
            data_size = struct.unpack_from("<Q", raw, pos + 48)[0]
            if data_size > (1 << 63):
                raise NtfsMetadataError("NTFS nonresident data size overflows")
        else:
            if length < 24:
                raise NtfsMetadataError("NTFS resident attribute header is truncated")
            value_length = struct.unpack_from("<I", raw, pos + 16)[0]
            value_offset = struct.unpack_from("<H", raw, pos + 20)[0]
            if value_offset < 24 or value_offset + value_length > length:
                raise NtfsMetadataError("NTFS resident attribute value exceeds record")
            resident_value = bytes(
                raw[pos + value_offset : pos + value_offset + value_length]
            )
        result.append(
            NtfsAttributeSummary(
                type_code,
                length,
                nonresident,
                name,
                lowest_vcn,
                highest_vcn,
                run_count,
                runs,
                resident_value,
                data_size,
            )
        )
        pos += length
    raise NtfsMetadataError("NTFS FILE record has no attribute end marker")


def parse_ntfs_index_root(
    value: bytes,
    *,
    parent_reference: int | None = None,
    allow_index_allocation: bool = False,
) -> tuple[NtfsIndexEntry, ...]:
    """Parse a resident INDEX_ROOT value containing FILE_NAME keys."""
    if len(value) < 32:
        raise NtfsMetadataError("NTFS INDEX_ROOT value is truncated")
    entry_offset, total_size, allocated_size = struct.unpack_from("<III", value, 16)
    index_flags = struct.unpack_from("<I", value, 28)[0]
    if index_flags & ~1:
        raise NtfsMetadataError("unsupported NTFS INDEX_ROOT flags")
    if index_flags & 1 and not allow_index_allocation:
        raise NtfsMetadataError("NTFS directory requires INDEX_ALLOCATION traversal")
    if allocated_size < total_size or entry_offset < 16:
        raise NtfsMetadataError("invalid NTFS INDEX_ROOT header bounds")
    start = 16 + entry_offset
    end = 16 + total_size
    if start < 16 or end > len(value) or start > end:
        raise NtfsMetadataError("NTFS INDEX_ROOT entries exceed the attribute")
    result: list[NtfsIndexEntry] = []
    previous_name = ""
    names: set[str] = set()
    pos = start
    while pos < end:
        if pos + 16 > end:
            raise NtfsMetadataError("truncated NTFS index entry header")
        reference = struct.unpack_from("<Q", value, pos)[0]
        entry_length, key_length = struct.unpack_from("<HH", value, pos + 8)
        flags = struct.unpack_from("<I", value, pos + 12)[0]
        if entry_length < 16 or entry_length & 7 or pos + entry_length > end:
            raise NtfsMetadataError("invalid NTFS index entry length")
        if flags & ~0x00000003:
            raise NtfsMetadataError("unsupported NTFS index entry flags")
        if flags & 0x00000002:
            if key_length:
                raise NtfsMetadataError("NTFS index end entry contains a key")
            if pos + entry_length != end:
                raise NtfsMetadataError("NTFS index end entry is not terminal")
            return tuple(result)
        if flags & 0x00000001:
            raise NtfsMetadataError(
                "NTFS directory requires INDEX_ALLOCATION traversal"
            )
        try:
            decode_ntfs_file_reference(reference)
        except NtfsRecordError as exc:
            raise NtfsMetadataError(str(exc)) from exc
        if key_length < 66 or 16 + key_length > entry_length:
            raise NtfsMetadataError("NTFS FILE_NAME key is truncated")
        key = value[pos + 16 : pos + 16 + key_length]
        parent = struct.unpack_from("<Q", key, 0)[0]
        try:
            decode_ntfs_file_reference(parent)
        except NtfsRecordError as exc:
            raise NtfsMetadataError("invalid NTFS FILE_NAME parent reference") from exc
        if parent_reference is not None and parent != parent_reference:
            raise NtfsMetadataError("NTFS FILE_NAME parent reference mismatch")
        name_length = key[0x40]
        name_end = 0x42 + name_length * 2
        if name_end > len(key):
            raise NtfsMetadataError("NTFS FILE_NAME name exceeds index entry")
        if name_length == 0 or key[0x41] > 3:
            raise NtfsMetadataError("invalid NTFS FILE_NAME namespace")
        try:
            name = key[0x42:name_end].decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise NtfsMetadataError("NTFS FILE_NAME is not valid UTF-16LE") from exc
        if (
            not name.isascii()
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise NtfsMetadataError(
                "NTFS index name is outside the native ASCII profile"
            )
        folded = name.lower()
        if folded in names or (previous_name and folded < previous_name):
            raise NtfsMetadataError("NTFS index names are duplicate or unsorted")
        names.add(folded)
        previous_name = folded
        result.append(NtfsIndexEntry(reference, name, bool(key[0x38] & 0x10000000)))
        pos += entry_length
    raise NtfsMetadataError("NTFS INDEX_ROOT has no end entry")


def parse_ntfs_index_block(
    raw_block: bytes, *, parent_reference: int | None = None
) -> tuple[NtfsIndexEntry, ...]:
    """Validate an INDX block's USA and parse its bounded index entries."""
    if len(raw_block) < 512 or len(raw_block) % 512:
        raise NtfsMetadataError("NTFS INDX block size is invalid")
    if raw_block[:4] != b"INDX":
        raise NtfsMetadataError("NTFS INDX signature is invalid")
    fixup_offset, fixup_count = struct.unpack_from("<HH", raw_block, 4)
    if fixup_offset not in (0x28, 0x2A) or fixup_count != len(raw_block) // 512 + 1:
        raise NtfsMetadataError("NTFS INDX USA geometry is invalid")
    if fixup_offset + fixup_count * 2 > 512:
        raise NtfsMetadataError("NTFS INDX USA exceeds its first sector")
    sequence = struct.unpack_from("<H", raw_block, fixup_offset)[0]
    fixed = bytearray(raw_block)
    for index in range(len(raw_block) // 512):
        trailer = (index + 1) * 512 - 2
        if struct.unpack_from("<H", raw_block, trailer)[0] != sequence:
            raise NtfsMetadataError("NTFS INDX USA mismatch")
        replacement = struct.unpack_from("<H", raw_block, fixup_offset + 2 + index * 2)[
            0
        ]
        struct.pack_into("<H", fixed, trailer, replacement)
    return parse_ntfs_index_root(bytes(fixed[8:]), parent_reference=parent_reference)


def parse_ntfs_volume_state(
    attributes: tuple[NtfsAttributeSummary, ...],
) -> NtfsVolumeState:
    """Parse resident $VOLUME_INFORMATION and reject missing state metadata."""
    matches = [a for a in attributes if a.type_code == 0x70 and not a.name]
    if len(matches) != 1:
        raise NtfsMetadataError("NTFS $Volume has ambiguous volume information")
    volume = matches[0]
    if volume is None or volume.nonresident or volume.resident_value is None:
        raise NtfsMetadataError("NTFS $Volume has no resident volume information")
    if len(volume.resident_value) != 12:
        raise NtfsMetadataError("NTFS $Volume information has an invalid length")
    major, minor = volume.resident_value[8:10]
    if (major, minor) not in ((1, 1), (3, 0), (3, 1)):
        raise NtfsMetadataError("unsupported NTFS $Volume version")
    return NtfsVolumeState(struct.unpack_from("<H", volume.resident_value, 10)[0])


def _validate_nonresident_data_stream(
    attribute: NtfsAttributeSummary, cluster_size: int, *, name: str
) -> None:
    if not attribute.nonresident or attribute.lowest_vcn != 0 or not attribute.runs:
        raise NtfsMetadataError(f"{name} has no complete nonresident DATA stream")
    capacity = sum(run.length for run in attribute.runs) * cluster_size
    if attribute.data_size <= 0 or attribute.data_size > capacity:
        raise NtfsMetadataError(f"{name} DATA size exceeds its allocated runlist")


def probe_ntfs_metadata(
    image_path, *, offset: int = 0, volume_size: int | None = None
) -> NtfsMetadataProfile:
    """Validate bounded NTFS boot, backup boot, MFT record 0 and its mirror."""
    try:
        boot = probe_ntfs_boot_sector(
            image_path, offset=offset, volume_size=volume_size
        )
        with open(image_path, "rb") as stream:
            image_size = os.fstat(stream.fileno()).st_size
            end = offset + boot.filesystem_size
            _read_exact(stream, offset, 512, image_size, offset + volume_size)
            backup_boot = _read_exact(
                stream, boot.backup_boot_sector_image_offset, 512, image_size, end
            )
            backup = parse_ntfs_boot_sector(
                backup_boot, volume_size=boot.filesystem_size
            )
            primary_identity = (
                boot.filesystem_size,
                boot.bytes_per_sector,
                boot.sectors_per_cluster,
                boot.cluster_size,
                boot.total_sectors,
                boot.cluster_count,
                boot.mft_lcn,
                boot.mft_mirror_lcn,
                boot.mft_offset,
                boot.mft_mirror_offset,
                boot.file_record_size,
                boot.index_record_size,
                boot.volume_serial,
                boot.boot_checksum,
            )
            backup_identity = (
                backup.filesystem_size,
                backup.bytes_per_sector,
                backup.sectors_per_cluster,
                backup.cluster_size,
                backup.total_sectors,
                backup.cluster_count,
                backup.mft_lcn,
                backup.mft_mirror_lcn,
                backup.mft_offset,
                backup.mft_mirror_offset,
                backup.file_record_size,
                backup.index_record_size,
                backup.volume_serial,
                backup.boot_checksum,
            )
            if backup_identity != primary_identity:
                raise NtfsMetadataError(
                    "NTFS backup boot profile disagrees with primary boot"
                )
            record_offset = offset + boot.mft_offset
            mirror_offset = offset + boot.mft_mirror_offset
            raw_record = _read_exact(
                stream, record_offset, boot.file_record_size, image_size, end
            )
            raw_mirror = _read_exact(
                stream, mirror_offset, boot.file_record_size, image_size, end
            )
        mft_record = parse_ntfs_file_record_header(raw_record)
        mirror_record = parse_ntfs_file_record_header(raw_mirror)
        if mft_record.fixed_record != mirror_record.fixed_record:
            raise NtfsMetadataError(
                "NTFS MFT mirror record does not match primary record"
            )
        attributes = parse_ntfs_attribute_stream(
            mft_record, cluster_count=boot.cluster_count
        )
        return NtfsMetadataProfile(boot, mft_record, mirror_record, attributes)
    except (OSError, NtfsProbeError, NtfsRecordError) as exc:
        raise NtfsMetadataError(str(exc)) from exc


__all__ = [
    "NtfsAttributeSummary",
    "NtfsIndexEntry",
    "NtfsMetadataError",
    "NtfsVolumeState",
    "NtfsMetadataProfile",
    "NtfsRun",
    "parse_ntfs_attribute_stream",
    "parse_ntfs_index_root",
    "parse_ntfs_index_block",
    "parse_ntfs_volume_state",
    "probe_ntfs_metadata",
]


class NativeNtfsMetadataImage:
    """Read-only access to validated unnamed DATA of MFT record 0 only."""

    backend_name = "native-ntfs-metadata-ro"

    def __init__(self, image_path, offset=0, volume_size=None):
        if volume_size is None:
            raise NtfsMetadataError("an explicit NTFS volume_size is required")
        self.image_path = str(image_path)
        self.offset = int(offset)
        self._stream = Path(self.image_path).open("rb")  # noqa: SIM115
        self._image_size = os.fstat(self._stream.fileno()).st_size
        self.profile = probe_ntfs_metadata(
            self.image_path, offset=self.offset, volume_size=volume_size
        )
        self._end = self.offset + self.profile.boot.filesystem_size
        self._mft_data = next(
            (a for a in self.profile.attributes if a.type_code == 0x80 and not a.name),
            None,
        )
        if self._mft_data is None:
            self.close()
            raise NtfsMetadataError("MFT record 0 has no unnamed DATA stream")
        _validate_nonresident_data_stream(
            self._mft_data, self.profile.boot.cluster_size, name="MFT"
        )
        volume_record = self.read_mft_record(3)
        volume_state = parse_ntfs_volume_state(
            parse_ntfs_attribute_stream(
                volume_record, cluster_count=self.profile.boot.cluster_count
            )
        )
        if not volume_state.clean:
            self.close()
            raise NtfsMetadataError("NTFS volume is dirty or requires repair")
        self.close()
        raise NtfsMetadataError(
            "NTFS $LogFile restart-area validation is not implemented; native browsing is disabled"
        )

    def close(self):
        self._stream.close()

    def reopen(self):
        if self._stream.closed:
            self._stream = Path(self.image_path).open("rb")  # noqa: SIM115

    def list_dir(self, path="/"):
        record_number, record = self._resolve(path)
        if not record.is_directory:
            raise NtfsMetadataError("NTFS path is not a directory")
        entries = self.list_dir_by_record(record_number, record)
        result = []
        for entry in entries:
            child_number = entry.reference & ((1 << 48) - 1)
            child = self.read_mft_record(child_number, reference=entry.reference)
            if child.is_directory != entry.is_directory:
                raise NtfsMetadataError("NTFS index directory flag disagrees with MFT")
            child_attrs = parse_ntfs_attribute_stream(
                child, cluster_count=self.profile.boot.cluster_count
            )
            data = next(
                (a for a in child_attrs if a.type_code == 0x80 and not a.name), None
            )
            result.append(
                {
                    "name": entry.name,
                    "is_dir": child.is_directory,
                    "size": (
                        0
                        if child.is_directory or data is None
                        else (
                            len(data.resident_value)
                            if data.resident_value is not None
                            else data.data_size
                        )
                    ),
                    "date": "",
                    "inode": child_number,
                }
            )
        result.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return result

    def file_size(self, path):
        if path == "/$MFT":
            return self._mft_data.data_size
        _number, record = self._resolve(path)
        if record.is_directory:
            return 0
        data = next(
            (
                a
                for a in parse_ntfs_attribute_stream(
                    record, cluster_count=self.profile.boot.cluster_count
                )
                if a.type_code == 0x80 and not a.name
            ),
            None,
        )
        if data is None:
            raise NtfsMetadataError("NTFS file has no unnamed DATA attribute")
        return (
            len(data.resident_value)
            if data.resident_value is not None
            else data.data_size
        )

    def read_file_range(self, path, offset=0, length=1 << 20):
        if not isinstance(offset, int) or not isinstance(length, int):
            raise NtfsMetadataError("invalid NTFS metadata read request")
        if path == "/$MFT":
            data = self._mft_data
            if offset < 0 or length < 0 or offset >= data.data_size or not length:
                return b""
            length = min(length, data.data_size - offset, 1 << 20)
            return self._read_runs(data.runs, offset, length)
        _number, record = self._resolve(path)
        if record.is_directory:
            raise NtfsMetadataError("cannot read directory content as a file")
        data = next(
            (
                a
                for a in parse_ntfs_attribute_stream(
                    record, cluster_count=self.profile.boot.cluster_count
                )
                if a.type_code == 0x80 and not a.name
            ),
            None,
        )
        if data is None:
            raise NtfsMetadataError("NTFS file has no unnamed DATA attribute")
        if data.resident_value is not None:
            if (
                offset < 0
                or length < 0
                or offset >= len(data.resident_value)
                or not length
            ):
                return b""
            return data.resident_value[offset : offset + min(length, 1 << 20)]
        if offset < 0 or length < 0 or offset >= data.data_size or not length:
            return b""
        length = min(length, data.data_size - offset, 1 << 20)
        return self._read_runs(data.runs, offset, length)

    def _read_runs(self, runs, offset, length):
        output = bytearray()
        wanted_end = offset + length
        for run in runs:
            start = run.vcn * self.profile.boot.cluster_size
            end = start + run.length * self.profile.boot.cluster_size
            if offset >= end or wanted_end <= start:
                continue
            take_start = max(offset, start)
            take_end = min(wanted_end, end)
            absolute = (
                self.offset
                + run.lcn * self.profile.boot.cluster_size
                + take_start
                - start
            )
            output.extend(
                _read_exact(
                    self._stream,
                    absolute,
                    take_end - take_start,
                    self._image_size,
                    self._end,
                )
            )
        if len(output) != length:
            raise NtfsMetadataError("NTFS DATA runlist does not cover requested range")
        return bytes(output)

    def _resolve(self, path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise NtfsMetadataError("NTFS path must be absolute")
        parts = [part for part in path.split("/") if part]
        if any(
            not part.isascii() or part in (".", "..") or "\\" in part or "\x00" in part
            for part in parts
        ):
            raise NtfsMetadataError("NTFS path is outside the native ASCII profile")
        number = 5
        record = self.read_mft_record(number)
        for part in parts:
            match = next(
                (
                    item
                    for item in self.list_dir_by_record(number, record)
                    if item.name.lower() == part.lower()
                ),
                None,
            )
            if match is None:
                raise NtfsMetadataError(f"NTFS path component not found: {part}")
            number = match.reference & ((1 << 48) - 1)
            record = self.read_mft_record(number, reference=match.reference)
        return number, record

    def list_dir_by_record(self, record_number, record):
        attrs = parse_ntfs_attribute_stream(
            record, cluster_count=self.profile.boot.cluster_count
        )
        roots = [a for a in attrs if a.type_code == 0x90 and a.name == "$I30"]
        if len(roots) != 1 or roots[0].nonresident or roots[0].resident_value is None:
            raise NtfsMetadataError(
                "NTFS directory has no unique resident $I30 INDEX_ROOT"
            )
        root = roots[0]
        root_value = root.resident_value
        entries = parse_ntfs_index_root(
            root_value,
            parent_reference=record_number | (record.sequence_number << 48),
        )
        unique = {}
        for entry in entries:
            unique[(entry.name.lower(), entry.reference)] = entry
        return tuple(sorted(unique.values(), key=lambda item: item.name.lower()))

    def read_file(self, path):
        return self.read_file_range(path, 0, self.file_size(path))

    def read_mft_record(self, record_number, *, reference=None):
        """Load one MFT record only after checked arithmetic and bounds checks."""
        if not isinstance(record_number, int) or isinstance(record_number, bool):
            raise NtfsMetadataError("MFT record number must be an integer")
        if record_number < 0 or record_number >= (1 << 48):
            raise NtfsMetadataError("MFT record number is outside NTFS bounds")
        record_size = self.profile.boot.file_record_size
        start = record_number * record_size
        if record_number and start // record_size != record_number:
            raise NtfsMetadataError("MFT record offset overflows")
        raw = self.read_file_range("/$MFT", start, record_size)
        if len(raw) != record_size:
            raise NtfsMetadataError("MFT record is outside the DATA runlist")
        try:
            record = parse_ntfs_file_record_header(raw)
            if reference is not None:
                referenced_number = validate_ntfs_file_reference(reference, record)
                if referenced_number != record_number:
                    raise NtfsMetadataError("MFT reference record number mismatch")
            return record
        except NtfsRecordError as exc:
            raise NtfsMetadataError(str(exc)) from exc


__all__.append("NativeNtfsMetadataImage")
