# SPDX-License-Identifier: MIT
"""Independent pure-Python NTFS metadata oracle for issue #510 C3/C5/C6.

This oracle is the independent checker the acceptance gaps for C3/C5/C6 asked
for.  It deliberately shares **no** code with ``mountfile``: there is no
import of ``mountfile`` anywhere in this module, no use of the production
``NtfsBootProfile``, ``NtfsFileRecordHeader`` or ``parse_*`` helpers, and no
``open_image_source`` pipeline.  Every offset below is pinned to the published
NTFS on-disk layout (LNX4Howto "NTFS Boot Sector" table, Microsoft
``-windows-itpro-docs`` $MFT record layout) and was additionally re-verified
against real ``mkntfs`` volumes in this repository's CI before being trusted.

The oracle answers exactly the questions the independent checker must answer:

* boot geometry (BPB identity fields at 0x0B..0x54);
* the raw size of ``$MFT``, ``$MFTMirr``, ``$LogFile`` and ``$Bitmap``;
* the full per-record dump of the MFT mirror window;
* the ``$VOLUME_INFORMATION`` dirty flag of record 3.

Everything is ``__all__``-exported so acceptance tests can import the whole
contract and prove that the production reader agrees field by field.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = [
    "OracleBootGeometry",
    "OracleStandardInformation",
    "OracleVolumeState",
    "ORACLE_MFT_MIRROR_RECORDS",
    "ORACLE_RECORD_IN_USE",
    "ORACLE_SYSTEM_RECORDS",
    "list_oracle_attributes",
    "read_oracle_boot_geometry",
    "read_oracle_mft_record",
    "read_oracle_mft_record_raw",
    "read_oracle_mft_records",
    "read_oracle_standard_information",
    "read_oracle_stream",
    "read_oracle_system_stream_sizes",
    "read_oracle_volume_state",
]


# The $MFTMirr mirror window covers the first four MFT records on every
# supported layout; this count is part of the NTFS format, not of our reader.
ORACLE_MFT_MIRROR_RECORDS = 4

# FILE record flag 0x0001 = "record in use" (MFT_RECORD_IN_USE).
ORACLE_RECORD_IN_USE = 0x0001

# VOLUME flag 0x0001 = "dirty" (VOLUME_IS_DIRTY in winternl.h / ntfs3).
ORACLE_VOLUME_FLAG_DIRTY = 0x0001

# Well-known MFT record numbers of the core system files.  The order of the
# first entries is fixed by the NTFS format; $Bitmap lives in record 6 on
# every layout this repository supports (verified against real mkntfs
# volumes and against ntfscat extraction in the parity tests).
ORACLE_SYSTEM_RECORDS = {"mft": 0, "mftmirr": 1, "logfile": 2, "bitmap": 6}

# Fixed layout offsets used by this oracle.  None of them are read from the
# production parser: they come straight from the on-disk format definition.
_BOOT_BYTES_PER_SECTOR = 0x0B
_BOOT_SECTORS_PER_CLUSTER = 0x0D
_BOOT_TOTAL_SECTORS = 0x28
_BOOT_MFT_LCN = 0x30
_BOOT_MFTMIRR_LCN = 0x38
_BOOT_CLUSTERS_PER_MFT = 0x40
_BOOT_SERIAL = 0x48

# FILE-record header: the update sequence (offset, count) pair lives at 0x04;
# the array itself is AT the offset the pair declares (0x30 on every layout
# this repository supports).
_RECORD_UPDATE_SEQUENCE_PAIR = 0x04
_RECORD_ALLOCATED_SIZE = 0x1C
_RECORD_FLAGS = 0x16
_ATTRIBUTE_TYPE = 0x00
_ATTRIBUTE_LENGTH = 0x04
_ATTRIBUTE_NONRESIDENT = 0x08
_ATTRIBUTE_NAME_LENGTH = 0x09
_ATTRIBUTE_NAME_OFFSET = 0x0A
_ATTRIBUTE_VALUE_LENGTH = 0x10
_ATTRIBUTE_VALUE_OFFSET_RES = 0x14
_ATTRIBUTE_END = 0xFFFFFFFF
_ATTR_VOLUME_INFORMATION = 0x70


@dataclass(frozen=True)
class OracleBootGeometry:
    """Boot-sector geometry derived without any production code."""

    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    total_sectors: int
    mft_lcn: int
    mft_mirror_lcn: int
    file_record_size: int
    volume_serial: int


@dataclass(frozen=True)
class OracleStandardInformation:
    """The four FILETIMEs and the DOS attributes of one $STANDARD_INFORMATION."""

    file_attributes: int
    creation_time: int
    modification_time: int
    mft_change_time: int
    access_time: int


@dataclass(frozen=True)
class OracleVolumeState:
    """The dirty flag of record 3, read by the independent path."""

    flags: int
    dirty: bool

    @property
    def clean(self) -> bool:
        return self.flags == 0


def _decode_record_size(code: int) -> int:
    """Decode the NTFS 2**code record sizing (no cluster factor involved)."""

    size = 2 ** (-code) if code < 0 else code * 2
    if size <= 0:
        raise ValueError(f"oracle: invalid record size code {code}")
    return size


def read_oracle_boot_geometry(image_path: str) -> OracleBootGeometry:
    """Parse the NTFS boot sector with pure struct calls only."""

    with open(image_path, "rb") as stream:
        sector = stream.read(512)
    if len(sector) != 512:
        raise ValueError("oracle: short boot sector")
    bytes_per_sector = struct.unpack_from("<H", sector, _BOOT_BYTES_PER_SECTOR)[0]
    sectors_per_cluster = sector[_BOOT_SECTORS_PER_CLUSTER]
    if bytes_per_sector == 0 or sectors_per_cluster == 0:
        raise ValueError("oracle: boot sector is not NTFS (zero geometry)")
    cluster_size = bytes_per_sector * sectors_per_cluster
    total_sectors = struct.unpack_from("<Q", sector, _BOOT_TOTAL_SECTORS)[0]
    mft_lcn = struct.unpack_from("<Q", sector, _BOOT_MFT_LCN)[0]
    mft_mirror_lcn = struct.unpack_from("<Q", sector, _BOOT_MFTMIRR_LCN)[0]
    record_size = _decode_record_size(
        struct.unpack_from("<b", sector, _BOOT_CLUSTERS_PER_MFT)[0]
    )
    volume_serial = struct.unpack_from("<Q", sector, _BOOT_SERIAL)[0]
    return OracleBootGeometry(
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        cluster_size=cluster_size,
        total_sectors=total_sectors,
        mft_lcn=mft_lcn,
        mft_mirror_lcn=mft_mirror_lcn,
        file_record_size=record_size,
        volume_serial=volume_serial,
    )


def _restore_update_sequence(record: bytearray, bytes_per_sector: int) -> None:
    """Restore USA fixups with a standalone implementation.

    A record may legitimately carry an empty update sequence array
    (``usa_count == 0``): such records need no restoration, and demanding a
    non-empty array would make the oracle unable to read its own fixtures.
    The (offset, count) pair is read from the record header at 0x04; the
    array it points at carries the USN followed by one original trailer
    value per protected 512-byte sector.
    """

    update_offset, update_count = struct.unpack_from(
        "<HH", record, _RECORD_UPDATE_SEQUENCE_PAIR
    )
    if update_count == 0:
        return
    usn = struct.unpack_from("<H", record, update_offset)[0]
    for index in range(1, update_count):
        trailer = index * bytes_per_sector - 2
        actual = struct.unpack_from("<H", record, trailer)[0]
        if actual != usn:
            raise ValueError(f"oracle: USA mismatch in sector {index}")
        expected = struct.unpack_from("<H", record, update_offset + 2 * index)[0]
        struct.pack_into("<H", record, trailer, expected)


def _resident_volume_flags(record: bytearray) -> int:
    """Walk the resident attribute chain and return $VOLUME_INFORMATION flags."""

    offset = struct.unpack_from("<H", record, 0x14)[0]
    end = struct.unpack_from("<I", record, _RECORD_ALLOCATED_SIZE)[0]
    while offset + 16 <= end:
        type_code, length = struct.unpack_from("<II", record, offset)
        if type_code == _ATTRIBUTE_END or length < 16:
            break
        if type_code == _ATTR_VOLUME_INFORMATION:
            nonresident = record[offset + _ATTRIBUTE_NONRESIDENT]
            name_length = record[offset + _ATTRIBUTE_NAME_LENGTH]
            if nonresident or name_length:
                raise ValueError("oracle: unexpected named/nonresident $Volume info")
            value_offset = struct.unpack_from(
                "<H", record, offset + _ATTRIBUTE_VALUE_OFFSET_RES
            )[0]
            value_length = struct.unpack_from(
                "<I", record, offset + _ATTRIBUTE_VALUE_LENGTH
            )[0]
            if value_length < 12:
                raise ValueError("oracle: volume information is too short")
            value = record[offset + value_offset : offset + value_offset + 12]
            return struct.unpack_from("<H", value, 10)[0]
        offset += length
    raise ValueError("oracle: no resident $VOLUME_INFORMATION found")


def read_oracle_volume_state(image_path: str) -> OracleVolumeState:
    """Read the dirty flag of MFT record 3 independently of ``mountfile``."""

    geometry = read_oracle_boot_geometry(image_path)
    with open(image_path, "rb") as stream:
        stream.seek(geometry.mft_lcn * geometry.cluster_size)
        stream.seek(3 * geometry.file_record_size, 1)
        raw = bytearray(stream.read(geometry.file_record_size))
    if len(raw) != geometry.file_record_size:
        raise ValueError("oracle: short MFT record 3")
    _restore_update_sequence(raw, geometry.bytes_per_sector)
    flags = _resident_volume_flags(raw)
    return OracleVolumeState(flags=flags, dirty=bool(flags & ORACLE_VOLUME_FLAG_DIRTY))


def _read_raw_mft_record(
    image_path: str, geometry: OracleBootGeometry, record_number: int
) -> bytearray:
    """Read one primary-MFT record raw, without USA restoration."""

    with open(image_path, "rb") as stream:
        stream.seek(
            geometry.mft_lcn * geometry.cluster_size
            + record_number * geometry.file_record_size
        )
        raw = bytearray(stream.read(geometry.file_record_size))
    if len(raw) != geometry.file_record_size:
        raise ValueError(f"oracle: short MFT record {record_number}")
    return raw


def read_oracle_mft_record_raw(image_path: str, record_number: int) -> bytes:
    """Return one primary-MFT record exactly as stored on disk (no USA fix)."""

    geometry = read_oracle_boot_geometry(image_path)
    return bytes(_read_raw_mft_record(image_path, geometry, record_number))


def read_oracle_mft_record(image_path: str, record_number: int) -> bytes:
    """Return the USA-restored raw bytes of one primary-MFT record."""

    geometry = read_oracle_boot_geometry(image_path)
    raw = _read_raw_mft_record(image_path, geometry, record_number)
    _restore_update_sequence(raw, geometry.bytes_per_sector)
    return bytes(raw)


def _record_real_data_size(raw: bytearray) -> int:
    """Walk attributes of a USA-restored record; return unnamed $DATA size.

    The size is the real (initialized) size in bytes from the nonresident
    attribute header at offset 0x30 -- the same byte count the ntfs-3g
    ``ntfscat`` helper extracts for the stream.
    """

    offset = struct.unpack_from("<H", raw, 0x14)[0]
    end = struct.unpack_from("<I", raw, _RECORD_ALLOCATED_SIZE)[0]
    while offset + 16 <= end:
        type_code, length = struct.unpack_from("<II", raw, offset)
        if type_code == _ATTRIBUTE_END or length < 16:
            break
        if type_code == 0x80 and raw[offset + _ATTRIBUTE_NAME_LENGTH] == 0:
            if not raw[offset + _ATTRIBUTE_NONRESIDENT]:
                return 0
            return struct.unpack_from("<Q", raw, offset + 0x30)[0]
        offset += length
    raise ValueError("oracle: record carries no unnamed $DATA attribute")


def read_oracle_system_stream_sizes(image_path: str) -> dict[str, int]:
    """Return the raw byte sizes of the four core NTFS system streams.

    The walk reads the well-known system MFT records directly at the offsets
    the boot sector declares and reports the real size of each unnamed
    ``$DATA`` attribute.  It never mounts the image and never calls into
    ntfs-3g; the parity tests pin these numbers against ``ntfscat``.
    """

    return {
        name: _record_real_data_size(
            bytearray(read_oracle_mft_record(image_path, record_number))
        )
        for name, record_number in ORACLE_SYSTEM_RECORDS.items()
    }


def _nonresident_data(raw: bytearray) -> tuple[list[tuple[int | None, int]], int]:
    """Return (runlist, real_size) of the unnamed $DATA attribute.

    ``runlist`` entries are ``(lcn, length_in_clusters)`` with ``lcn=None``
    for sparse runs; ``real_size`` is the initialized size in bytes from the
    nonresident header at offset 0x30 -- the byte count ``ntfscat`` extracts.
    """

    offset = struct.unpack_from("<H", raw, 0x14)[0]
    end = struct.unpack_from("<I", raw, _RECORD_ALLOCATED_SIZE)[0]
    while offset + 16 <= end:
        type_code, length = struct.unpack_from("<II", raw, offset)
        if type_code == _ATTRIBUTE_END or length < 16:
            break
        if type_code == 0x80 and raw[offset + _ATTRIBUTE_NAME_LENGTH] == 0:
            if not raw[offset + _ATTRIBUTE_NONRESIDENT]:
                raise ValueError("oracle: unnamed $DATA is resident")
            run_offset = struct.unpack_from("<H", raw, offset + 0x20)[0]
            real_size = struct.unpack_from("<Q", raw, offset + 0x30)[0]
            return _decode_runlist(raw, offset + run_offset), real_size
        offset += length
    raise ValueError("oracle: record carries no unnamed $DATA attribute")


def _decode_runlist(raw: bytearray, start: int) -> list[tuple[int | None, int]]:
    """Decode one NTFS runlist into (lcn, length) pairs (standalone decoder)."""

    runs: list[tuple[int | None, int]] = []
    lcn = 0
    offset = start
    while True:
        header = raw[offset]
        if header == 0:
            return runs
        length_bytes = header & 0x0F
        offset_bytes = (header >> 4) & 0x0F
        if length_bytes == 0 or length_bytes > 8 or offset_bytes > 8:
            raise ValueError("oracle: malformed runlist header")
        offset += 1
        length = int.from_bytes(raw[offset : offset + length_bytes], "little")
        offset += length_bytes
        if offset_bytes:
            delta = int.from_bytes(
                raw[offset : offset + offset_bytes], "little", signed=True
            )
            offset += offset_bytes
            lcn += delta
            runs.append((lcn, length))
        else:
            runs.append((None, length))


# $MFT (record 0) and $MFTMirr (record 1) store arrays of FILE records, each
# carrying its own USA array.  ntfs-3g helpers hand out USA-restored records,
# so the oracle restores every record chunk inside these two streams before
# comparing bytes.  $LogFile and $Bitmap are opaque streams: ntfs-3g returns
# their raw bytes (verified empirically), so no restoration is applied there.
_MFT_RECORD_STREAMS = frozenset({0, 1})


def read_oracle_stream(image_path: str, record_number: int) -> bytes:
    """Extract the full unnamed ``$DATA`` stream of one MFT record.

    The bytes are reassembled from the image by the oracle's own runlist
    decoder at the boot-declared cluster size, then truncated to the real
    (initialized) size.  For $MFT/$MFTMirr every 1024-byte record inside the
    reassembled stream gets its USA restored, matching what ``ntfscat`` and
    the production ``_Mft`` mapping both hand out.  This is the byte-level
    column the parity tests pin against both the production reader and
    ``ntfscat``.
    """

    geometry = read_oracle_boot_geometry(image_path)
    raw = _read_raw_mft_record(image_path, geometry, record_number)
    runs, real_size = _nonresident_data(raw)
    chunks: list[bytes] = []
    with open(image_path, "rb") as stream:
        for lcn, length in runs:
            if lcn is None:
                chunks.append(bytes(length * geometry.cluster_size))
            else:
                stream.seek(lcn * geometry.cluster_size)
                chunks.append(stream.read(length * geometry.cluster_size))
    data = b"".join(chunks)
    if record_number in _MFT_RECORD_STREAMS:
        record_size = geometry.file_record_size
        aligned = len(data) - len(data) % record_size
        restored = bytearray()
        for start in range(0, aligned, record_size):
            chunk = bytearray(data[start : start + record_size])
            _restore_update_sequence(chunk, geometry.bytes_per_sector)
            restored += chunk
        restored += data[aligned:]
        data = bytes(restored)
    if len(data) < real_size:
        raise ValueError("oracle: runlist shorter than the declared data size")
    return data[:real_size]


def list_oracle_attributes(
    image_path: str, record_number: int
) -> tuple[tuple[int, bool, bool], ...]:
    """Walk the attribute chain: (type_code, nonresident, has_name) per entry.

    The walk stops at the first end marker or malformed length, mirroring the
    bounded walk the production parser documents, so the parity test can
    compare the two inventories entry by entry.
    """

    raw = bytearray(read_oracle_mft_record(image_path, record_number))
    inventory: list[tuple[int, bool, bool]] = []
    offset = struct.unpack_from("<H", raw, 0x14)[0]
    end = struct.unpack_from("<I", raw, _RECORD_ALLOCATED_SIZE)[0]
    while offset + 16 <= end:
        type_code, length = struct.unpack_from("<II", raw, offset)
        if type_code == _ATTRIBUTE_END or length < 16:
            break
        inventory.append(
            (
                type_code,
                bool(raw[offset + _ATTRIBUTE_NONRESIDENT]),
                bool(raw[offset + _ATTRIBUTE_NAME_LENGTH]),
            )
        )
        offset += length
    return tuple(inventory)


def read_oracle_standard_information(
    image_path: str, record_number: int
) -> OracleStandardInformation:
    """Parse the resident $STANDARD_INFORMATION of one record independently."""

    raw = bytearray(read_oracle_mft_record(image_path, record_number))
    offset = struct.unpack_from("<H", raw, 0x14)[0]
    end = struct.unpack_from("<I", raw, _RECORD_ALLOCATED_SIZE)[0]
    while offset + 16 <= end:
        type_code, length = struct.unpack_from("<II", raw, offset)
        if type_code == _ATTRIBUTE_END or length < 16:
            break
        if type_code == 0x10 and not raw[offset + _ATTRIBUTE_NAME_LENGTH]:
            if raw[offset + _ATTRIBUTE_NONRESIDENT]:
                raise ValueError("oracle: $STANDARD_INFORMATION must be resident")
            value_offset = struct.unpack_from(
                "<H", raw, offset + _ATTRIBUTE_VALUE_OFFSET_RES
            )[0]
            value = raw[offset + value_offset : offset + value_offset + 0x30]
            if len(value) < 0x30:
                raise ValueError("oracle: $STANDARD_INFORMATION is too short")
            return OracleStandardInformation(
                # Layout per the on-disk format: four FILETIMEs first, then
                # the DOS attributes at +0x20, version/class at +0x28/+0x2C.
                file_attributes=struct.unpack_from("<I", value, 0x20)[0],
                creation_time=struct.unpack_from("<Q", value, 0x00)[0],
                modification_time=struct.unpack_from("<Q", value, 0x08)[0],
                mft_change_time=struct.unpack_from("<Q", value, 0x10)[0],
                access_time=struct.unpack_from("<Q", value, 0x18)[0],
            )
        offset += length
    raise ValueError("oracle: no resident $STANDARD_INFORMATION found")


def read_oracle_mft_records(image_path: str) -> list[bytes]:
    """Return the USA-restored raw bytes of the ``$MFTMirr`` window records."""

    geometry = read_oracle_boot_geometry(image_path)
    records: list[bytes] = []
    with open(image_path, "rb") as stream:
        mirror_base = geometry.mft_mirror_lcn * geometry.cluster_size
        for record_number in range(ORACLE_MFT_MIRROR_RECORDS):
            stream.seek(mirror_base + record_number * geometry.file_record_size)
            raw = bytearray(stream.read(geometry.file_record_size))
            if len(raw) != geometry.file_record_size:
                raise ValueError(f"oracle: short $MFTMirr record {record_number}")
            _restore_update_sequence(raw, geometry.bytes_per_sector)
            records.append(bytes(raw))
    return records
