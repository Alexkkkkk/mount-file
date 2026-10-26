# SPDX-License-Identifier: MIT
"""Bounded parsing of in-use NTFS FILE-record headers and USA fixups.

This module does not parse attributes or provide a filesystem reader. A record
is returned only after every protected 512-byte sector trailer has been
validated; invalid records produce no partially restored output.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_FILE_SIGNATURE = b"FILE"
_FIXUP_SECTOR_SIZE = 512
_MIN_RECORD_SIZE = 512
_MAX_RECORD_SIZE = 1024 * 1024
_MIN_FIXUP_OFFSET = 0x2A
_KNOWN_RECORD_FLAGS = 0x000F
_FLAG_IN_USE = 0x0001
_FLAG_DIRECTORY = 0x0002


class NtfsRecordError(ValueError):
    """An NTFS FILE record is malformed, inconsistent, or unsupported."""


@dataclass(frozen=True)
class NtfsFileRecordHeader:
    """Header fields and USA-restored bytes for one validated FILE record."""

    fixed_record: bytes
    sequence_number: int
    hard_link_count: int
    first_attribute_offset: int
    flags: int
    bytes_in_use: int
    bytes_allocated: int
    base_file_reference: int
    next_attribute_id: int
    update_sequence_number: int
    update_sequence_offset: int
    update_sequence_count: int

    @property
    def record_size(self) -> int:
        return len(self.fixed_record)

    @property
    def in_use(self) -> bool:
        return bool(self.flags & _FLAG_IN_USE)

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & _FLAG_DIRECTORY)


def parse_ntfs_file_record_header(raw_record) -> NtfsFileRecordHeader:
    """Validate and restore the USA of one in-use NTFS FILE record.

    USA trailer protection is evaluated at fixed 512-byte strides, matching the
    NTFS3 implementation. Only the record header and sector fixups are parsed;
    attribute contents remain opaque.
    """
    try:
        view = memoryview(raw_record)
    except TypeError as exc:
        raise NtfsRecordError("FILE record must be bytes-like") from exc
    record_size = view.nbytes
    if (
        record_size < _MIN_RECORD_SIZE
        or record_size > _MAX_RECORD_SIZE
        or record_size & (record_size - 1)
        or record_size % _FIXUP_SECTOR_SIZE
    ):
        raise NtfsRecordError("FILE record size is outside the supported range")
    try:
        record = bytes(view)
    except (TypeError, ValueError) as exc:
        raise NtfsRecordError("FILE record buffer is not readable") from exc
    if record[:4] != _FILE_SIGNATURE:
        raise NtfsRecordError("NTFS FILE record signature is invalid")

    fixup_offset, fixup_count = struct.unpack_from("<HH", record, 0x04)
    expected_count = record_size // _FIXUP_SECTOR_SIZE + 1
    if fixup_offset not in (0x2A, 0x30):
        raise NtfsRecordError("NTFS USA offset is not a supported FILE record layout")
    if fixup_count != expected_count:
        raise NtfsRecordError("NTFS USA count does not match FILE record sectors")
    fixup_end = fixup_offset + fixup_count * 2
    if fixup_end > _FIXUP_SECTOR_SIZE:
        raise NtfsRecordError("NTFS USA array exceeds its first 512-byte sector")

    sequence_number = struct.unpack_from("<H", record, 0x10)[0]
    hard_link_count = struct.unpack_from("<H", record, 0x12)[0]
    first_attribute_offset = struct.unpack_from("<H", record, 0x14)[0]
    flags = struct.unpack_from("<H", record, 0x16)[0]
    bytes_in_use = struct.unpack_from("<I", record, 0x18)[0]
    bytes_allocated = struct.unpack_from("<I", record, 0x1C)[0]
    base_file_reference = struct.unpack_from("<Q", record, 0x20)[0]
    next_attribute_id = struct.unpack_from("<H", record, 0x28)[0]

    if sequence_number == 0:
        raise NtfsRecordError("in-use FILE record has a zero sequence number")
    if not flags & _FLAG_IN_USE:
        raise NtfsRecordError("FILE record is not marked in use")
    if flags & ~_KNOWN_RECORD_FLAGS:
        raise NtfsRecordError("FILE record contains unsupported flag bits")
    if bytes_allocated != record_size:
        raise NtfsRecordError("FILE record allocated size does not match its buffer")
    if bytes_in_use < first_attribute_offset + 4 or bytes_in_use > bytes_allocated:
        raise NtfsRecordError("FILE record used-size/attribute bounds are invalid")
    if (
        first_attribute_offset & 7
        or first_attribute_offset < fixup_end
        or first_attribute_offset < _MIN_FIXUP_OFFSET
    ):
        raise NtfsRecordError("FILE record first-attribute offset is invalid")

    update_sequence_number = struct.unpack_from("<H", record, fixup_offset)[0]
    sector_count = record_size // _FIXUP_SECTOR_SIZE
    replacements = []
    for sector_index in range(sector_count):
        trailer_offset = (sector_index + 1) * _FIXUP_SECTOR_SIZE - 2
        trailer = struct.unpack_from("<H", record, trailer_offset)[0]
        if trailer != update_sequence_number:
            raise NtfsRecordError(
                f"FILE record USA mismatch at protected sector {sector_index}"
            )
        replacement = struct.unpack_from(
            "<H", record, fixup_offset + 2 * (sector_index + 1)
        )[0]
        replacements.append((trailer_offset, replacement))

    fixed_record = bytearray(record)
    for trailer_offset, replacement in replacements:
        struct.pack_into("<H", fixed_record, trailer_offset, replacement)

    return NtfsFileRecordHeader(
        fixed_record=bytes(fixed_record),
        sequence_number=sequence_number,
        hard_link_count=hard_link_count,
        first_attribute_offset=first_attribute_offset,
        flags=flags,
        bytes_in_use=bytes_in_use,
        bytes_allocated=bytes_allocated,
        base_file_reference=base_file_reference,
        next_attribute_id=next_attribute_id,
        update_sequence_number=update_sequence_number,
        update_sequence_offset=fixup_offset,
        update_sequence_count=fixup_count,
    )


__all__ = [
    "NtfsFileRecordHeader",
    "NtfsRecordError",
    "parse_ntfs_file_record_header",
]
