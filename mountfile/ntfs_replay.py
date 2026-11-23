# SPDX-License-Identifier: MIT
"""Filesystem-aware NTFS ``$LogFile`` replay executor.

The read-only parser in :mod:`mountfile.ntfs_logfile` proves the LFS record
layout but deliberately stops before byte targets.  This module adds the
filesystem-aware half of issue #510 block C:

* parse the restart-area tables (open attribute, dirty page, transaction);
* derive the redo start from the dirty-page/transaction tables, as fslog.c
  ``log_replay()`` does, rather than from the checkpoint LSN;
* resolve ``target_attr`` through the open attribute table and the MFT runlist;
* apply bounded redo and undo operations to a disposable copy;
* write the clean restart pages, clear the ``$Volume`` dirty flag and refresh
  ``$MFTMirr``;
* re-check the volume with the independent ``$LogFile`` gate.

Every target is validated against a USA-restored FILE record and its runlist.
Unsupported operations (index surgery, allocation of new MFT records outside
the existing stream, attribute resizing, sparse runs, ``$ATTRIBUTE_LIST``)
raise :class:`NtfsReplayError` before any byte is written.  The executor is
only called on a copy; publication stays with the atomic writer.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from mountfile.native_write import _fault
from mountfile.ntfs_logfile import (
    ATTR_DATA,
    LOG_REC_HDR,
    LogRecord,
    NtfsLogFileAnalysis,
    NtfsLogFileNeverWritten,
    NtfsReplayExecutorError,
    analyze_logfile,
    parse_restart_pages,
    read_logfile_stream,
)
from mountfile.ntfs_mft import (
    NtfsFileRecordHeader,
    NtfsRecordError,
    parse_ntfs_file_record_header,
)
from mountfile.ntfs_mft_allocation import NtfsMftAllocationError, plan_mft_record_reuse
from mountfile.ntfs_probe import NtfsBootProfile
from mountfile.ntfs_reader import (
    NtfsAttributeSummary,
    NtfsMetadataError,
    parse_ntfs_attribute_stream,
    probe_ntfs_metadata,
)

SECTOR_SIZE = 512
MFT_RECORD_VOLUME = 3
MFT_RECORD_MIRROR = 1
MFT_MIRROR_RECORDS = 4

NTFS_REPLAY_WRITE_FAULT_POINTS = (
    "ntfs:replay:mft_record:applied",
    "ntfs:replay:stream_write:applied",
    "ntfs:replay:restart_pages:applied",
    "ntfs:replay:volume_marker:applied",
    "ntfs:replay:mft_mirror:applied",
)

# NTFS attribute type codes used by the replay profile.
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0
ATTR_VOLUME_INFORMATION = 0x70

# fslog.c operation codes.
OP_NOOP = 0x00
OP_COMPENSATION = 0x01
OP_INITIALIZE_FILE_RECORD = 0x02
OP_DEALLOCATE_FILE_RECORD = 0x03
OP_WRITE_END_OF_FILE_RECORD = 0x04
OP_CREATE_ATTRIBUTE = 0x05
OP_DELETE_ATTRIBUTE = 0x06
OP_UPDATE_RESIDENT_VALUE = 0x07
OP_UPDATE_NONRESIDENT_VALUE = 0x08
OP_UPDATE_MAPPING_PAIRS = 0x09
OP_DELETE_DIRTY_CLUSTERS = 0x0A
OP_SET_NEW_ATTRIBUTE_SIZES = 0x0B
OP_ADD_INDEX_ENTRY_ROOT = 0x0C
OP_DELETE_INDEX_ENTRY_ROOT = 0x0D
OP_ADD_INDEX_ENTRY_ALLOCATION = 0x0E
OP_DELETE_INDEX_ENTRY_ALLOCATION = 0x0F
OP_WRITE_END_OF_INDEX_BUFFER = 0x10
OP_SET_INDEX_ENTRY_VCN_ROOT = 0x11
OP_SET_INDEX_ENTRY_VCN_ALLOCATION = 0x12
OP_UPDATE_FILE_NAME_ROOT = 0x13
OP_UPDATE_FILE_NAME_ALLOCATION = 0x14
OP_SET_BITS_IN_NONRESIDENT_BITMAP = 0x15
OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP = 0x16
OP_HOT_FIX = 0x17
OP_UPDATE_RECORD_DATA_ROOT = 0x21
OP_UPDATE_RECORD_DATA_ALLOCATION = 0x22
OP_UPDATE_RELATIVE_DATA_IN_INDEX = 0x23
OP_UPDATE_RELATIVE_DATA_IN_INDEX2 = 0x24
OP_ZERO_END_OF_FILE_RECORD = 0x25

SKIPPABLE = frozenset((OP_NOOP, OP_COMPENSATION))
# Operations that address an MFT record through ``vbo >> record_bits``.
MFT_OPERATIONS = frozenset(
    (
        OP_INITIALIZE_FILE_RECORD,
        OP_DEALLOCATE_FILE_RECORD,
        OP_WRITE_END_OF_FILE_RECORD,
        OP_CREATE_ATTRIBUTE,
        OP_DELETE_ATTRIBUTE,
        OP_UPDATE_RESIDENT_VALUE,
        OP_UPDATE_MAPPING_PAIRS,
        OP_SET_NEW_ATTRIBUTE_SIZES,
        OP_ADD_INDEX_ENTRY_ROOT,
        OP_DELETE_INDEX_ENTRY_ROOT,
        OP_SET_INDEX_ENTRY_VCN_ROOT,
        OP_UPDATE_FILE_NAME_ROOT,
        OP_UPDATE_RECORD_DATA_ROOT,
        OP_ZERO_END_OF_FILE_RECORD,
    )
)
# Operations that address an attribute stream through the open attribute table.
STREAM_OPERATIONS = frozenset(
    (
        OP_UPDATE_NONRESIDENT_VALUE,
        OP_ADD_INDEX_ENTRY_ALLOCATION,
        OP_DELETE_INDEX_ENTRY_ALLOCATION,
        OP_WRITE_END_OF_INDEX_BUFFER,
        OP_SET_INDEX_ENTRY_VCN_ALLOCATION,
        OP_UPDATE_FILE_NAME_ALLOCATION,
        OP_SET_BITS_IN_NONRESIDENT_BITMAP,
        OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
        OP_UPDATE_RECORD_DATA_ALLOCATION,
    )
)
_BITMAP_OPERATIONS = frozenset(
    (OP_SET_BITS_IN_NONRESIDENT_BITMAP, OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP)
)
_BITMAP_ATTRIBUTE_NAMES = frozenset(("$bitmap", "$i30"))
# The bounded executor implements these byte-level operations.  MFT record
# allocation/deallocation and bitmap mutation remain refused until the MFT
# allocation planner is wired in and independently checked; index entry
# surgery is also refused until it has an independent index checker.
SUPPORTED_OPERATIONS = frozenset(
    (
        OP_WRITE_END_OF_FILE_RECORD,
        OP_CREATE_ATTRIBUTE,
        OP_DELETE_ATTRIBUTE,
        OP_UPDATE_RESIDENT_VALUE,
        OP_UPDATE_NONRESIDENT_VALUE,
        OP_UPDATE_MAPPING_PAIRS,
        OP_SET_NEW_ATTRIBUTE_SIZES,
        OP_ZERO_END_OF_FILE_RECORD,
    )
)

# Durable phase boundaries exposed through the existing native-write fault seam.
# These names are intentionally stable so acceptance harnesses can inject a
# process kill without guessing internal function names.
NTFS_REPLAY_FAULT_POINTS = (
    "replay:redo:start",
    "replay:redo:done",
    "replay:undo:start",
    "replay:undo:done",
    "replay:journal:clean-pages",
    "replay:volume:dirty-flag",
    "replay:mirror:update",
    "replay:flush",
)


class NtfsReplayError(NtfsReplayExecutorError):
    """The replay cannot be proven safe for this bounded NTFS profile."""


@dataclass(frozen=True)
class OpenAttributeEntry:
    index: int
    next_index: int
    bytes_per_index: int
    type_code: int
    is_dirty: bool
    reference: int
    lsn: int
    record_number: int

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "type": self.type_code,
            "record": self.record_number,
            "lsn": self.lsn,
        }


@dataclass(frozen=True)
class DirtyPageEntry:
    index: int
    target_attr: int
    transfer_len: int
    lcns_follow: int
    vcn: int
    oldest_lsn: int
    page_lcns: tuple[int, ...]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "target_attr": self.target_attr,
            "vcn": self.vcn,
            "lcns": self.lcns_follow,
            "oldest_lsn": self.oldest_lsn,
        }


@dataclass(frozen=True)
class TransactionEntry:
    index: int
    state: int
    first_lsn: int
    prev_lsn: int
    undo_next_lsn: int
    undo_records: int
    undo_len: int

    @property
    def active(self) -> bool:
        return self.state == 1 and self.undo_next_lsn != 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "state": self.state,
            "first_lsn": self.first_lsn,
            "undo_next_lsn": self.undo_next_lsn,
        }


@dataclass(frozen=True)
class NtfsRestartTables:
    open_attributes: tuple[OpenAttributeEntry, ...]
    dirty_pages: tuple[DirtyPageEntry, ...]
    transactions: tuple[TransactionEntry, ...]
    open_attribute_lsn: int
    dirty_page_lsn: int
    transaction_lsn: int

    def as_dict(self) -> dict:
        return {
            "open_attributes": [entry.as_dict() for entry in self.open_attributes],
            "dirty_pages": [entry.as_dict() for entry in self.dirty_pages],
            "transactions": [entry.as_dict() for entry in self.transactions],
            "open_attribute_lsn": self.open_attribute_lsn,
            "dirty_page_lsn": self.dirty_page_lsn,
            "transaction_lsn": self.transaction_lsn,
        }


@dataclass(frozen=True)
class NtfsReplayPlan:
    redo_start: int
    redo: tuple[LogRecord, ...]
    undo: tuple[LogRecord, ...]
    skipped_redo: tuple[int, ...]
    skipped_undo: tuple[int, ...]

    @property
    def empty(self) -> bool:
        return not self.redo and not self.undo

    def as_dict(self) -> dict:
        return {
            "redo_start": self.redo_start,
            "redo": [record.lsn for record in self.redo],
            "undo": [record.lsn for record in self.undo],
            "skipped_redo": list(self.skipped_redo),
            "skipped_undo": list(self.skipped_undo),
        }


@dataclass(frozen=True)
class NtfsReplayResult:
    changed: bool
    redo_applied: int
    undo_applied: int
    redo_start: int
    records_written: int
    stream_writes: int
    clean_restart_pages: int
    volume_flags_before: int
    volume_flags_after: int
    mirror_updated: bool
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "changed": self.changed,
            "redo_applied": self.redo_applied,
            "undo_applied": self.undo_applied,
            "redo_start": self.redo_start,
            "records_written": self.records_written,
            "stream_writes": self.stream_writes,
            "clean_restart_pages": self.clean_restart_pages,
            "volume_flags_before": self.volume_flags_before,
            "volume_flags_after": self.volume_flags_after,
            "mirror_updated": self.mirror_updated,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------- restart tables


def _restart_table_body(
    stream: bytes, analysis: NtfsLogFileAnalysis, lsn: int
) -> bytes:
    record = analysis.record_at(lsn)
    if record is None:
        raise NtfsReplayError(
            f"restart table LSN 0x{lsn:x} is not present in the parsed log"
        )
    if not record.is_client_record:
        raise NtfsReplayError(f"restart table LSN 0x{lsn:x} is not a client record")
    start = record.byte_offset + analysis.restart.log_record_header_length
    end = start + record.data_length
    if start < 0 or end > len(stream):
        raise NtfsReplayError(
            f"restart table LSN 0x{lsn:x} body is outside the $LogFile stream"
        )
    body = stream[start:end]
    if len(body) < LOG_REC_HDR.size:
        raise NtfsReplayError(
            f"restart table LSN 0x{lsn:x} body is shorter than LOG_REC_HDR"
        )
    redo_op, _undo_op, redo_off, redo_len = struct.unpack_from("<HHHH", body, 0)
    if redo_op not in (0x1D, 0x1F, 0x20):
        raise NtfsReplayError(
            f"restart table LSN 0x{lsn:x} carries operation 0x{redo_op:02x}, "
            "not a table dump"
        )
    if redo_off < LOG_REC_HDR.size or redo_off + redo_len > len(body):
        raise NtfsReplayError(
            f"restart table LSN 0x{lsn:x} redo slice is outside its record"
        )
    return body[redo_off : redo_off + redo_len]


def _parse_restart_table(
    body: bytes, *, minimum_entry_size: int, table_name: str
) -> tuple[bytes, int, int]:
    if len(body) < 0x18:
        raise NtfsReplayError(f"{table_name} dump is shorter than its header")
    size, used, total, _free_goal, _first_free, _last_free = struct.unpack_from(
        "<6I", body, 0
    )
    if size < minimum_entry_size:
        raise NtfsReplayError(
            f"{table_name} entry size {size} is smaller than its header "
            f"({minimum_entry_size})"
        )
    if total > 0x10000 or used > total:
        raise NtfsReplayError(f"{table_name} table geometry is invalid")
    if 0x18 + total * size > len(body):
        raise NtfsReplayError(f"{table_name} table exceeds its dump record")
    return body[0x18:], size, total


def _parse_open_attribute_table(body: bytes) -> tuple[OpenAttributeEntry, ...]:
    entries, size, total = _parse_restart_table(
        body, minimum_entry_size=0x28, table_name="open attribute table"
    )
    result: list[OpenAttributeEntry] = []
    for index in range(total):
        entry = entries[index * size : (index + 1) * size]
        if entry[:4] == b"\xff\xff\xff\xff":
            continue
        next_index, bytes_per_index, type_code = struct.unpack_from("<III", entry, 0)
        is_dirty = entry[0x0C]
        reference, lsn = struct.unpack_from("<QQ", entry, 0x10)
        record_number = reference & ((1 << 48) - 1)
        if record_number == 0 or reference >> 48 == 0:
            raise NtfsReplayError("open attribute entry has a zero record reference")
        result.append(
            OpenAttributeEntry(
                index=index,
                next_index=next_index,
                bytes_per_index=bytes_per_index,
                type_code=type_code,
                is_dirty=bool(is_dirty),
                reference=reference,
                lsn=lsn,
                record_number=record_number,
            )
        )
    return tuple(result)


def _parse_dirty_page_table(body: bytes) -> tuple[DirtyPageEntry, ...]:
    entries, size, total = _parse_restart_table(
        body, minimum_entry_size=0x20, table_name="dirty page table"
    )
    if (size - 0x20) % 8:
        raise NtfsReplayError("dirty page entry size is not 8-byte aligned")
    capacity = (size - 0x20) // 8
    result: list[DirtyPageEntry] = []
    for index in range(total):
        entry = entries[index * size : (index + 1) * size]
        if entry[:4] == b"\xff\xff\xff\xff":
            continue
        target_attr, transfer_len, lcns_follow = struct.unpack_from("<III", entry, 4)
        vcn, oldest_lsn = struct.unpack_from("<QQ", entry, 0x10)
        if lcns_follow > capacity or 0x20 + lcns_follow * 8 > len(entry):
            raise NtfsReplayError("dirty page entry LCN count is outside its record")
        lcns = struct.unpack_from(f"<{lcns_follow}Q", entry, 0x20)
        result.append(
            DirtyPageEntry(
                index=index,
                target_attr=target_attr,
                transfer_len=transfer_len,
                lcns_follow=lcns_follow,
                vcn=vcn,
                oldest_lsn=oldest_lsn,
                page_lcns=tuple(lcns),
            )
        )
    return tuple(result)


def _parse_transaction_table(body: bytes) -> tuple[TransactionEntry, ...]:
    entries, size, total = _parse_restart_table(
        body, minimum_entry_size=0x28, table_name="transaction table"
    )
    result: list[TransactionEntry] = []
    for index in range(total):
        entry = entries[index * size : (index + 1) * size]
        if entry[:4] == b"\xff\xff\xff\xff":
            continue
        state = struct.unpack_from("<I", entry, 4)[0]
        if state > 3:
            raise NtfsReplayError(
                f"transaction entry {index} has invalid state {state}"
            )
        first_lsn, prev_lsn, undo_next = struct.unpack_from("<QQQ", entry, 8)
        undo_records, undo_len = struct.unpack_from("<II", entry, 0x20)
        result.append(
            TransactionEntry(
                index=index,
                state=state,
                first_lsn=first_lsn,
                prev_lsn=prev_lsn,
                undo_next_lsn=undo_next,
                undo_records=undo_records,
                undo_len=undo_len,
            )
        )
    return tuple(result)


def parse_restart_tables(
    stream: bytes, analysis: NtfsLogFileAnalysis
) -> NtfsRestartTables:
    """Parse the three checkpoint tables referenced by the restart area."""

    restart = analysis.restart_area
    if restart is None:
        raise NtfsReplayError("$LogFile has no client restart record")
    open_body = _restart_table_body(stream, analysis, restart.open_attribute_table_lsn)
    dirty_body = _restart_table_body(stream, analysis, restart.dirty_page_table_lsn)
    transaction_body = _restart_table_body(
        stream, analysis, restart.transaction_table_lsn
    )
    return NtfsRestartTables(
        open_attributes=_parse_open_attribute_table(open_body),
        dirty_pages=_parse_dirty_page_table(dirty_body),
        transactions=_parse_transaction_table(transaction_body),
        open_attribute_lsn=restart.open_attribute_table_lsn,
        dirty_page_lsn=restart.dirty_page_table_lsn,
        transaction_lsn=restart.transaction_table_lsn,
    )


# ---------------------------------------------------------------- replay plan


def _find_dirty_page(
    tables: NtfsRestartTables, target_attr: int, vcn: int
) -> DirtyPageEntry | None:
    for page in tables.dirty_pages:
        if (
            page.target_attr == target_attr
            and vcn >= page.vcn
            and vcn - page.vcn < max(1, page.lcns_follow)
        ):
            return page
    return None


def plan_ntfs_replay(
    analysis: NtfsLogFileAnalysis, tables: NtfsRestartTables
) -> NtfsReplayPlan:
    """Derive redo and undo records from the checkpoint tables.

    This mirrors fslog.c ``log_replay()``: the redo start is the smallest
    nonzero ``oldest_lsn``/``first_lsn`` in the dirty-page and transaction
    tables, and a record is replayed only when its ``target_attr``/``vcn``
    match a dirty page whose ``oldest_lsn`` is not newer than the record.
    """

    if not tables.dirty_pages and not tables.transactions:
        return NtfsReplayPlan(0, (), (), (), ())

    candidates = [page.oldest_lsn for page in tables.dirty_pages if page.oldest_lsn] + [
        transaction.first_lsn
        for transaction in tables.transactions
        if transaction.first_lsn
    ]
    redo_start = min(candidates) if candidates else 0

    redo: list[LogRecord] = []
    skipped_redo: list[int] = []
    for record in analysis.records:
        if record.lsn < redo_start:
            continue
        if not record.is_client_record or record.lcns_follow == 0:
            continue
        page = _find_dirty_page(tables, record.target_attr, record.target_vcn)
        if page is None or record.lsn < page.oldest_lsn:
            continue
        if record.redo_op in SKIPPABLE or not record.redo_data:
            continue
        redo.append(record)

    undo: list[LogRecord] = []
    skipped_undo: list[int] = []
    for transaction in tables.transactions:
        if not transaction.active:
            continue
        lsn = transaction.undo_next_lsn
        seen: set[int] = set()
        while lsn and lsn not in seen:
            seen.add(lsn)
            record = analysis.record_at(lsn)
            if record is None:
                raise NtfsReplayError(
                    f"undo chain of transaction {transaction.index} points at "
                    f"missing LSN 0x{lsn:x}"
                )
            if record.undo_op in SKIPPABLE or not record.undo_data:
                skipped_undo.append(record.lsn)
            else:
                undo.append(record)
            lsn = record.undo_next_lsn
    return NtfsReplayPlan(
        redo_start=redo_start,
        redo=tuple(redo),
        undo=tuple(undo),
        skipped_redo=tuple(skipped_redo),
        skipped_undo=tuple(skipped_undo),
    )


# ---------------------------------------------------------------- MFT access


def _apply_usa(fixed: bytes) -> bytes:
    """Convert a USA-restored FILE record back to its on-disk form."""

    raw = bytearray(fixed)
    if len(raw) < SECTOR_SIZE or len(raw) % SECTOR_SIZE:
        raise NtfsReplayError("FILE record size is not a 512-byte multiple")
    fixup_offset, fixup_count = struct.unpack_from("<HH", raw, 0x04)
    expected = len(raw) // SECTOR_SIZE + 1
    if fixup_count != expected or fixup_offset < 0x2A:
        raise NtfsReplayError("FILE record USA header is invalid")
    usn = struct.unpack_from("<H", raw, fixup_offset)[0]
    for index in range(fixup_count - 1):
        trailer = (index + 1) * SECTOR_SIZE - 2
        original = struct.unpack_from("<H", raw, trailer)[0]
        struct.pack_into("<H", raw, fixup_offset + 2 * (index + 1), original)
        struct.pack_into("<H", raw, trailer, usn)
    return bytes(raw)


def _record_attribute(
    header: NtfsFileRecordHeader, type_code: int, *, unique: bool = True
) -> tuple[int, NtfsAttributeSummary]:
    attributes = parse_ntfs_attribute_stream(header, allow_end_marker_padding=True)
    matches = [
        attribute for attribute in attributes if attribute.type_code == type_code
    ]
    if not matches:
        raise NtfsReplayError(
            f"FILE record {header.fixed_record[:0]} has no attribute 0x{type_code:x}"
        )
    if unique and len(matches) != 1:
        raise NtfsReplayError(
            f"FILE record has {len(matches)} attributes of type 0x{type_code:x}; "
            "named attribute disambiguation is outside the replay profile"
        )
    return attributes.index(matches[0]), matches[0]


def _attribute_offset(header: NtfsFileRecordHeader, attribute_id: int) -> int:
    raw = header.fixed_record
    position = header.first_attribute_offset
    limit = header.bytes_in_use
    while position + 4 <= limit:
        if struct.unpack_from("<I", raw, position)[0] == 0xFFFFFFFF:
            break
        if position + 16 > limit:
            raise NtfsReplayError("attribute header is truncated")
        length = struct.unpack_from("<I", raw, position + 4)[0]
        if length < 24 or position + length > limit:
            raise NtfsReplayError("attribute bounds are invalid")
        identifier = struct.unpack_from("<H", raw, position + 14)[0]
        if identifier == attribute_id:
            return position
        position += length
    raise NtfsReplayError(f"attribute id {attribute_id} is not present in the record")


def _attribute_id_at(header: NtfsFileRecordHeader, offset: int) -> int:
    raw = header.fixed_record
    if offset < header.first_attribute_offset or offset + 16 > header.bytes_in_use:
        raise NtfsReplayError("attribute offset is outside the record")
    length = struct.unpack_from("<I", raw, offset + 4)[0]
    if length < 24 or offset + length > header.bytes_in_use:
        raise NtfsReplayError("attribute bounds are invalid")
    return struct.unpack_from("<H", raw, offset + 14)[0]


class _Mft:
    """Bounded read/write access to ``$MFT`` through record 0's DATA runlist."""

    def __init__(
        self,
        stream,
        boot: NtfsBootProfile,
        mft_data: NtfsAttributeSummary,
        *,
        base_offset: int,
    ) -> None:
        self._stream = stream
        self.boot = boot
        self.mft_data = mft_data
        self.base_offset = base_offset
        self.record_size = boot.file_record_size
        if not mft_data.nonresident or not mft_data.runs:
            raise NtfsReplayError("$MFT DATA stream is not a bounded runlist")
        if mft_data.data_size <= 0:
            raise NtfsReplayError("$MFT DATA stream is empty")
        self.record_count = mft_data.data_size // self.record_size
        if self.record_count < MFT_MIRROR_RECORDS:
            raise NtfsReplayError("$MFT is smaller than the mirror window")

    def _map(self, stream_offset: int, length: int) -> list[tuple[int, int, int]]:
        if stream_offset < 0 or length <= 0:
            raise NtfsReplayError("$MFT range is invalid")
        end = stream_offset + length
        if end > self.mft_data.data_size:
            raise NtfsReplayError("$MFT range exceeds the DATA stream")
        segments: list[tuple[int, int, int]] = []
        cursor = stream_offset
        while cursor < end:
            found = False
            for run in self.mft_data.runs:
                run_start = run.vcn * self.boot.cluster_size
                run_end = run_start + run.length * self.boot.cluster_size
                if run_start <= cursor < run_end:
                    if run.lcn is None:
                        raise NtfsReplayError("$MFT runlist is sparse")
                    available = min(end, run_end) - cursor
                    physical = (
                        self.base_offset
                        + run.lcn * self.boot.cluster_size
                        + (cursor - run_start)
                    )
                    segments.append((cursor, physical, available))
                    cursor += available
                    found = True
                    break
            if not found:
                raise NtfsReplayError("$MFT runlist has a hole")
        return segments

    def read(self, stream_offset: int, length: int) -> bytes:
        segments = list(self._map(stream_offset, length))
        result = bytearray(sum(size for _offset, _physical, size in segments))
        view = memoryview(result)
        position = 0
        for _offset, physical, size in segments:
            self._stream.seek(physical)
            chunk = self._stream.read(size)
            if len(chunk) != size:
                raise NtfsReplayError("short read from $MFT")
            view[position : position + size] = chunk
            position += size
        return bytes(result)

    def write(self, stream_offset: int, data: bytes) -> None:
        payload = bytes(data)
        for offset, physical, size in self._map(stream_offset, len(payload)):
            chunk = payload[offset - stream_offset : offset - stream_offset + size]
            self._stream.seek(physical)
            written = self._stream.write(chunk)
            if written != size:
                raise NtfsReplayError("short write to $MFT")

    def read_record_raw(self, record_number: int) -> bytes:
        if record_number < 0 or record_number >= self.record_count:
            raise NtfsReplayError(f"MFT record {record_number} is outside $MFT")
        return self.read(record_number * self.record_size, self.record_size)

    def read_record(self, record_number: int) -> NtfsFileRecordHeader:
        try:
            return parse_ntfs_file_record_header(self.read_record_raw(record_number))
        except NtfsRecordError as exc:
            raise NtfsReplayError(
                f"MFT record {record_number} is invalid: {exc}"
            ) from exc

    def write_record(self, record_number: int, fixed_record: bytes) -> None:
        if len(fixed_record) != self.record_size:
            raise NtfsReplayError("modified FILE record has the wrong size")
        raw = _apply_usa(fixed_record)
        try:
            probe = bytearray(raw)
            flags = struct.unpack_from("<H", probe, 0x16)[0]
            if not flags & 0x0001:
                # DeallocateFileRecordSegment clears IN_USE; the header parser
                # intentionally accepts only in-use records, so validate the
                # structure with the bit restored and write the cleared form.
                struct.pack_into("<H", probe, 0x16, flags | 0x0001)
                parse_ntfs_file_record_header(bytes(probe))
            else:
                parse_ntfs_file_record_header(raw)
        except NtfsRecordError as exc:
            raise NtfsReplayError(f"modified FILE record is invalid: {exc}") from exc
        self.write(record_number * self.record_size, raw)


