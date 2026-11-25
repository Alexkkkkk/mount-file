"""Build a synthetic but fslog.c-shaped NTFS crash fixture.

The fixture starts from a real ``mkntfs`` volume and a real ``ntfscp`` file,
then writes a JBD-style LFS log with a client restart record, the three
checkpoint tables, one committed (or active) resident-value update and two
valid RSTR restart pages.  ``$Volume`` is marked dirty so the production gate
treats the image exactly like an interrupted mount.  The log layout follows
``fs/ntfs3/fslog.c`` (RSTR/RCRD headers, USA, LFS_RECORD_HDR, LOG_REC_HDR,
RESTART_TABLE entries); it is not a byte-for-byte Windows capture.
"""

from __future__ import annotations

import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mountfile.ntfs_logfile import (
    CLIENT_REC,
    LFS_RECORD_HDR,
    LOG_REC_HDR,
    NTFS_RESTART,
    read_logfile_stream,
)
from mountfile.ntfs_mft import NtfsRecordError, parse_ntfs_file_record_header
from mountfile.ntfs_reader import (
    NtfsMetadataError,
    parse_ntfs_attribute_stream,
    parse_ntfs_file_name,
    probe_ntfs_metadata,
)
from mountfile.ntfs_replay import NtfsReplayError, _attribute_offset, _Mft

SECTOR = 512
PAGE = 4096
FIRST_PAGE = 0x22 * PAGE
MFT_RECORD_SIZE = 1024  # mkntfs default; checked against boot.file_record_size
NONRESIDENT_LOG_PATCH_SIZE = 64


@dataclass(frozen=True)
class CrashFixture:
    image: Path
    record_number: int
    record_size: int
    attribute_offset: int
    value_offset: int
    old_data: bytes
    new_data: bytes
    nonresident: bool
    log_size: int
    restart_lsn: int
    update_lsn: int


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{command}: {result.stderr or result.stdout}")
    return result


def _align8(value: int) -> int:
    return (value + 7) & ~7


def _apply_usa(page: bytearray) -> None:
    count = len(page) // SECTOR + 1
    struct.pack_into("<HH", page, 4, 0x1E, count)
    usn = 1
    struct.pack_into("<H", page, 0x1E, usn)
    for index in range(count - 1):
        trailer = (index + 1) * SECTOR - 2
        original = struct.unpack_from("<H", page, trailer)[0]
        struct.pack_into("<H", page, 0x1E + 2 * (index + 1), original)
        struct.pack_into("<H", page, trailer, usn)


def _lsn(vbo: int, file_data_bits: int, sequence: int = 1) -> int:
    return (vbo >> 3) | (sequence << file_data_bits)


def _log_body(
    redo_op: int,
    undo_op: int,
    redo_data: bytes,
    undo_data: bytes,
    *,
    target_attr: int,
    lcns: tuple[int, ...],
    record_off: int,
    attr_off: int,
    cluster_off: int,
    target_vcn: int,
) -> bytes:
    redo_off = LOG_REC_HDR.size + 8 * len(lcns)
    undo_off = _align8(redo_off + len(redo_data))
    length = _align8(undo_off + len(undo_data))
    body = bytearray(length)
    LOG_REC_HDR.pack_into(
        body,
        0,
        redo_op,
        undo_op,
        redo_off,
        len(redo_data),
        undo_off,
        len(undo_data),
        target_attr,
        len(lcns),
        record_off,
        attr_off,
        cluster_off,
        0,
        target_vcn,
    )
    for index, lcn in enumerate(lcns):
        struct.pack_into("<Q", body, LOG_REC_HDR.size + 8 * index, lcn)
    body[redo_off : redo_off + len(redo_data)] = redo_data
    body[undo_off : undo_off + len(undo_data)] = undo_data
    return bytes(body)


def _record(lsn: int, record_type: int, body: bytes, *, txid: int = 0) -> bytes:
    header = bytearray(LFS_RECORD_HDR.size)
    LFS_RECORD_HDR.pack_into(
        header, 0, lsn, 0, 0, len(body), 1, 0, record_type, txid, 0
    )
    return bytes(header) + body


def _table(entry_size: int, entry: bytes) -> bytes:
    header = struct.pack("<6I", entry_size, 1, 1, 0, 0xFFFFFFFF, 0xFFFFFFFF)
    return header + entry


def _open_table(record_reference: int, lsn: int) -> bytes:
    entry = bytearray(0x28)
    struct.pack_into("<III", entry, 0, 0, 0, 0x80)
    entry[0x0C] = 1
    struct.pack_into("<QQ", entry, 0x10, record_reference, lsn)
    return _table(0x28, bytes(entry))


