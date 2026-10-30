# SPDX-License-Identifier: MIT
"""Conservative, bounded, read-only NTFS metadata and tree provider.

The accepted profile validates the boot region, MFT records and fixups,
attributes/runlists, directory indexes, volume state and bounded $LogFile
restart metadata before allowing list/stat/read-range operations. Recovery,
log replay and every native NTFS write operation remain disabled.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, replace
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
from mountfile.safety import RefusalCode, RefusalError, checked_range
from mountfile.vfs_api import ReadOnlyVFSStatMixin

_ATTRIBUTE_END = 0xFFFFFFFF
_ATTRIBUTE_LIST = 0x20
_MAX_ATTRIBUTES = 4096
_MAX_ATTRIBUTE_LIST_BYTES = 1 << 20
_MAX_RUNLIST_BYTES = 1 << 20
_ATTRIBUTE_FLAG_COMPRESSED = 0x0001
_ATTRIBUTE_FLAG_ENCRYPTED = 0x4000
_ATTRIBUTE_FLAG_SPARSE = 0x8000
_VOLUME_KNOWN_FLAGS = 0x807F
_LOGFILE_PAGE_SIZE = 4096
_MAX_READ_RANGE_BYTES = 1 << 20
_UPCASE_TABLE_ENTRIES = 1 << 16
_UPCASE_TABLE_SIZE = _UPCASE_TABLE_ENTRIES * 2
_LOGFILE_CLEAN_DISMOUNT = 0x0002
_LOGFILE_KNOWN_FLAGS = _LOGFILE_CLEAN_DISMOUNT | 0x0001


class NtfsMetadataError(RefusalError):
    """NTFS metadata is malformed, inconsistent, or outside the selected extent."""

    def __init__(
        self, message: str, code: RefusalCode = RefusalCode.MALFORMED_METADATA
    ):
        super().__init__(message, code)


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
class NtfsAttributeListEntry:
    """One bounded reference to an attribute in an NTFS FILE record."""

    type_code: int
    name: str
    lowest_vcn: int
    file_reference: int
    attribute_id: int


@dataclass(frozen=True)
class NtfsFileName:
    """The bounded, resident portion of an NTFS FILE_NAME attribute."""

    parent_reference: int
    name: str
    namespace: int
    file_attributes: int


@dataclass(frozen=True)
class NtfsStandardInformation:
    """The fixed prefix of a resident STANDARD_INFORMATION attribute."""

    file_attributes: int
    version: int
    class_id: int


@dataclass(frozen=True)
class NtfsMetadataProfile:
    boot: NtfsBootProfile
    mft_record: NtfsFileRecordHeader
    mirror_record: NtfsFileRecordHeader
    attributes: tuple[NtfsAttributeSummary, ...]
    logfile_restart: "NtfsLogFileRestartArea | None" = None

    @property
    def mirror_matches_mft(self) -> bool:
        return self.mft_record.fixed_record == self.mirror_record.fixed_record


@dataclass(frozen=True)
class NtfsLogFileRestartArea:
    """Validated, read-only eligibility metadata from one ``$LogFile`` page."""

    system_page_size: int
    log_page_size: int
    restart_offset: int
    restart_length: int
    flags: int
    current_lsn: int
    revision: int
    version: tuple[int, int] = (0, 0)

    @property
    def clean_dismount(self) -> bool:
        return bool(self.flags & _LOGFILE_CLEAN_DISMOUNT)


def _fixup_logfile_page(raw_page: bytes, *, page_size: int) -> bytes:
    if len(raw_page) != page_size or page_size < 512 or page_size % 512:
        raise NtfsMetadataError("NTFS $LogFile restart page is truncated or mis-sized")
    if raw_page[:4] != b"RSTR":
        raise NtfsMetadataError("NTFS $LogFile restart page signature is invalid")
    usa_offset, usa_count = struct.unpack_from("<HH", raw_page, 4)
    expected = page_size // 512 + 1
    if usa_count != expected or usa_offset < 0x1E or usa_offset + usa_count * 2 > 512:
        raise NtfsMetadataError("NTFS $LogFile restart page USA geometry is invalid")
    sequence = struct.unpack_from("<H", raw_page, usa_offset)[0]
    fixed = bytearray(raw_page)
    for index in range(page_size // 512):
        trailer = (index + 1) * 512 - 2
        if struct.unpack_from("<H", raw_page, trailer)[0] != sequence:
            raise NtfsMetadataError("NTFS $LogFile restart page USA mismatch")
        replacement = struct.unpack_from("<H", raw_page, usa_offset + 2 + index * 2)[0]
        struct.pack_into("<H", fixed, trailer, replacement)
    return bytes(fixed)


def parse_ntfs_logfile_restart_area(raw_page: bytes) -> NtfsLogFileRestartArea:
    """Validate one bounded 4 KiB ``RSTR`` restart page, without replaying logs."""
    if len(raw_page) != _LOGFILE_PAGE_SIZE:
        raise NtfsMetadataError("NTFS $LogFile restart page must be exactly 4096 bytes")
    fixed = _fixup_logfile_page(raw_page, page_size=_LOGFILE_PAGE_SIZE)
    system_page, log_page = struct.unpack_from("<II", fixed, 0x10)
    restart_offset = struct.unpack_from("<H", fixed, 0x18)[0]
    minor, major = struct.unpack_from("<hh", fixed, 0x1A)
    if (
        system_page != _LOGFILE_PAGE_SIZE
        or log_page < 512
        or log_page > (1 << 20)
        or log_page & (log_page - 1)
    ):
        raise NtfsMetadataError("NTFS $LogFile restart page geometry is unsupported")
    if major < 1 or major > 2 or (major == 1 and minor == 0):
        raise NtfsMetadataError("NTFS $LogFile restart page version is unsupported")
    usa_offset = struct.unpack_from("<H", fixed, 4)[0]
    usa_end = usa_offset + struct.unpack_from("<H", fixed, 6)[0] * 2
    if restart_offset < (usa_end + 7) & ~7 or restart_offset + 0x2A > system_page:
        raise NtfsMetadataError("NTFS $LogFile restart offset is outside the page")
    area = restart_offset
    current_lsn = struct.unpack_from("<Q", fixed, area)[0]
    clients = struct.unpack_from("<H", fixed, area + 8)[0]
    flags = struct.unpack_from("<H", fixed, area + 0x0E)[0]
    restart_length = struct.unpack_from("<H", fixed, area + 0x14)[0]
    client_offset = struct.unpack_from("<H", fixed, area + 0x16)[0]
    revision = struct.unpack_from("<I", fixed, area + 0x28)[0]
    if clients > 1 or flags & ~_LOGFILE_KNOWN_FLAGS:
        raise NtfsMetadataError("NTFS $LogFile restart area has unknown state")
    if (
        client_offset < 0x30
        or client_offset & 7
        or client_offset + clients * 0xA0 > restart_length
    ):
        raise NtfsMetadataError("NTFS $LogFile client array bounds are invalid")
    if restart_length < 0x2A or area + restart_length > system_page:
        raise NtfsMetadataError("NTFS $LogFile restart length exceeds the page")
    return NtfsLogFileRestartArea(
        system_page_size=system_page,
        log_page_size=log_page,
        restart_offset=restart_offset,
        restart_length=restart_length,
        flags=flags,
        current_lsn=current_lsn,
        revision=revision,
        version=(major, minor),
    )


def validate_ntfs_logfile_restart_pages(raw_logfile: bytes) -> NtfsLogFileRestartArea:
    """Validate the first two restart pages and return their newer clean area."""
    if len(raw_logfile) < 2 * _LOGFILE_PAGE_SIZE:
        raise NtfsMetadataError("NTFS $LogFile DATA stream is truncated")
    first = parse_ntfs_logfile_restart_area(raw_logfile[:_LOGFILE_PAGE_SIZE])
    second = parse_ntfs_logfile_restart_area(
        raw_logfile[_LOGFILE_PAGE_SIZE : 2 * _LOGFILE_PAGE_SIZE]
    )
    if (
        first.system_page_size,
        first.log_page_size,
        first.restart_offset,
        first.restart_length,
    ) != (
        second.system_page_size,
        second.log_page_size,
        second.restart_offset,
        second.restart_length,
    ):
        raise NtfsMetadataError("NTFS $LogFile restart pages disagree on geometry")
    if first.version != second.version:
        raise NtfsMetadataError("NTFS $LogFile restart pages disagree on version")
    if first.flags != second.flags:
        raise NtfsMetadataError("NTFS $LogFile restart pages disagree on clean state")
    if not first.clean_dismount:
        raise NtfsMetadataError("NTFS $LogFile requires recovery or has unknown state")
    return max((first, second), key=lambda item: (item.revision, item.current_lsn))


def _read_exact(stream, absolute: int, size: int, image_size: int, end: int) -> bytes:
    try:
        checked_range(
            absolute,
            size,
            lower=0,
            upper=end,
            context="NTFS metadata read exceeds selected volume bounds",
            error_type=NtfsMetadataError,
        )
        checked_range(
            absolute,
            size,
            lower=0,
            upper=image_size,
            context="NTFS metadata read exceeds image bounds",
            error_type=NtfsMetadataError,
        )
    except NtfsMetadataError as exc:
        # Keep the long-standing human-readable NTFS errors stable while
        # exposing a machine-readable refusal code to callers.
        raise NtfsMetadataError(str(exc), RefusalCode.RANGE_OUT_OF_BOUNDS) from exc
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
        if lcn is not None and (
            lcn < 0 or lcn + run_length < lcn or lcn + run_length > cluster_count
        ):
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


def parse_ntfs_attribute_list(
    value: bytes, *, upcase_table: tuple[int, ...] | None = None
) -> tuple[NtfsAttributeListEntry, ...]:
    """Validate a bounded resident ``$ATTRIBUTE_LIST`` value.

    Extension-record traversal and extent merging remain unsupported by the
    native provider; this parser only validates the list's serialized entries.
    A validated ``$UpCase`` table enables NTFS ordering checks for Unicode names.
    """
    try:
        view = memoryview(value)
    except TypeError as exc:
        raise NtfsMetadataError("NTFS ATTRIBUTE_LIST value must be bytes-like") from exc
    if not view.nbytes or view.nbytes > _MAX_ATTRIBUTE_LIST_BYTES or view.nbytes & 7:
        raise NtfsMetadataError("NTFS ATTRIBUTE_LIST has invalid bounded length")
    if upcase_table is not None:
        try:
            if len(upcase_table) != _UPCASE_TABLE_ENTRIES:
                raise NtfsMetadataError("$UpCase entry count is invalid")
            table_bytes = struct.pack("<65536H", *upcase_table)
            upcase_table = _parse_ntfs_upcase_table(table_bytes)
        except (NtfsMetadataError, struct.error, TypeError, OverflowError) as exc:
            raise NtfsMetadataError(
                "NTFS ATTRIBUTE_LIST received an invalid $UpCase table"
            ) from exc
    raw = view.tobytes()
    result: list[NtfsAttributeListEntry] = []
    seen: set[tuple[int, object, int]] = set()
    pos = 0
    previous_type = 0
    previous_name: str | None = None
    previous_name_key: tuple[int, ...] | None = None
    previous_lowest_vcn = 0
    while pos < len(raw):
        if len(result) >= _MAX_ATTRIBUTES or pos + 26 > len(raw):
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST entry header is truncated")
        type_code, entry_length, name_length, name_offset = struct.unpack_from(
            "<IHBB", raw, pos
        )
        if type_code == _ATTRIBUTE_LIST:
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST must not reference itself")
        if type_code == 0 or type_code == _ATTRIBUTE_END or type_code < previous_type:
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST type order is invalid")
        if entry_length < 26 or entry_length & 7 or pos + entry_length > len(raw):
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST entry length is invalid")
        lowest_vcn = struct.unpack_from("<q", raw, pos + 8)[0]
        if lowest_vcn < 0:
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST lowest VCN is negative")
        if name_length:
            if (
                name_offset < 26
                or name_offset & 1
                or name_offset + name_length * 2 > entry_length
            ):
                raise NtfsMetadataError("NTFS ATTRIBUTE_LIST name exceeds its entry")
            name = _decode_name(raw[pos : pos + entry_length], name_length, name_offset)
            if "\x00" in name:
                raise NtfsMetadataError("NTFS ATTRIBUTE_LIST name contains NUL")
        else:
            if name_offset not in (0, 26):
                raise NtfsMetadataError(
                    "NTFS ATTRIBUTE_LIST empty-name offset is invalid"
                )
            name = ""
        try:
            encoded_name = name.encode("utf-16le")
        except UnicodeEncodeError as exc:
            raise NtfsMetadataError(
                "NTFS ATTRIBUTE_LIST name is not valid UTF-16"
            ) from exc
        if upcase_table is not None:
            units = struct.unpack(f"<{len(encoded_name) // 2}H", encoded_name)
            name_key = tuple(upcase_table[unit] for unit in units)
        elif name.isascii():
            name_key = tuple(ord(character.upper()) for character in name)
        else:
            name_key = None
        if type_code == previous_type and result:
            if previous_name_key is not None and name_key is not None:
                if name_key < previous_name_key or (
                    name_key == previous_name_key and lowest_vcn < previous_lowest_vcn
                ):
                    raise NtfsMetadataError(
                        "NTFS ATTRIBUTE_LIST name/VCN order is invalid"
                    )
            elif name == previous_name and lowest_vcn < previous_lowest_vcn:
                raise NtfsMetadataError("NTFS ATTRIBUTE_LIST VCN order is invalid")
        file_reference = struct.unpack_from("<Q", raw, pos + 16)[0]
        try:
            decode_ntfs_file_reference(file_reference)
        except NtfsRecordError as exc:
            raise NtfsMetadataError(str(exc)) from exc
        attribute_id = struct.unpack_from("<H", raw, pos + 24)[0]
        identity_name: object = name_key if name_key is not None else name
        identity = (type_code, identity_name, lowest_vcn)
        if identity in seen:
            raise NtfsMetadataError("NTFS ATTRIBUTE_LIST contains a duplicate entry")
        seen.add(identity)
        result.append(
            NtfsAttributeListEntry(
                type_code, name, lowest_vcn, file_reference, attribute_id
            )
        )
        previous_type = type_code
        previous_name = name
        previous_name_key = name_key
        previous_lowest_vcn = lowest_vcn
        pos += entry_length
    return tuple(result)


def parse_ntfs_attribute_stream(
    record: NtfsFileRecordHeader,
    *,
    cluster_count: int | None = None,
    upcase_table: tuple[int, ...] | None = None,
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
        name_minimum = 64 if nonresident else 24
        if name_length and name_offset < name_minimum:
            raise NtfsMetadataError("NTFS attribute name offset is inside its header")
        if not name_length and name_offset not in (0, name_minimum):
            raise NtfsMetadataError("NTFS unnamed attribute has an invalid name offset")
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
        if type_code == _ATTRIBUTE_LIST:
            if name:
                raise NtfsMetadataError("NTFS ATTRIBUTE_LIST must be unnamed")
            if resident_value is not None:
                parse_ntfs_attribute_list(resident_value, upcase_table=upcase_table)
            raise NtfsMetadataError(
                "NTFS ATTRIBUTE_LIST extension-record resolution is unsupported"
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


def _parse_ntfs_index_entries(
    value: bytes,
    *,
    start: int,
    end: int,
    parent_reference: int | None = None,
    allow_children: bool = False,
) -> tuple[tuple[NtfsIndexEntry, ...], tuple[int, ...]]:
    """Parse bounded FILE_NAME index entries and return child VCNs."""
    if start < 0 or end < start or end > len(value):
        raise NtfsMetadataError("NTFS index entries exceed the attribute")
    if len(value) < 32:
        raise NtfsMetadataError("NTFS index value is truncated")
    result: list[NtfsIndexEntry] = []
    children: list[int] = []
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
        if flags & 1 and not allow_children:
            raise NtfsMetadataError(
                "NTFS directory requires INDEX_ALLOCATION traversal"
            )
        if flags & 0x00000002:
            if key_length:
                raise NtfsMetadataError("NTFS index end entry contains a key")
            if pos + entry_length != end:
                raise NtfsMetadataError("NTFS index end entry is not terminal")
            if flags & 1:
                if entry_length < 24:
                    raise NtfsMetadataError("NTFS index child pointer is truncated")
                children.append(
                    struct.unpack_from("<Q", value, pos + entry_length - 8)[0]
                )
            return tuple(result), tuple(children)
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
        if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            raise NtfsMetadataError(
                "NTFS index name contains an invalid path component"
            )
        folded = name.lower() if name.isascii() else name
        previous_is_ascii = previous_name is not None and previous_name.isascii()
        if folded in names or (
            name.isascii() and previous_is_ascii and folded < previous_name.lower()
        ):
            raise NtfsMetadataError("NTFS index names are duplicate or unsorted")
        names.add(folded)
        previous_name = name
        result.append(NtfsIndexEntry(reference, name, bool(key[0x38] & 0x10000000)))
        if flags & 1:
            if not allow_children or entry_length < 24 + key_length:
                raise NtfsMetadataError(
                    "NTFS directory requires INDEX_ALLOCATION traversal"
                )
            children.append(struct.unpack_from("<Q", value, pos + entry_length - 8)[0])
        pos += entry_length
    raise NtfsMetadataError("NTFS INDEX_ROOT has no end entry")


def parse_ntfs_index_root(
    value: bytes,
    *,
    parent_reference: int | None = None,
    allow_index_allocation: bool = False,
) -> tuple[NtfsIndexEntry, ...]:
    """Parse a resident INDEX_ROOT value containing bounded FILE_NAME keys."""
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
    entries, _children = _parse_ntfs_index_entries(
        value,
        start=start,
        end=end,
        parent_reference=parent_reference,
        allow_children=allow_index_allocation,
    )
    return entries


def _parse_ntfs_index_block(
    raw_block: bytes,
    *,
    parent_reference: int | None = None,
    expected_vcn: int | None = None,
    allow_children: bool = False,
) -> tuple[tuple[NtfsIndexEntry, ...], tuple[int, ...]]:
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
    if (
        expected_vcn is not None
        and struct.unpack_from("<q", fixed, 16)[0] != expected_vcn
    ):
        raise NtfsMetadataError("NTFS INDX VCN does not match requested block")
    entry_offset, total_size, allocated_size = struct.unpack_from("<III", fixed, 24)
    flags = struct.unpack_from("<I", fixed, 36)[0]
    if flags & ~1 or allocated_size < total_size or entry_offset < 16:
        raise NtfsMetadataError("invalid NTFS INDX header bounds")
    start = 24 + entry_offset
    end = 24 + total_size
    entries, children = _parse_ntfs_index_entries(
        bytes(fixed),
        start=start,
        end=end,
        parent_reference=parent_reference,
        allow_children=allow_children,
    )
    if children and not allow_children:
        raise NtfsMetadataError("NTFS INDX child pointers are unsupported")
    return entries, children


def parse_ntfs_index_block(
    raw_block: bytes,
    *,
    parent_reference: int | None = None,
    expected_vcn: int | None = None,
) -> tuple[NtfsIndexEntry, ...]:
    """Validate an INDX block and parse entries without exposing child pointers."""
    entries, _children = _parse_ntfs_index_block(
        raw_block, parent_reference=parent_reference, expected_vcn=expected_vcn
    )
    return entries


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
    flags = struct.unpack_from("<H", volume.resident_value, 10)[0]
    if flags & ~_VOLUME_KNOWN_FLAGS:
        raise NtfsMetadataError("NTFS $Volume has unknown volume flags")
    return NtfsVolumeState(flags)


def parse_ntfs_file_name(value: bytes) -> NtfsFileName:
    """Parse a resident FILE_NAME value with bounded UTF-16 validation."""
    if len(value) < 66:
        raise NtfsMetadataError("NTFS FILE_NAME value is truncated")
    parent_reference = struct.unpack_from("<Q", value, 0)[0]
    try:
        decode_ntfs_file_reference(parent_reference)
    except NtfsRecordError as exc:
        raise NtfsMetadataError("invalid NTFS FILE_NAME parent reference") from exc
    name_length = value[64]
    namespace = value[65]
    if name_length == 0 or namespace > 3:
        raise NtfsMetadataError("invalid NTFS FILE_NAME name or namespace")
    end = 66 + name_length * 2
    if end > len(value):
        raise NtfsMetadataError("NTFS FILE_NAME name exceeds its value")
    try:
        name = value[66:end].decode("utf-16le")
    except UnicodeDecodeError as exc:
        raise NtfsMetadataError("NTFS FILE_NAME is not valid UTF-16LE") from exc
    if name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
        raise NtfsMetadataError("NTFS FILE_NAME contains an invalid path component")
    return NtfsFileName(
        parent_reference,
        name,
        namespace,
        struct.unpack_from("<I", value, 56)[0],
    )


def parse_ntfs_standard_information(value: bytes) -> NtfsStandardInformation:
    """Parse the stable 48-byte prefix of resident STANDARD_INFORMATION."""
    if len(value) < 48:
        raise NtfsMetadataError("NTFS STANDARD_INFORMATION value is truncated")
    return NtfsStandardInformation(
        struct.unpack_from("<I", value, 32)[0],
        struct.unpack_from("<I", value, 40)[0],
        struct.unpack_from("<I", value, 44)[0],
    )


def _parse_ntfs_upcase_table(raw: bytes) -> tuple[int, ...]:
    """Parse and validate the bounded 64K-entry UTF-16 NTFS upcase table."""
    if len(raw) != _UPCASE_TABLE_SIZE:
        raise NtfsMetadataError("NTFS $UpCase DATA stream has an invalid size")
    table = struct.unpack("<65536H", raw)
    if any(table[mapped] != mapped for mapped in table):
        raise NtfsMetadataError("NTFS $UpCase table contains a non-idempotent mapping")
    if any(
        table[codepoint] != codepoint for codepoint in range(ord("A"), ord("Z") + 1)
    ):
        raise NtfsMetadataError("NTFS $UpCase table changes an uppercase ASCII letter")
    if any(
        table[codepoint] != codepoint - 32
        for codepoint in range(ord("a"), ord("z") + 1)
    ):
        raise NtfsMetadataError("NTFS $UpCase table has unsupported ASCII mappings")
    return table


def _validate_nonresident_data_stream(
    attribute: NtfsAttributeSummary, cluster_size: int, *, name: str
) -> None:
    if not attribute.nonresident or attribute.lowest_vcn != 0 or not attribute.runs:
        raise NtfsMetadataError(f"{name} has no complete nonresident DATA stream")
    clusters = sum(run.length for run in attribute.runs)
    if clusters <= 0 or clusters > (1 << 63) // cluster_size:
        raise NtfsMetadataError(f"{name} DATA runlist capacity overflows")
    capacity = clusters * cluster_size
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
    "NtfsAttributeListEntry",
    "NtfsAttributeSummary",
    "NtfsIndexEntry",
    "NtfsMetadataError",
    "NtfsVolumeState",
    "NtfsMetadataProfile",
    "NtfsLogFileRestartArea",
    "NtfsRun",
    "NtfsFileName",
    "NtfsStandardInformation",
    "parse_ntfs_attribute_list",
    "parse_ntfs_attribute_stream",
    "parse_ntfs_index_root",
    "parse_ntfs_index_block",
    "parse_ntfs_volume_state",
    "parse_ntfs_file_name",
    "parse_ntfs_standard_information",
    "parse_ntfs_logfile_restart_area",
    "validate_ntfs_logfile_restart_pages",
    "probe_ntfs_metadata",
]


class NativeNtfsMetadataImage(ReadOnlyVFSStatMixin):
    """Read-only access to the validated bounded NTFS tree and unnamed DATA."""

    backend_name = "native-ntfs-metadata-ro"
    case_insensitive_paths = False
    case_insensitive_ascii_paths = True
    max_read_range_bytes = _MAX_READ_RANGE_BYTES

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
        self._upcase_table: tuple[int, ...] | None = None
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
        try:
            logfile_record = self.read_mft_record(2)
            logfile_attrs = parse_ntfs_attribute_stream(
                logfile_record, cluster_count=self.profile.boot.cluster_count
            )
            logfile_data = next(
                (a for a in logfile_attrs if a.type_code == 0x80 and not a.name), None
            )
            if logfile_data is None:
                raise NtfsMetadataError("NTFS $LogFile has no unnamed DATA stream")
            _validate_nonresident_data_stream(
                logfile_data, self.profile.boot.cluster_size, name="$LogFile"
            )
            if logfile_data.data_size < 2 * _LOGFILE_PAGE_SIZE:
                raise NtfsMetadataError(
                    "NTFS $LogFile DATA stream is shorter than two restart pages"
                )
            self.logfile_restart = validate_ntfs_logfile_restart_pages(
                self._read_runs(logfile_data.runs, 0, 2 * _LOGFILE_PAGE_SIZE)
            )
            self.profile = replace(self.profile, logfile_restart=self.logfile_restart)
            self._upcase_table = self._load_upcase_table()
        except Exception:
            self.close()
            raise
        self.close()

    def close(self):
        self._stream.close()

    def reopen(self):
        if self._stream.closed:
            self._stream = Path(self.image_path).open("rb")  # noqa: SIM115

    def _load_upcase_table(self) -> tuple[int, ...] | None:
        """Load $UpCase only when its unnamed DATA stream has the exact size."""
        try:
            record = self.read_mft_record(10)
            attributes = parse_ntfs_attribute_stream(
                record, cluster_count=self.profile.boot.cluster_count
            )
        except NtfsMetadataError:
            return None
        data_streams = [
            attribute
            for attribute in attributes
            if attribute.type_code == 0x80 and not attribute.name
        ]
        if len(data_streams) != 1:
            return None
        data = data_streams[0]
        if data.resident_value is not None:
            raw = data.resident_value
        else:
            if data.data_size != _UPCASE_TABLE_SIZE:
                return None
            try:
                _validate_nonresident_data_stream(
                    data, self.profile.boot.cluster_size, name="$UpCase"
                )
                raw = self._read_runs(data.runs, 0, _UPCASE_TABLE_SIZE)
            except NtfsMetadataError:
                return None
        try:
            return _parse_ntfs_upcase_table(raw)
        except NtfsMetadataError:
            return None

    def _name_key(self, name: str) -> tuple[int, ...]:
        """Return a conservative NTFS comparison key for one path component."""
        try:
            encoded = name.encode("utf-16le")
        except UnicodeEncodeError as exc:
            raise NtfsMetadataError("NTFS path component is not valid UTF-16") from exc
        if len(encoded) > 255 * 2:
            raise NtfsMetadataError("NTFS path component exceeds 255 UTF-16 code units")
        table = getattr(self, "_upcase_table", None)
        if table is None:
            if name.isascii():
                encoded = name.lower().encode("utf-16le")
            return struct.unpack(f"<{len(encoded) // 2}H", encoded)
        units = struct.unpack(f"<{len(encoded) // 2}H", encoded)
        return tuple(table[unit] for unit in units)

    def _path_names_equal(self, requested: str, actual: str) -> bool:
        if requested == actual:
            return True
        return self._name_key(requested) == self._name_key(actual)

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
                child,
                cluster_count=self.profile.boot.cluster_count,
                upcase_table=getattr(self, "_upcase_table", None),
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
                    record,
                    cluster_count=self.profile.boot.cluster_count,
                    upcase_table=getattr(self, "_upcase_table", None),
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
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or not isinstance(length, int)
            or isinstance(length, bool)
        ):
            raise NtfsMetadataError("invalid NTFS metadata read request")
        if offset < 0 or length < 0:
            raise NtfsMetadataError("negative NTFS read range")
        if length > _MAX_READ_RANGE_BYTES:
            raise NtfsMetadataError("NTFS read range exceeds the 1 MiB limit")
        if path == "/$MFT":
            data = self._mft_data
            if offset >= data.data_size or not length:
                return b""
            length = min(length, data.data_size - offset)
            return self._read_runs(data.runs, offset, length)
        _number, record = self._resolve(path)
        if record.is_directory:
            raise NtfsMetadataError("cannot read directory content as a file")
        data = next(
            (
                a
                for a in parse_ntfs_attribute_stream(
                    record,
                    cluster_count=self.profile.boot.cluster_count,
                    upcase_table=getattr(self, "_upcase_table", None),
                )
                if a.type_code == 0x80 and not a.name
            ),
            None,
        )
        if data is None:
            raise NtfsMetadataError("NTFS file has no unnamed DATA attribute")
        if data.resident_value is not None:
            if offset >= len(data.resident_value) or not length:
                return b""
            return data.resident_value[offset : offset + length]
        if offset >= data.data_size or not length:
            return b""
        length = min(length, data.data_size - offset)
        return self._read_runs(data.runs, offset, length)

    def _read_runs(self, runs, offset, length):
        self.reopen()
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
        if any(part in (".", "..") or "\\" in part or "\x00" in part for part in parts):
            raise NtfsMetadataError("NTFS path contains an invalid component")
        number = 5
        record = self.read_mft_record(number)
        visited = {number}
        for part in parts:
            match = next(
                (
                    item
                    for item in self.list_dir_by_record(number, record)
                    if self._path_names_equal(item.name, part)
                ),
                None,
            )
            if match is None:
                raise NtfsMetadataError(f"NTFS path component not found: {part}")
            number = match.reference & ((1 << 48) - 1)
            if number in visited:
                raise NtfsMetadataError("NTFS directory tree contains a cycle")
            visited.add(number)
            record = self.read_mft_record(number, reference=match.reference)
        return number, record

    def list_dir_by_record(self, record_number, record):
        attrs = parse_ntfs_attribute_stream(
            record,
            cluster_count=self.profile.boot.cluster_count,
            upcase_table=getattr(self, "_upcase_table", None),
        )
        roots = [a for a in attrs if a.type_code == 0x90 and a.name == "$I30"]
        if len(roots) != 1 or roots[0].nonresident or roots[0].resident_value is None:
            raise NtfsMetadataError(
                "NTFS directory has no unique resident $I30 INDEX_ROOT"
            )
        root = roots[0]
        root_value = root.resident_value
        parent_reference = record_number | (record.sequence_number << 48)
        entry_offset, total_size, allocated_size = struct.unpack_from(
            "<III", root_value, 16
        )
        index_flags = struct.unpack_from("<I", root_value, 28)[0]
        if index_flags & ~1 or allocated_size < total_size or entry_offset < 16:
            raise NtfsMetadataError("invalid NTFS INDEX_ROOT header bounds")
        root_entries, child_vcns = _parse_ntfs_index_entries(
            root_value,
            start=16 + entry_offset,
            end=16 + total_size,
            parent_reference=parent_reference,
            allow_children=bool(index_flags & 1),
        )
        entries = list(root_entries)
        if index_flags & 1:
            allocations = [a for a in attrs if a.type_code == 0xA0 and a.name == "$I30"]
            if len(allocations) != 1:
                raise NtfsMetadataError(
                    "NTFS directory has no unique $I30 INDEX_ALLOCATION"
                )
            allocation = allocations[0]
            _validate_nonresident_data_stream(
                allocation, self.profile.boot.cluster_size, name="$I30"
            )
            block_size = self.profile.boot.index_record_size
            if block_size < 512 or allocation.data_size % block_size:
                raise NtfsMetadataError(
                    "NTFS $I30 INDEX_ALLOCATION has invalid block bounds"
                )
            pending = list(child_vcns)
            visited_vcns: set[int] = set()
            while pending:
                vcn = pending.pop()
                if vcn in visited_vcns:
                    raise NtfsMetadataError("NTFS $I30 index contains a cycle")
                if vcn < 0 or vcn * block_size >= allocation.data_size:
                    raise NtfsMetadataError("NTFS $I30 child VCN is outside allocation")
                visited_vcns.add(vcn)
                block = self._read_runs(allocation.runs, vcn * block_size, block_size)
                block_entries, block_children = _parse_ntfs_index_block(
                    block,
                    parent_reference=parent_reference,
                    expected_vcn=vcn,
                    allow_children=True,
                )
                entries.extend(block_entries)
                pending.extend(block_children)
        unique: dict[tuple[tuple[int, ...], int], NtfsIndexEntry] = {}
        names: set[tuple[int, ...]] = set()
        for entry in entries:
            folded = self._name_key(entry.name)
            if folded in names:
                raise NtfsMetadataError("NTFS directory index contains duplicate names")
            names.add(folded)
            unique[(folded, entry.reference)] = entry
        return tuple(sorted(unique.values(), key=lambda item: item.name.lower()))

    def read_file(self, path):
        """Return the complete file, fetching it through bounded ranges."""
        size = self.file_size(path)
        output = bytearray()
        offset = 0
        while offset < size:
            requested = min(_MAX_READ_RANGE_BYTES, size - offset)
            chunk = self.read_file_range(path, offset, requested)
            if len(chunk) != requested:
                raise NtfsMetadataError(
                    "short NTFS read while assembling complete file"
                )
            output.extend(chunk)
            offset += len(chunk)
        return bytes(output)

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