# ---------------------------------------------------------------- operation helpers


def _check_bounds(record: NtfsFileRecordHeader, offset: int, length: int) -> None:
    if offset < 0 or length < 0 or offset + length > record.bytes_in_use:
        raise NtfsReplayError("replay range is outside the FILE record used area")


def _set_record_lsn(record: bytearray, lsn: int) -> None:
    struct.pack_into("<Q", record, 0x08, lsn)


def _apply_mft_operation(
    header: NtfsFileRecordHeader,
    record: LogRecord,
    payload: bytes,
    *,
    redo: bool,
) -> tuple[bytes, str]:
    """Apply one MFT-record operation and return the modified fixed record."""

    fixed = bytearray(header.fixed_record)
    op = record.redo_op if redo else record.undo_op
    roff = record.record_off
    aoff = record.attr_off
    dlen = len(payload)
    if op in (OP_INITIALIZE_FILE_RECORD, OP_DEALLOCATE_FILE_RECORD):
        if op == OP_INITIALIZE_FILE_RECORD:
            if roff + dlen > len(fixed):
                raise NtfsReplayError("InitializeFileRecordSegment exceeds the record")
            fixed[roff : roff + dlen] = payload
        else:
            flags = struct.unpack_from("<H", fixed, 0x16)[0]
            struct.pack_into("<H", fixed, 0x16, flags & ~0x0001)
            sequence = struct.unpack_from("<H", fixed, 0x10)[0]
            struct.pack_into("<H", fixed, 0x10, (sequence + 1) & 0xFFFF)
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_WRITE_END_OF_FILE_RECORD:
        if roff + dlen > len(fixed):
            raise NtfsReplayError("WriteEndOfFileRecordSegment exceeds the record")
        fixed[roff : roff + dlen] = payload
        struct.pack_into("<I", fixed, 0x18, (roff + dlen + 7) & ~7)
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_CREATE_ATTRIBUTE:
        if roff + dlen > len(fixed):
            raise NtfsReplayError("CreateAttribute exceeds the record")
        used = struct.unpack_from("<I", fixed, 0x18)[0]
        if dlen > len(fixed) - used:
            raise NtfsReplayError("CreateAttribute does not fit in the record")
        fixed[roff + dlen : used + dlen] = fixed[roff:used]
        fixed[roff : roff + dlen] = payload
        struct.pack_into("<I", fixed, 0x18, used + dlen)
        attribute_id = struct.unpack_from("<H", payload, 14)[0]
        next_id = struct.unpack_from("<H", fixed, 0x28)[0]
        if next_id <= attribute_id:
            struct.pack_into("<H", fixed, 0x28, attribute_id + 1)
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_DELETE_ATTRIBUTE:
        used = struct.unpack_from("<I", fixed, 0x18)[0]
        _check_bounds(header, roff, 24)
        size = struct.unpack_from("<I", fixed, roff + 4)[0]
        if size > used - roff:
            raise NtfsReplayError("DeleteAttribute exceeds the record used area")
        fixed[roff : used - size] = fixed[roff + size : used]
        fixed[used - size : used] = b"\x00" * size
        struct.pack_into("<I", fixed, 0x18, used - size)
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_UPDATE_RESIDENT_VALUE:
        _check_bounds(header, roff, 24)
        attribute_size = struct.unpack_from("<I", fixed, roff + 4)[0]
        if attribute_size < 24 or roff + attribute_size > len(fixed):
            raise NtfsReplayError("resident attribute bounds are invalid")
        if struct.unpack_from("<B", fixed, roff + 8)[0] != 0:
            raise NtfsReplayError("UpdateResidentValue target is nonresident")
        value_offset = struct.unpack_from("<H", fixed, roff + 20)[0]
        if aoff < value_offset or aoff + dlen > attribute_size:
            raise NtfsReplayError("resident value range is outside the attribute")
        if record.redo_len != record.undo_len:
            raise NtfsReplayError(
                "resident value resizing is outside the bounded replay profile"
            )
        fixed[roff + aoff : roff + aoff + dlen] = payload
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_UPDATE_MAPPING_PAIRS:
        _check_bounds(header, roff, 24)
        attribute_size = struct.unpack_from("<I", fixed, roff + 4)[0]
        if struct.unpack_from("<B", fixed, roff + 8)[0] == 0:
            raise NtfsReplayError("UpdateMappingPairs target is resident")
        run_offset = struct.unpack_from("<H", fixed, roff + 0x20)[0]
        if aoff < run_offset or aoff + dlen > attribute_size:
            raise NtfsReplayError("mapping-pairs range is outside the attribute")
        if record.redo_len != record.undo_len:
            raise NtfsReplayError(
                "mapping-pairs resizing is outside the bounded replay profile"
            )
        fixed[roff + aoff : roff + aoff + dlen] = payload
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_SET_NEW_ATTRIBUTE_SIZES:
        _check_bounds(header, roff, 24)
        if struct.unpack_from("<B", fixed, roff + 8)[0] == 0:
            raise NtfsReplayError("SetNewAttributeSizes target is resident")
        if dlen < 32:
            raise NtfsReplayError("SetNewAttributeSizes payload is truncated")
        for field, offset in ((0x28, 0), (0x30, 8), (0x38, 16), (0x40, 24)):
            struct.pack_into(
                "<Q", fixed, roff + field, struct.unpack_from("<Q", payload, offset)[0]
            )
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    if op == OP_ZERO_END_OF_FILE_RECORD:
        if roff + dlen > len(fixed):
            raise NtfsReplayError("ZeroEndOfFileRecord exceeds the record")
        fixed[roff : roff + dlen] = b"\x00" * dlen
        _set_record_lsn(fixed, record.lsn)
        return bytes(fixed), "record"

    raise NtfsReplayError(f"operation 0x{op:02x} has no MFT-record handler")