def _dirty_table(lsn: int, lcn: int) -> bytes:
    # struct DIR_PAGE_ENTRY is 0x20 bytes plus one u64 per cluster-per-page.
    entry = bytearray(0x20 + 8 * 8)
    struct.pack_into("<III", entry, 4, 0, PAGE, 1)
    struct.pack_into("<QQ", entry, 0x10, 0, lsn)
    struct.pack_into("<Q", entry, 0x20, lcn)
    return _table(0x20 + 8 * 8, bytes(entry))


def _transaction_table(lsn: int, *, active: bool) -> bytes:
    entry = bytearray(0x28)
    struct.pack_into("<I", entry, 4, 1 if active else 3)
    struct.pack_into("<QQQ", entry, 8, lsn, 0, lsn if active else 0)
    struct.pack_into("<II", entry, 0x20, 1 if active else 0, 0)
    return _table(0x28, bytes(entry))


def _write_rstr(
    stream: bytearray,
    offset: int,
    *,
    current_lsn: int,
    restart_lsn: int,
    file_size: int,
) -> None:
    page = bytearray(PAGE)
    page[0:4] = b"RSTR"
    struct.pack_into("<II", page, 0x10, PAGE, PAGE)
    struct.pack_into("<H", page, 0x18, 0x30)
    struct.pack_into("<HH", page, 0x1A, 1, 1)
    ra = 0x30
    struct.pack_into("<Q", page, ra, current_lsn)
    struct.pack_into("<H", page, ra + 0x08, 1)
    struct.pack_into("<HHH", page, ra + 0x0A, 0xFFFF, 0, 0)
    seq_bits = 67 - file_size.bit_length()
    struct.pack_into("<H", page, ra + 0x10, seq_bits)
    struct.pack_into("<H", page, ra + 0x14, 0xD0)
    struct.pack_into("<H", page, ra + 0x16, 0x30)
    struct.pack_into("<Q", page, ra + 0x18, file_size)
    struct.pack_into("<I", page, ra + 0x20, 0)
    struct.pack_into("<HH", page, ra + 0x24, 0x30, 0x40)
    struct.pack_into("<I", page, ra + 0x28, 1)
    struct.pack_into("<HH", page, ra + 0x2C, 0xFFFF, 0)
    client = ra + 0x30
    CLIENT_REC.pack_into(
        page,
        client,
        restart_lsn,
        restart_lsn,
        0xFFFF,
        0xFFFF,
        1,
        8,
        "NTFS".encode("utf-16-le").ljust(128, b"\x00"),
    )
    _apply_usa(page)
    stream[offset : offset + PAGE] = page


def _write_rcrd(stream: bytearray, offset: int, records: list[bytes]) -> None:
    page = bytearray(PAGE)
    page[0:4] = b"RCRD"
    struct.pack_into("<II", page, 0x10, PAGE, PAGE)
    cursor = 0x40
    for record in records:
        page[cursor : cursor + len(record)] = record
        cursor = _align8(cursor + len(record))
    struct.pack_into("<H", page, 0x18, cursor)
    _apply_usa(page)
    stream[offset : offset + PAGE] = page


def _read_run_data(
    handle, attribute, cluster_size: int, offset: int, length: int
) -> bytes:
    result = bytearray()
    cursor = offset
    while len(result) < length:
        for run in attribute.runs:
            start = run.vcn * cluster_size
            end = start + run.length * cluster_size
            if start <= cursor < end:
                if run.lcn is None:
                    raise RuntimeError("fixture stream is sparse")
                handle.seek(run.lcn * cluster_size + (cursor - start))
                chunk = handle.read(min(end - cursor, length - len(result)))
                result += chunk
                cursor += len(chunk)
                break
        else:
            raise RuntimeError("fixture stream has a hole")
    return bytes(result)


def make_crash_fixture(
    tmp_path: Path, *, active: bool = False, nonresident: bool = False
) -> CrashFixture:
    image = tmp_path / "crash.img"
    with open(image, "wb") as handle:
        handle.truncate(64 << 20)
    _run(["mkntfs", "-q", "-F", "-s", "512", "-c", "4096", str(image)])
    payload = tmp_path / "payload.bin"
    old_data = (
        b"OLD-NTFS-NONRESIDENT-" + b"n" * 16348
        if nonresident
        else b"OLD-NTFS-RESIDENT-" + b"o" * 44
    )
    payload.write_bytes(old_data)
    _run(["ntfscp", str(image), str(payload), "/target"])

    profile = probe_ntfs_metadata(str(image), volume_size=os.path.getsize(image))
    boot = profile.boot
    mft_data = [a for a in profile.attributes if a.type_code == 0x80 and not a.name][0]
    with open(image, "r+b") as handle:
        mft = _Mft(handle, boot, mft_data, base_offset=0)
        record_number = None
        for candidate in range(4, mft.record_count):
            try:
                header = mft.read_record(candidate)
                attributes = parse_ntfs_attribute_stream(
                    header, allow_end_marker_padding=True
                )
            except (NtfsMetadataError, NtfsRecordError, NtfsReplayError):
                continue
            names = [
                parse_ntfs_file_name(attribute.resident_value).name
                for attribute in attributes
                if attribute.type_code == 0x30 and attribute.resident_value
            ]
            if "target" in names:
                record_number = candidate
                break
        if record_number is None:
            raise RuntimeError("the fixture file was not found in $MFT")
        header = mft.read_record(record_number)
        data_attribute = [
            attribute
            for attribute in parse_ntfs_attribute_stream(
                header, allow_end_marker_padding=True
            )
            if attribute.type_code == 0x80 and not attribute.name
        ][0]
        attribute_offset = _attribute_offset(header, data_attribute.attribute_id)
        value_offset = struct.unpack_from(
            "<H", header.fixed_record, attribute_offset + 20
        )[0]
        if nonresident:
            if data_attribute.resident_value is not None:
                raise RuntimeError(
                    "fixture requested nonresident but $DATA is resident"
                )
            old_data = _read_run_data(
                handle,
                data_attribute,
                boot.cluster_size,
                0,
                data_attribute.data_size,
            )
            new_data = b"NEW!" + old_data[4:]
            target_vcn = 0
            cluster_off = 0
            record_off = 0
            update_lcns = (data_attribute.runs[0].lcn,)
            update_op = 0x08
        else:
            old_data = bytes(data_attribute.resident_value or b"")
            new_data = b"NEW!" + old_data[4:]
            target_vcn = 0
            cluster_off = (record_number * boot.file_record_size) // SECTOR
            record_off = attribute_offset
            update_lcns = (mft_data.runs[0].lcn,)
            update_op = 0x07
        record_reference = record_number | (header.sequence_number << 48)
        mft_lcn = mft_data.runs[0].lcn
        assert mft_lcn is not None
        dirty_lcn = update_lcns[0]
        assert dirty_lcn is not None

        # Mark $Volume dirty exactly like an interrupted mount.
        volume = mft.read_record(3)
        volume_info = [
            attribute
            for attribute in parse_ntfs_attribute_stream(
                volume, allow_end_marker_padding=True
            )
            if attribute.type_code == 0x70
        ][0]
        volume_offset = _attribute_offset(volume, volume_info.attribute_id)
        fixed_volume = bytearray(volume.fixed_record)
        value = struct.unpack_from("<H", fixed_volume, volume_offset + 20)[0]
        flags = struct.unpack_from("<H", fixed_volume, volume_offset + value + 10)[0]
        struct.pack_into("<H", fixed_volume, volume_offset + value + 10, flags | 0x0001)
        mft.write_record(3, bytes(fixed_volume))

        log_stream, _cluster, logfile = read_logfile_stream(str(image))
        log_size = logfile.data_size
        file_data_bits = 64 - (67 - log_size.bit_length())
        stream = bytearray(b"\xff" * log_size)

        cursor = FIRST_PAGE + 0x40
        restart_lsn = _lsn(cursor, file_data_bits)
        restart_record = _record(
            restart_lsn,
            2,
            NTFS_RESTART.pack(2, 0, restart_lsn, 0, 0, 0, 0, 0, 0, 0, 0),
        )
        cursor = _align8(cursor + len(restart_record))
        open_lsn = _lsn(cursor, file_data_bits)
        open_record = _record(
            open_lsn,
            1,
            _log_body(
                0x1D,
                0,
                _open_table(record_reference, 0),
                b"",
                target_attr=0,
                lcns=(),
                record_off=0,
                attr_off=0,
                cluster_off=0,
                target_vcn=0,
            ),
        )
        cursor = _align8(cursor + len(open_record))
        dirty_lsn = _lsn(cursor, file_data_bits)
        dirty_record = _record(
            dirty_lsn,
            1,
            _log_body(
                0x1F,
                0,
                _dirty_table(0, dirty_lcn),
                b"",
                target_attr=0,
                lcns=(),
                record_off=0,
                attr_off=0,
                cluster_off=0,
                target_vcn=0,
            ),
        )
        cursor = _align8(cursor + len(dirty_record))
        update_lsn = _lsn(cursor, file_data_bits)
        transaction_record = _record(
            _lsn(cursor + 0x1000, file_data_bits),
            1,
            _log_body(
                0x20,
                0,
                _transaction_table(update_lsn, active=active),
                b"",
                target_attr=0,
                lcns=(),
                record_off=0,
                attr_off=0,
                cluster_off=0,
                target_vcn=0,
            ),
        )
        # The transaction dump LSN must be known before the restart record can
        # reference it; rebuild the restart record with the final table LSNs.
        transaction_lsn = struct.unpack_from("<Q", transaction_record, 0)[0]
        restart_record = _record(
            restart_lsn,
            2,
            NTFS_RESTART.pack(
                2,
                0,
                restart_lsn,
                open_lsn,
                0,
                dirty_lsn,
                transaction_lsn,
                len(_open_table(record_reference, 0)),
                0,
                len(_dirty_table(0, dirty_lcn)),
                len(_transaction_table(update_lsn, active=active)),
            ),
        )
        open_record = _record(
            open_lsn,
            1,
            _log_body(
                0x1D,
                0,
                _open_table(record_reference, update_lsn),
                b"",
                target_attr=0,
                lcns=(),
                record_off=0,
                attr_off=0,
                cluster_off=0,
                target_vcn=0,
            ),
        )
        dirty_record = _record(
            dirty_lsn,
            1,
            _log_body(
                0x1F,
                0,
                _dirty_table(update_lsn, dirty_lcn),
                b"",
                target_attr=0,
                lcns=(),
                record_off=0,
                attr_off=0,
                cluster_off=0,
                target_vcn=0,
            ),
        )
        update_record = _record(
            update_lsn,
            1,
            _log_body(
                update_op,
                update_op,
                # Keep the fixture's full logical contents for read-back
                # expectations, but log only the partial nonresident patch.
                new_data[:NONRESIDENT_LOG_PATCH_SIZE] if nonresident else new_data,
                old_data[:NONRESIDENT_LOG_PATCH_SIZE] if nonresident else old_data,
                target_attr=0,
                lcns=update_lcns,
                record_off=record_off,
                attr_off=0 if nonresident else value_offset,
                cluster_off=cluster_off,
                target_vcn=target_vcn,
            ),
            txid=0x18,
        )
        records = [
            restart_record,
            open_record,
            dirty_record,
            transaction_record,
            update_record,
        ]
        _write_rstr(
            stream,
            0,
            current_lsn=update_lsn,
            restart_lsn=restart_lsn,
            file_size=log_size,
        )
        _write_rstr(
            stream,
            PAGE,
            current_lsn=update_lsn,
            restart_lsn=restart_lsn,
            file_size=log_size,
        )
        _write_rcrd(stream, FIRST_PAGE, records)
        # Write the $LogFile DATA stream through its runlist.
        for run in logfile.runs:
            assert run.lcn is not None
            start = run.vcn * boot.cluster_size
            length = run.length * boot.cluster_size
            if start >= len(stream):
                continue
            handle.seek(run.lcn * boot.cluster_size)
            handle.write(stream[start : start + length])
    return CrashFixture(
        image=image,
        record_number=record_number,
        record_size=boot.file_record_size,
        attribute_offset=attribute_offset,
        value_offset=value_offset,
        old_data=old_data,
        new_data=new_data,
        nonresident=nonresident,
        log_size=log_size,
        restart_lsn=restart_lsn,
        update_lsn=update_lsn,
    )