def _run_segments(
    attribute: NtfsAttributeSummary,
    cluster_size: int,
    stream_offset: int,
    length: int,
    base_offset: int,
) -> list[tuple[int, int, int]]:
    if not attribute.nonresident or not attribute.runs:
        raise NtfsReplayError("stream operation requires a nonresident runlist")
    if stream_offset < 0 or length <= 0:
        raise NtfsReplayError("stream range is invalid")
    if stream_offset + length > (attribute.data_size or attribute.allocated_size or 0):
        raise NtfsReplayError("stream range exceeds the attribute data size")
    segments: list[tuple[int, int, int]] = []
    cursor = stream_offset
    end = stream_offset + length
    while cursor < end:
        found = False
        for run in attribute.runs:
            run_start = run.vcn * cluster_size
            run_end = run_start + run.length * cluster_size
            if run_start <= cursor < run_end:
                if run.lcn is None:
                    raise NtfsReplayError("sparse stream runs are unsupported")
                available = min(end, run_end) - cursor
                physical = base_offset + run.lcn * cluster_size + (cursor - run_start)
                segments.append((cursor, physical, available))
                cursor += available
                found = True
                break
        if not found:
            raise NtfsReplayError("stream runlist has a hole")
    return segments


def _apply_stream_operation(
    stream,
    header: NtfsFileRecordHeader,
    attribute: NtfsAttributeSummary,
    record: LogRecord,
    payload: bytes,
    *,
    cluster_size: int,
    base_offset: int,
    redo: bool,
) -> int:
    op = record.redo_op if redo else record.undo_op
    if op in _BITMAP_OPERATIONS:
        if attribute.type_code != ATTR_BITMAP:
            raise NtfsReplayError("bitmap operation targets a non-bitmap attribute")
        if attribute.name.casefold() not in _BITMAP_ATTRIBUTE_NAMES:
            raise NtfsReplayError(
                "bitmap operation targets an unrecognized bitmap stream"
            )
    cluster_bits = cluster_size.bit_length() - 1
    if (1 << cluster_bits) != cluster_size:
        raise NtfsReplayError("cluster size is not a power of two")
    vbo = (record.target_vcn << cluster_bits) + (record.cluster_off << 9)
    roff = record.record_off
    dlen = len(payload)
    if op == OP_UPDATE_NONRESIDENT_VALUE:
        length = roff + dlen
        segments = _run_segments(attribute, cluster_size, vbo, length, base_offset)
        buffer = bytearray(sum(size for _offset, _physical, size in segments))
        view = memoryview(buffer)
        position = 0
        for _offset, physical, size in segments:
            stream.seek(physical)
            chunk = stream.read(size)
            if len(chunk) != size:
                raise NtfsReplayError("short read from nonresident stream")
            view[position : position + size] = chunk
            position += size
        buffer[roff : roff + dlen] = payload
        cursor = 0
        for _offset, physical, size in segments:
            stream.seek(physical)
            written = stream.write(buffer[cursor : cursor + size])
            if written != size:
                raise NtfsReplayError("short write to nonresident stream")
            cursor += size
        return 1
    if op in (OP_SET_BITS_IN_NONRESIDENT_BITMAP, OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP):
        if dlen < 8:
            raise NtfsReplayError("bitmap range payload is truncated")
        bitmap_off, bits = struct.unpack_from("<II", payload, 0)
        byte_start = bitmap_off // 8
        byte_end = (bitmap_off + bits + 7) // 8
        if byte_end <= byte_start:
            return 0
        range_offset = vbo + roff + byte_start
        segments = _run_segments(
            attribute, cluster_size, range_offset, byte_end - byte_start, base_offset
        )
        set_bit = op == OP_SET_BITS_IN_NONRESIDENT_BITMAP
        buffer = bytearray(sum(size for _offset, _physical, size in segments))
        view = memoryview(buffer)
        position = 0
        for _offset, physical, size in segments:
            stream.seek(physical)
            chunk = stream.read(size)
            if len(chunk) != size:
                raise NtfsReplayError("short read from bitmap stream")
            view[position : position + size] = chunk
            position += size
        for bit in range(bitmap_off, bitmap_off + bits):
            # ``buffer`` starts at byte_start, not at the requested bit. Keep
            # the bit's intra-byte offset while preserving adjacent bits.
            position = bit - byte_start * 8
            if set_bit:
                buffer[position // 8] |= 1 << (position % 8)
            else:
                buffer[position // 8] &= ~(1 << (position % 8))
        cursor = 0
        for _offset, physical, size in segments:
            stream.seek(physical)
            written = stream.write(buffer[cursor : cursor + size])
            if written != size:
                raise NtfsReplayError("short write to bitmap stream")
            cursor += size
        return 1
    raise NtfsReplayError(f"operation 0x{op:02x} has no stream handler")


# ---------------------------------------------------------------- clean markers


def _clean_restart_page(fixed_page: bytes, analysis: NtfsLogFileAnalysis) -> bytes:
    page_size = analysis.restart.log_page_size
    if len(fixed_page) != page_size:
        raise NtfsReplayError("restart page has the wrong size")
    page = bytearray(page_size)
    page[0:4] = b"RSTR"
    fixup_count = page_size // SECTOR_SIZE + 1
    struct.pack_into("<HH", page, 4, 0x1E, fixup_count)
    struct.pack_into("<I", page, 0x10, page_size)
    struct.pack_into("<I", page, 0x14, page_size)
    ra_off = (0x1E + 2 * fixup_count + 7) & ~7
    struct.pack_into("<H", page, 0x18, ra_off)
    struct.pack_into("<HH", page, 0x1A, 1, 1)
    source = analysis.restart
    if source.restart_offset + source.restart_length > len(fixed_page):
        raise NtfsReplayError("restart area is outside the restart page")
    raw_area = fixed_page[
        source.restart_offset : source.restart_offset + source.restart_length
    ]
    page[ra_off : ra_off + len(raw_area)] = raw_area
    client_off = struct.unpack_from("<H", page, ra_off + 0x16)[0]
    if client_off < 0x30 or ra_off + client_off + 0x30 > page_size:
        raise NtfsReplayError("restart area client offset is outside the page")
    struct.pack_into("<HH", page, ra_off + 0x0A, 0, 0xFFFF)
    struct.pack_into("<H", page, ra_off + 0x0E, 2)
    open_count = struct.unpack_from("<I", page, ra_off + 0x28)[0]
    struct.pack_into("<I", page, ra_off + 0x28, (open_count + 1) & 0xFFFFFFFF)
    struct.pack_into("<HH", page, ra_off + client_off + 0x10, 0xFFFF, 0xFFFF)
    usn = 0x0001
    struct.pack_into("<H", page, 0x1E, usn)
    for index in range(fixup_count - 1):
        trailer = (index + 1) * SECTOR_SIZE - 2
        original = struct.unpack_from("<H", page, trailer)[0]
        struct.pack_into("<H", page, 0x1E + 2 * (index + 1), original)
        struct.pack_into("<H", page, trailer, usn)
    return bytes(page)


def _write_clean_restart_pages(
    stream,
    logfile: NtfsAttributeSummary,
    log_stream: bytes,
    analysis: NtfsLogFileAnalysis,
    *,
    cluster_size: int,
    base_offset: int,
) -> int:
    page_size = analysis.restart.log_page_size
    _area, fixed_page = parse_restart_pages(log_stream)
    clean = _clean_restart_page(fixed_page, analysis)
    for page_offset in (0, page_size):
        for offset, physical, size in _run_segments(
            logfile, cluster_size, page_offset, page_size, base_offset
        ):
            start = offset - page_offset
            stream.seek(physical)
            written = stream.write(clean[start : start + size])
            if written != size:
                raise NtfsReplayError("short write of a clean restart page")
    return 2


# ---------------------------------------------------------------- entry point


def _open_attribute_entry(
    tables: NtfsRestartTables, record: LogRecord
) -> OpenAttributeEntry:
    for entry in tables.open_attributes:
        if entry.index == record.target_attr:
            return entry
    raise NtfsReplayError(
        f"log record 0x{record.lsn:x} targets open attribute table slot "
        f"{record.target_attr}, which is not allocated"
    )


def _find_attribute_by_type(
    header: NtfsFileRecordHeader, type_code: int
) -> NtfsAttributeSummary:
    attributes = parse_ntfs_attribute_stream(header, allow_end_marker_padding=True)
    matches = [
        attribute for attribute in attributes if attribute.type_code == type_code
    ]
    if not matches:
        raise NtfsReplayError(f"FILE record has no attribute of type 0x{type_code:x}")
    unnamed = [attribute for attribute in matches if not attribute.name]
    if len(matches) == 1:
        return matches[0]
    if len(unnamed) == 1:
        return unnamed[0]
    raise NtfsReplayError(
        f"FILE record has {len(matches)} attributes of type 0x{type_code:x}; "
        "named attribute disambiguation is outside the replay profile"
    )


def _validate_attribute_type(
    header: NtfsFileRecordHeader, offset: int, expected: int
) -> None:
    if offset < header.first_attribute_offset or offset + 16 > header.bytes_in_use:
        raise NtfsReplayError("attribute offset is outside the FILE record")
    actual = struct.unpack_from("<I", header.fixed_record, offset)[0]
    if actual != expected:
        raise NtfsReplayError(
            f"attribute at record offset 0x{offset:x} has type 0x{actual:x}, "
            f"not the open-table type 0x{expected:x}"
        )


def _apply_one(
    mft: _Mft,
    tables: NtfsRestartTables,
    record: LogRecord,
    payload: bytes,
    *,
    redo: bool,
    cluster_size: int,
    base_offset: int,
) -> tuple[int, int]:
    """Apply one redo/undo record; return (record writes, stream writes)."""

    op = record.redo_op if redo else record.undo_op
    entry = _open_attribute_entry(tables, record)
    if op in MFT_OPERATIONS:
        cluster_bits = cluster_size.bit_length() - 1
        if (1 << cluster_bits) != cluster_size:
            raise NtfsReplayError("cluster size is not a power of two")
        vbo = (record.target_vcn << cluster_bits) + (record.cluster_off << 9)
        rno = vbo >> (mft.record_size.bit_length() - 1)
        if rno * mft.record_size != vbo:
            raise NtfsReplayError(
                f"log record 0x{record.lsn:x} targets an unaligned MFT record"
            )
        if rno >= mft.record_count:
            raise NtfsReplayError(
                f"log record 0x{record.lsn:x} targets MFT record {rno}, outside "
                "the existing $MFT DATA stream"
            )
        if (
            op == OP_INITIALIZE_FILE_RECORD
            and record.record_off == 0
            and len(payload) == mft.record_size
        ):
            try:
                initialized = parse_ntfs_file_record_header(payload)
            except NtfsRecordError as exc:
                raise NtfsReplayError(
                    f"InitializeFileRecordSegment payload is invalid: {exc}"
                ) from exc
            raw_before = mft.read_record_raw(rno)
            gate_mft_record_reuse(
                mft._stream,
                mft,
                mft.boot,
                base_offset=base_offset,
                record_number=rno,
                raw_before=raw_before,
                initialized=initialized,
                lsn=record.lsn,
            )
            fixed = bytearray(initialized.fixed_record)
            _set_record_lsn(fixed, record.lsn)
            mft.write_record(rno, bytes(fixed))
            _fault("ntfs:replay:mft_record:applied")
            return 1, 0
        header = mft.read_record(rno)
        if op in (
            OP_UPDATE_RESIDENT_VALUE,
            OP_UPDATE_MAPPING_PAIRS,
            OP_SET_NEW_ATTRIBUTE_SIZES,
            OP_ZERO_END_OF_FILE_RECORD,
        ):
            if rno != entry.record_number:
                raise NtfsReplayError(
                    f"log record 0x{record.lsn:x} resolves to MFT record {rno}, "
                    f"but its open attribute entry references record "
                    f"{entry.record_number}"
                )
            _validate_attribute_type(header, record.record_off, entry.type_code)
        modified, _kind = _apply_mft_operation(header, record, payload, redo=redo)
        mft.write_record(rno, modified)
        _fault("ntfs:replay:mft_record:applied")
        return 1, 0
    if op in STREAM_OPERATIONS:
        if entry.record_number >= mft.record_count:
            raise NtfsReplayError(
                f"open attribute entry references MFT record {entry.record_number}, "
                "which is outside the existing $MFT DATA stream"
            )
        header = mft.read_record(entry.record_number)
        if header.sequence_number != (entry.reference >> 48) & 0xFFFF:
            raise NtfsReplayError(
                "open attribute entry sequence does not match the FILE record"
            )
        attribute = _find_attribute_by_type(header, entry.type_code)
        stream_writes = _apply_stream_operation(
            mft._stream,
            header,
            attribute,
            record,
            payload,
            cluster_size=cluster_size,
            base_offset=base_offset,
            redo=redo,
        )
        if stream_writes:
            _fault("ntfs:replay:stream_write:applied")
        return 0, stream_writes
    raise NtfsReplayError(
        f"operation 0x{op:02x} at LSN 0x{record.lsn:x} has no image-level handler"
    )


def _clear_volume_dirty(mft: _Mft) -> tuple[int, int]:
    """Clear ``VOLUME_FLAG_DIRTY`` in ``$Volume`` and return (before, after)."""

    header = mft.read_record(MFT_RECORD_VOLUME)
    attributes = parse_ntfs_attribute_stream(header, allow_end_marker_padding=True)
    if any(attribute.type_code == ATTR_ATTRIBUTE_LIST for attribute in attributes):
        raise NtfsReplayError("$Volume uses $ATTRIBUTE_LIST; replay refuses it")
    matches = [
        attribute
        for attribute in attributes
        if attribute.type_code == ATTR_VOLUME_INFORMATION and not attribute.name
    ]
    if len(matches) != 1 or matches[0].resident_value is None:
        raise NtfsReplayError("$Volume has no unique resident volume information")
    if len(matches[0].resident_value) != 12:
        raise NtfsReplayError("$Volume information has an invalid length")
    offset = _attribute_offset(header, matches[0].attribute_id)
    fixed = bytearray(header.fixed_record)
    value_offset = struct.unpack_from("<H", fixed, offset + 20)[0]
    flags = struct.unpack_from("<H", fixed, offset + value_offset + 10)[0]
    if not flags & 0x0001:
        return flags, flags
    struct.pack_into("<H", fixed, offset + value_offset + 10, flags & ~0x0001)
    mft.write_record(MFT_RECORD_VOLUME, bytes(fixed))
    return flags, flags & ~0x0001


def _update_mft_mirror(
    stream, mft: _Mft, boot: NtfsBootProfile, *, base_offset: int
) -> bool:
    """Copy the first four MFT records to ``$MFTMirr`` (ntfs_update_mftmirr)."""

    changed = False
    mirror = base_offset + boot.mft_mirror_offset
    for index in range(MFT_MIRROR_RECORDS):
        raw = mft.read_record_raw(index)
        target = mirror + index * mft.record_size
        stream.seek(target)
        existing = stream.read(len(raw))
        if existing != raw:
            stream.seek(target)
            written = stream.write(raw)
            if written != len(raw):
                raise NtfsReplayError("short write to $MFTMirr")
            changed = True
    return changed


def gate_mft_record_reuse(
    stream,
    mft: _Mft,
    boot: NtfsBootProfile,
    *,
    base_offset: int,
    record_number: int,
    raw_before: bytes,
    initialized: NtfsFileRecordHeader,
    lsn: int,
) -> None:
    """Gate reuse of a free MFT record slot through the allocation planner (#510 C5).

    A free (not-in-use) candidate must be backed by a byte-identical
    ``$MFTMirr`` copy and carry a sequence that continues the pre-image
    (mod 2**16) before the executor may initialize it.  Zeroed or unreadable
    pre-images and re-initialization of a live record keep their existing
    behavior; records outside the mirrored window are refused outright.
    Every refusal happens before a single byte is written.
    """
    try:
        current = parse_ntfs_file_record_header(raw_before, require_in_use=False)
    except NtfsRecordError:
        # A zeroed or structurally unreadable pre-image has no mirror contract
        # to check; the payload itself stays fully validated by the caller.
        return
    if current.in_use:
        # Overwriting a live record is re-initialization, not allocation.
        return
    if record_number >= MFT_MIRROR_RECORDS:
        raise NtfsReplayError(
            f"log record 0x{lsn:x} allocates MFT record {record_number} outside "
            "the $MFTMirr window; record reuse is refused"
        )
    mirror_offset = (
        base_offset + boot.mft_mirror_offset + record_number * mft.record_size
    )
    stream.seek(mirror_offset)
    raw_mirror = stream.read(mft.record_size)
    if len(raw_mirror) != mft.record_size:
        raise NtfsReplayError("short read from $MFTMirr during allocation gate")
    reference = (initialized.sequence_number << 48) | record_number
    try:
        plan_mft_record_reuse(
            raw_before, raw_mirror, reference, candidate_record_number=record_number
        )
    except NtfsMftAllocationError as exc:
        raise NtfsReplayError(
            f"MFT record {record_number} reuse refused before write: {exc}"
        ) from exc


def _find_mft_data(profile) -> NtfsAttributeSummary:
    for attribute in profile.attributes:
        if attribute.type_code == ATTR_DATA and not attribute.name:
            if not attribute.nonresident or not attribute.runs:
                break
            return attribute
    raise NtfsReplayError("$MFT has no bounded unnamed nonresident DATA stream")


def _repair_volume_markers(
    image_path: str, profile, offset: int
) -> tuple[int, int, bool]:
    """Clear ``$Volume`` and refresh ``$MFTMirr`` without replaying records."""

    mft_data = _find_mft_data(profile)
    with open(image_path, "r+b") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=offset)
        before, after = _clear_volume_dirty(mft)
        _fault("ntfs:replay:volume_marker:applied")
        mirror_updated = _update_mft_mirror(
            handle, mft, profile.boot, base_offset=offset
        )
        _fault("ntfs:replay:mft_mirror:applied")
        handle.flush()
        os.fsync(handle.fileno())
    return before, after, mirror_updated


def native_ntfs_recover(image_path: str, offset: int = 0) -> NtfsReplayResult:
    """Replay a dirty ``$LogFile`` on the image handed in (a disposable copy)."""

    image_path = os.fspath(image_path)
    if offset < 0:
        raise NtfsReplayError("volume offset is negative")
    volume_size = os.path.getsize(image_path) - offset
    try:
        profile = probe_ntfs_metadata(
            image_path,
            offset=offset,
            volume_size=volume_size,
            validate_mirror=False,
        )
    except NtfsMetadataError as exc:
        raise NtfsReplayError(f"cannot inspect NTFS metadata: {exc}") from exc
    try:
        stream, cluster_size, logfile = read_logfile_stream(
            image_path, offset=offset, validate_mirror=False
        )
        analysis = analyze_logfile(stream, cluster_size=cluster_size)
    except NtfsLogFileNeverWritten:
        # Only two fully erased restart pages qualify as a never-written log.
        # Any malformed or unsupported active log propagates without mutation.
        _fault("mutation:start")
        before, after, mirror = _repair_volume_markers(image_path, profile, offset)
        _fault("mutation:flush")
        verified_profile = probe_ntfs_metadata(
            image_path, offset=offset, volume_size=volume_size
        )
        if not verified_profile.mirror_matches_mft:
            raise NtfsReplayError(
                "$MFT and $MFTMirr still differ after repair"
            ) from None
        _fault("mutation:done")
        return NtfsReplayResult(
            changed=before != after or mirror,
            redo_applied=0,
            undo_applied=0,
            redo_start=0,
            records_written=0,
            stream_writes=0,
            clean_restart_pages=0,
            volume_flags_before=before,
            volume_flags_after=after,
            mirror_updated=mirror,
            notes=(
                "$LogFile restart pages were never written; repaired $Volume/$MFTMirr only",
            ),
        )
    if not analysis.dirty:
        _fault("mutation:start")
        before, after, mirror = _repair_volume_markers(image_path, profile, offset)
        _fault("mutation:flush")
        verified_profile = probe_ntfs_metadata(
            image_path, offset=offset, volume_size=volume_size
        )
        if not verified_profile.mirror_matches_mft:
            raise NtfsReplayError(
                "$MFT and $MFTMirr still differ after repair"
            ) from None
        _fault("mutation:done")
        return NtfsReplayResult(
            changed=before != after or mirror,
            redo_applied=0,
            undo_applied=0,
            redo_start=0,
            records_written=0,
            stream_writes=0,
            clean_restart_pages=0,
            volume_flags_before=before,
            volume_flags_after=after,
            mirror_updated=mirror,
            notes=("$LogFile is already clean; repaired $Volume/$MFTMirr only",),
        )
    tables = parse_restart_tables(stream, analysis)
    plan = plan_ntfs_replay(analysis, tables)
    unsupported = {record.redo_op for record in plan.redo} | {
        record.undo_op for record in plan.undo
    }
    unsupported -= SKIPPABLE
    unsupported -= SUPPORTED_OPERATIONS
    if unsupported:
        raise NtfsReplayError(
            "unsupported NTFS replay operations: "
            + ", ".join(f"0x{op:02x}" for op in sorted(unsupported))
        )
    mft_data = _find_mft_data(profile)
    redo_applied = 0
    undo_applied = 0
    records_written = 0
    stream_writes = 0
    _fault("mutation:start")
    with open(image_path, "r+b") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=offset)
        if plan.redo:
            _fault("replay:redo:start")
        for record in plan.redo:
            writes, streamed = _apply_one(
                mft,
                tables,
                record,
                record.redo_data,
                redo=True,
                cluster_size=cluster_size,
                base_offset=offset,
            )
            records_written += writes
            stream_writes += streamed
            redo_applied += 1
        if plan.redo:
            _fault("replay:redo:done")
        if plan.undo:
            _fault("replay:undo:start")
        for record in plan.undo:
            writes, streamed = _apply_one(
                mft,
                tables,
                record,
                record.undo_data,
                redo=False,
                cluster_size=cluster_size,
                base_offset=offset,
            )
            records_written += writes
            stream_writes += streamed
            undo_applied += 1
        if plan.undo:
            _fault("replay:undo:done")
        _fault("replay:journal:clean-pages")
        clean_pages = _write_clean_restart_pages(
            handle,
            logfile,
            stream,
            analysis,
            cluster_size=cluster_size,
            base_offset=offset,
        )
        _fault("ntfs:replay:restart_pages:applied")
        _fault("replay:volume:dirty-flag")
        volume_before, volume_after = _clear_volume_dirty(mft)
        _fault("ntfs:replay:volume_marker:applied")
        _fault("replay:mirror:update")
        mirror_updated = _update_mft_mirror(
            handle, mft, profile.boot, base_offset=offset
        )
        _fault("ntfs:replay:mft_mirror:applied")
        handle.flush()
        os.fsync(handle.fileno())
    _fault("replay:flush")
    _fault("mutation:flush")
    verify_stream, verify_cluster, _verify_logfile = read_logfile_stream(
        image_path, offset=offset
    )
    verified = analyze_logfile(verify_stream, cluster_size=verify_cluster)
    if verified.dirty:
        raise NtfsReplayError("$LogFile still reports a dirty dismount after replay")
    verified_profile = probe_ntfs_metadata(
        image_path, offset=offset, volume_size=volume_size
    )
    if not verified_profile.mirror_matches_mft:
        raise NtfsReplayError("$MFT and $MFTMirr still differ after replay")
    _fault("mutation:done")
    return NtfsReplayResult(
        changed=True,
        redo_applied=redo_applied,
        undo_applied=undo_applied,
        redo_start=plan.redo_start,
        records_written=records_written,
        stream_writes=stream_writes,
        clean_restart_pages=clean_pages,
        volume_flags_before=volume_before,
        volume_flags_after=volume_after,
        mirror_updated=mirror_updated,
    )