def mark_volume_dirty(image: Path) -> None:
    """Set VOLUME_FLAG_DIRTY on a clean image without writing a $LogFile."""

    profile = probe_ntfs_metadata(str(image), volume_size=os.path.getsize(image))
    mft_data = [
        attribute
        for attribute in profile.attributes
        if attribute.type_code == 0x80 and not attribute.name
    ][0]
    with open(image, "r+b") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=0)
        volume = mft.read_record(3)
        volume_info = [
            attribute
            for attribute in parse_ntfs_attribute_stream(
                volume, allow_end_marker_padding=True
            )
            if attribute.type_code == 0x70
        ][0]
        offset = _attribute_offset(volume, volume_info.attribute_id)
        fixed = bytearray(volume.fixed_record)
        value = struct.unpack_from("<H", fixed, offset + 20)[0]
        flags = struct.unpack_from("<H", fixed, offset + value + 10)[0]
        struct.pack_into("<H", fixed, offset + value + 10, flags | 0x0001)
        mft.write_record(3, bytes(fixed))


def corrupt_mft_mirror(image: Path) -> None:
    """Overwrite $MFTMirr record 0 so the mirror check must repair it."""

    profile = probe_ntfs_metadata(str(image), volume_size=os.path.getsize(image))
    with open(image, "r+b") as handle:
        handle.seek(profile.boot.mft_mirror_offset)
        handle.write(b"\x00" * profile.boot.file_record_size)
