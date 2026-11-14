# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only ``$LogFile`` analyzer: parse, classify, plan -- never mutate.

Issue #510 block C asked for the ``$LogFile`` parser (restart area, RCRD pages,
LFS client records, redo/undo payloads, USA validation) that the NTFS path did
not have.  This module is that parser.  It is deliberately **not** a replay
engine: the super-audit of 2026-11-14 concluded that wiring an executor before
the acceptance invariants of issue #387 exist would make the tree *less* safe
than today's fail-closed gate, so :mod:`mountfile.ntfs_native_writer` still
never replays a dirty volume: ``recover="copy"`` quarantines the evidence and
then refuses, and nothing here opens an image read-write.  Every
:class:`ReplayStep` carries a blocker, ``executable`` stays ``False``
unconditionally, and :data:`EXECUTION_BLOCKERS` names what is still missing.

Structures and rules come from ``fs/ntfs3/fslog.c`` (torvalds/linux), whose
``static_assert`` calls pin the sizes used below.  The offsets and the analysis
rules are recorded in ``docs/ntfs-logfile-layout.md``.  The three places where
the first draft of issue #510 was materially wrong are fixed here and covered by
tests:

* redo and undo are **independent slices** of the record body
  (``redo_off/redo_len`` vs ``undo_off/undo_len``); sharing one payload makes an
  undo write redo bytes, which is corruption rather than recovery;
* ``ForgetTransaction`` **frees** the transaction-table entry, so a forgotten
  transaction is neither redone nor undone;
* rollback candidates are transactions still in the *active* state with a
  non-zero ``undo_next_lsn`` (fslog.c:5204-5216), walked **forward** from that
  LSN -- prepared and committed transactions are never undone, and the undo
  pointer lives in the record header, not in the client record.

The module also exposes :class:`NtfsReplayOverlay`, an atomic in-memory reducer
for a plan whose byte targets were resolved by the caller.  It never opens an
image, never guesses MFT/attribute/runlist locations, and is not wired into the
native writer.  It exists so redo/undo ordering and payload selection can be
proven before any image-level executor is allowed to exist.

Refusals are typed, never silent: a log page whose size differs from the 4 KiB
system page, records that cross the page's free-space boundary, multi-page
records, ``transact_id`` values that are not valid table offsets, redo/undo
slices that leave the record body, duplicated LSNs and unknown page signatures
all raise :class:`NtfsLogFileError`.
"""

from __future__ import annotations

import bisect
import os
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from mountfile.native_write import NativeWriteUnsafe
from mountfile.ntfs_mft import parse_ntfs_file_record_header
from mountfile.ntfs_reader import (
    NtfsAttributeSummary,
    NtfsLogFileRestartArea,
    NtfsMetadataError,
    NtfsRun,
    parse_ntfs_attribute_stream,
    parse_ntfs_logfile_restart_area,
    probe_ntfs_metadata,
)

SECTOR = 512
RSTR_MAGIC = b"RSTR"
RCRD_MAGIC = b"RCRD"
BAAD_MAGIC = b"BAAD"
CHKD_MAGIC = b"CHKD"

ATTR_DATA = 0x80
ATTR_ATTRIBUTE_LIST = 0x100
LOGFILE_RECORD_NUMBER = 2
RESTART_PAGE_SIZE = 4096

LFS_NO_CLIENT = 0xFFFF
LFS_CLIENT_RECORD = 1
LFS_CLIENT_RESTART = 2
LOG_RECORD_MULTI_PAGE = 0x0001

NTFS_RESTART_SIZE = 0x40
CLIENT_REC_SIZE = 0xA0
MAX_LOG_BYTES = 64 << 20
MAX_RECORDS = 1 << 16

# struct LFS_RECORD_HDR (fslog.c:245-255), static_assert size == 0x30.
LFS_RECORD_HDR = struct.Struct("<QQQIHHIIH6x")
# struct LOG_REC_HDR (fslog.c:73-88), static_assert size == 0x20.
LOG_REC_HDR = struct.Struct("<12HQ")
# struct CLIENT_REC (fslog.c:42-53), static_assert size == 0xa0.
CLIENT_REC = struct.Struct("<QQHHH6xI128s")
# struct NTFS_RESTART (fslog.c:201-215), static_assert size == 0x40.
NTFS_RESTART = struct.Struct("<IIQQQQQIIII")

# Attribute-required bitmap, fslog.c:347-356.
_ATTRIBUTE_REQUIRED = (0xFC, 0xFB, 0xFF, 0x10, 0x06)

# enum NTFS_LOG_OPERATION (fslog.c:298-340).
OPERATION_NAMES = {
    0x00: "Noop",
    0x01: "CompensationLogRecord",
    0x02: "InitializeFileRecordSegment",
    0x03: "DeallocateFileRecordSegment",
    0x04: "WriteEndOfFileRecordSegment",
    0x05: "CreateAttribute",
    0x06: "DeleteAttribute",
    0x07: "UpdateResidentValue",
    0x08: "UpdateNonresidentValue",
    0x09: "UpdateMappingPairs",
    0x0A: "DeleteDirtyClusters",
    0x0B: "SetNewAttributeSizes",
    0x0C: "AddIndexEntryRoot",
    0x0D: "DeleteIndexEntryRoot",
    0x0E: "AddIndexEntryAllocation",
    0x0F: "DeleteIndexEntryAllocation",
    0x10: "WriteEndOfIndexBuffer",
    0x11: "SetIndexEntryVcnRoot",
    0x12: "SetIndexEntryVcnAllocation",
    0x13: "UpdateFileNameRoot",
    0x14: "UpdateFileNameAllocation",
    0x15: "SetBitsInNonresidentBitMap",
    0x16: "ClearBitsInNonresidentBitMap",
    0x17: "HotFix",
    0x18: "EndTopLevelAction",
    0x19: "PrepareTransaction",
    0x1A: "CommitTransaction",
    0x1B: "ForgetTransaction",
    0x1C: "OpenNonresidentAttribute",
    0x1D: "OpenAttributeTableDump",
    0x1E: "AttributeNamesDump",
    0x1F: "DirtyPageTableDump",
    0x20: "TransactionTableDump",
    0x21: "UpdateRecordDataRoot",
    0x22: "UpdateRecordDataAllocation",
    0x23: "UpdateRelativeDataInIndex",
    0x24: "UpdateRelativeDataInIndex2",
    0x25: "ZeroEndOfFileRecord",
}

# can_skip_action() (fslog.c:358-378): operations that carry no byte payload.
SKIPPABLE_OPERATIONS = frozenset(
    (
        0x00,  # Noop
        0x01,  # CompensationLogRecord
        0x0A,  # DeleteDirtyClusters
        0x17,  # HotFix
        0x18,  # EndTopLevelAction
        0x19,  # PrepareTransaction
        0x1A,  # CommitTransaction
        0x1B,  # ForgetTransaction
        0x1C,  # OpenNonresidentAttribute
        0x1D,  # OpenAttributeTableDump
        0x1E,  # AttributeNamesDump
        0x1F,  # DirtyPageTableDump
        0x20,  # TransactionTableDump
    )
)

TRANSACTION_UNINITIALIZED = 0
TRANSACTION_ACTIVE = 1
TRANSACTION_PREPARED = 2
TRANSACTION_COMMITTED = 3

# The overlay executor is intentionally narrower than the parser. Resident
# value updates are the only operation with a bounded byte payload that can be
# applied after an independently validated target is supplied. MFT
# allocation/reuse, bitmap changes, index updates and mirror repair require a
# filesystem-aware metadata executor and remain typed refusals.
BOUNDED_OVERLAY_OPERATIONS = frozenset((0x07,))  # UpdateResidentValue

_STATE_NAMES = {
    TRANSACTION_UNINITIALIZED: "uninitialized",
    TRANSACTION_ACTIVE: "active",
    TRANSACTION_PREPARED: "prepared",
    TRANSACTION_COMMITTED: "committed",
}

# The replay executor is out of scope for this module by design (issue #510
# audit, section 2.1).  This is the single source of truth for why a plan is
# evidence rather than an instruction, so tests can assert that nothing here is
# ever advertised as runnable.
EXECUTION_BLOCKERS = (
    "target byte resolution needs the open attribute table and the per-record "
    "attribute lookup of fslog.c do_action - not implemented",
    "$ATTRIBUTE_LIST cross-record targets are not resolved",
    "the redo start must come from the dirty page table (rlsn), not from the "
    "checkpoint LSN - the tables are parsed but not consumed",
    "no acceptance evidence on a real crash image (issue #387)",
)


class NtfsLogFileError(NativeWriteUnsafe):
    """The log stream is outside the parsed-and-proven bounded profile."""


class NtfsReplayOverlayError(NtfsLogFileError):
    """A bounded replay overlay cannot be applied atomically."""


@dataclass(frozen=True)
class ReplayTarget:
    """Explicit byte target supplied by a caller that already resolved NTFS metadata.

    The parser deliberately cannot infer this mapping from ``target_attr`` alone.
    Keeping the mapping explicit makes this reducer useful for synthetic fixtures
    and prevents it from silently guessing MFT/attribute/runlist locations.
    """

    target_attr: int
    target_vcn: int
    offset: int
    length: int


@dataclass(frozen=True)
class OverlayProvenance:
    """Evidence describing one applied redo/undo payload."""

    lsn: int
    transaction_id: int
    kind: str
    operation: str
    target_attr: int
    target_vcn: int
    offset: int
    length: int


class NtfsReplayOverlay:
    """Apply a bounded, explicitly-targeted plan to a private byte overlay.

    This is intentionally not an image writer and is not used by the native
    NTFS path.  It validates every target and payload before creating the result,
    then applies all writes to a private copy so malformed plans can never leave
    a partially modified caller buffer.  A future image executor can reuse this
    reducer after it proves MFT/attribute/runlist resolution independently.
    """

    def __init__(self, source: bytes | bytearray):
        self._source = bytes(source)

    def apply(
        self,
        steps: Sequence[ReplayStep],
        targets: Mapping[tuple[int, int], ReplayTarget],
    ) -> tuple[bytes, tuple[OverlayProvenance, ...]]:
        staged = bytearray(self._source)
        provenance: list[OverlayProvenance] = []
        validated: list[tuple[ReplayStep, ReplayTarget]] = []
        # Interval list sorted by start offset.  A byte map would allocate one
        # entry per payload byte, which is unbounded for large nonresident
        # values; overlap detection only needs the occupied intervals.
        occupied: list[tuple[int, int, tuple[int, int]]] = []
        for step in steps:
            if step.operation in SKIPPABLE_OPERATIONS:
                continue
            if step.kind not in {"redo", "undo"}:
                raise NtfsReplayOverlayError(
                    f"unsupported replay step kind: {step.kind!r}"
                )
            target = targets.get((step.target_attr, step.target_vcn))
            if target is None:
                raise NtfsReplayOverlayError(
                    "replay target is unresolved: "
                    f"attr={step.target_attr} vcn={step.target_vcn}"
                )
            if (
                target.target_attr != step.target_attr
                or target.target_vcn != step.target_vcn
            ):
                raise NtfsReplayOverlayError("replay target identity mismatch")
            if target.offset < 0 or target.length < 0:
                raise NtfsReplayOverlayError("replay target range is negative")
            if target.offset + target.length > len(staged):
                raise NtfsReplayOverlayError("replay target range is outside overlay")
            if len(step.payload) != target.length:
                raise NtfsReplayOverlayError(
                    f"replay payload length {len(step.payload)} does not match "
                    f"target length {target.length}"
                )
            range_end = target.offset + target.length
            target_key = (target.target_attr, target.target_vcn)
            if target.length:
                for start, end, key in occupied:
                    if key != target_key and start < range_end and target.offset < end:
                        raise NtfsReplayOverlayError(
                            "overlapping replay targets are ambiguous"
                        )
                bisect.insort(occupied, (target.offset, range_end, target_key))
            validated.append((step, target))
        for step, target in validated:
            staged[target.offset : target.offset + target.length] = step.payload
            provenance.append(
                OverlayProvenance(
                    lsn=step.lsn,
                    transaction_id=step.transaction_id,
                    kind=step.kind,
                    operation=step.operation_name,
                    target_attr=step.target_attr,
                    target_vcn=step.target_vcn,
                    offset=target.offset,
                    length=target.length,
                )
            )
        return bytes(staged), tuple(provenance)


def replay_analysis_to_overlay(
    analysis: NtfsLogFileAnalysis,
    source: bytes | bytearray,
    targets: Mapping[tuple[int, int], ReplayTarget],
) -> tuple[bytes, tuple[OverlayProvenance, ...]]:
    """Apply a parsed redo/undo plan to a private, explicitly mapped overlay.

    This is the first executable part of block C, but it is intentionally not
    an NTFS image writer.  The caller must resolve every ``(attribute, VCN)``
    target from validated MFT/runlist metadata.  The order is the recovery
    order: redo committed transactions first, then undo active transactions.
    Truncated analysis is refused before any bytes are staged.  No MFT
    allocation, ``$MFTMirr`` repair, USA update, or filesystem publication is
    performed here; those remain separate acceptance-gated work.
    """
    if not isinstance(analysis, NtfsLogFileAnalysis):
        raise NtfsReplayOverlayError("replay analysis has an invalid type")
    if analysis.truncated:
        raise NtfsReplayOverlayError(
            "cannot execute a replay plan from a truncated $LogFile stream"
        )
    if not isinstance(targets, Mapping):
        raise NtfsReplayOverlayError("replay targets must be an explicit mapping")
    steps = (*analysis.redo_plan, *analysis.undo_plan)
    unsupported = [
        step.operation_name
        for step in steps
        if step.operation not in SKIPPABLE_OPERATIONS
        and step.operation not in BOUNDED_OVERLAY_OPERATIONS
    ]
    if unsupported:
        raise NtfsReplayOverlayError(
            "NTFS replay operation requires an image-level metadata executor: "
            + ", ".join(sorted(set(unsupported)))
        )
    return NtfsReplayOverlay(source).apply(steps, targets)


class _UnusedPage(Exception):
    """Internal marker for an erased (all ``0xFF``) log page."""


class _EndOfRecords(Exception):
    """Internal marker: a zeroed record header ends the page's record list."""


@dataclass(frozen=True)
class LogClient:
    """One validated ``CLIENT_REC`` of the restart area's client array."""

    index: int
    oldest_lsn: int
    restart_lsn: int
    prev_client: int
    next_client: int
    sequence_number: int
    name: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "oldest_lsn": self.oldest_lsn,
            "restart_lsn": self.restart_lsn,
            "name": self.name,
        }


@dataclass(frozen=True)
class ClientRestartArea:
    """The client data (``struct NTFS_RESTART``) of a checkpoint record."""

    lsn: int
    major_version: int
    minor_version: int
    check_point_start: int
    open_attribute_table_lsn: int
    attribute_names_lsn: int
    dirty_page_table_lsn: int
    transaction_table_lsn: int
    open_attribute_length: int
    attribute_names_length: int
    dirty_page_length: int
    transaction_table_length: int

    def as_dict(self) -> dict:
        return {
            "lsn": self.lsn,
            "check_point_start": self.check_point_start,
            "dirty_page_table_lsn": self.dirty_page_table_lsn,
            "transaction_table_lsn": self.transaction_table_lsn,
        }


@dataclass(frozen=True)
class LogRecord:
    """One LFS client record with redo and undo kept apart."""

    lsn: int
    byte_offset: int
    page_index: int
    record_type: int
    transaction_id: int
    sequence_number: int
    client_index: int
    prev_lsn: int
    undo_next_lsn: int
    flags: int
    data_length: int
    redo_op: int
    undo_op: int
    redo_data: bytes
    undo_data: bytes
    target_attr: int
    lcns_follow: int
    record_off: int
    attr_off: int
    cluster_off: int
    target_vcn: int
    page_lcns: tuple[int, ...]
    restart: ClientRestartArea | None = None

    @property
    def redo_len(self) -> int:
        return len(self.redo_data)

    @property
    def undo_len(self) -> int:
        return len(self.undo_data)

    @property
    def is_client_record(self) -> bool:
        return self.record_type == LFS_CLIENT_RECORD

    @property
    def is_restart_record(self) -> bool:
        return self.record_type == LFS_CLIENT_RESTART

    @property
    def redo_operation(self) -> str:
        return OPERATION_NAMES.get(self.redo_op, f"unknown(0x{self.redo_op:02x})")

    @property
    def undo_operation(self) -> str:
        return OPERATION_NAMES.get(self.undo_op, f"unknown(0x{self.undo_op:02x})")

    def as_dict(self) -> dict:
        return {
            "lsn": self.lsn,
            "byte_offset": self.byte_offset,
            "page": self.page_index,
            "type": self.record_type,
            "transaction_id": self.transaction_id,
            "prev_lsn": self.prev_lsn,
            "undo_next_lsn": self.undo_next_lsn,
            "redo_op": self.redo_operation,
            "undo_op": self.undo_operation,
            "redo_len": self.redo_len,
            "undo_len": self.undo_len,
            "target_attr": self.target_attr,
            "target_vcn": self.target_vcn,
            "lcns_follow": self.lcns_follow,
        }


@dataclass(frozen=True)
class Transaction:
    """The analysis-pass state of one ``transact_id`` (fslog.c:4608-4846)."""

    transaction_id: int
    state: str
    first_lsn: int
    prev_lsn: int
    undo_next_lsn: int
    record_lsns: tuple[int, ...]
    forgotten: bool = False

    @property
    def undo_candidate(self) -> bool:
        """Only active transactions with an undo pointer are rolled back."""
        return self.state == "active" and self.undo_next_lsn != 0

    def as_dict(self) -> dict:
        return {
            "id": self.transaction_id,
            "state": self.state,
            "first_lsn": self.first_lsn,
            "undo_next_lsn": self.undo_next_lsn,
            "records": len(self.record_lsns),
            "forgotten": self.forgotten,
        }


@dataclass(frozen=True)
class ReplayStep:
    """One planned byte write; the byte target is intentionally unresolved."""

    lsn: int
    transaction_id: int
    kind: str
    operation: int
    length: int
    payload: bytes
    target_attr: int
    target_vcn: int
    reasons: tuple[str, ...] = ()

    @property
    def operation_name(self) -> str:
        return OPERATION_NAMES.get(self.operation, f"unknown(0x{self.operation:02x})")

    @property
    def resolvable(self) -> bool:
        """Always false: this analyzer does not produce writable targets."""
        return False

    def as_dict(self) -> dict:
        return {
            "lsn": self.lsn,
            "transaction_id": self.transaction_id,
            "kind": self.kind,
            "operation": self.operation_name,
            "length": self.length,
            "target_attr": self.target_attr,
            "target_vcn": self.target_vcn,
            "resolvable": self.resolvable,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class NtfsLogFileAnalysis:
    """Everything the parser proved about one ``$LogFile`` stream."""

    restart: NtfsLogFileRestartArea
    page_size: int
    first_page: int
    log_size: int
    scanned_bytes: int
    current_lsn: int
    checkpoint_lsn: int
    oldest_lsn: int
    clients: tuple[LogClient, ...] = ()
    records: tuple[LogRecord, ...] = ()
    transactions: tuple[Transaction, ...] = ()
    redo_plan: tuple[ReplayStep, ...] = ()
    undo_plan: tuple[ReplayStep, ...] = ()
    restart_area: ClientRestartArea | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False
    executable: bool = False
    blockers: tuple[str, ...] = EXECUTION_BLOCKERS

    @property
    def dirty(self) -> bool:
        """The log was left open, i.e. the volume needs recovery."""
        return not self.restart.clean_dismount

    def record_at(self, lsn: int) -> LogRecord | None:
        for record in self.records:
            if record.lsn == lsn:
                return record
        return None

    def as_dict(self) -> dict:
        steps = (*self.redo_plan, *self.undo_plan)
        return {
            "parsed": True,
            "dirty": self.dirty,
            "page_size": self.page_size,
            "first_page": self.first_page,
            "log_size": self.log_size,
            "scanned_bytes": self.scanned_bytes,
            "truncated": self.truncated,
            "current_lsn": self.current_lsn,
            "checkpoint_lsn": self.checkpoint_lsn,
            "oldest_lsn": self.oldest_lsn,
            "clean_dismount": self.restart.clean_dismount,
            "version": list(self.restart.version),
            "clients": [client.as_dict() for client in self.clients],
            "records": [record.as_dict() for record in self.records],
            "record_count": len(self.records),
            "transactions": [
                transaction.as_dict() for transaction in self.transactions
            ],
            "redo_steps": [step.as_dict() for step in self.redo_plan],
            "undo_steps": [step.as_dict() for step in self.undo_plan],
            "unresolved_steps": sum(1 for step in steps if not step.resolvable),
            "executable": self.executable,
            "blockers": list(self.blockers),
            "notes": list(self.notes),
        }


def _fixup_page(page: bytes, magic: bytes, *, page_size: int) -> bytes:
    """Restore the update-sequence array of one log page.

    The USA covers the last ``u16`` of every 512-byte stride *including* the
    first sector (offset ``0x1FE`` on a 4 KiB page) and must itself end before
    that word -- the rule Microsoft documents for a multi-sector header.
    """

    if len(page) != page_size:
        raise NtfsLogFileError(f"log page is {len(page)} bytes, expected {page_size}")
    if page_size < SECTOR or page_size % SECTOR:
        raise NtfsLogFileError(f"log page size {page_size} is not a sector multiple")
    if not any(page):
        raise NtfsLogFileError("log page is a run of zeros")
    if not page.strip(b"\xff"):
        raise _UnusedPage()
    signature = page[:4]
    if signature == BAAD_MAGIC:
        raise NtfsLogFileError("log page is marked BAAD (torn write)")
    if signature == CHKD_MAGIC:
        raise NtfsLogFileError(
            "log page was written by chkdsk; its state is not parsed"
        )
    if signature != magic:
        raise NtfsLogFileError(f"log page signature is {signature!r}, not {magic!r}")
    usa_offset, usa_count = struct.unpack_from("<HH", page, 4)
    expected = page_size // SECTOR + 1
    if usa_count != expected:
        raise NtfsLogFileError(
            f"log page USA count is {usa_count}, expected {expected}"
        )
    if usa_offset < 8 or usa_offset + 2 * expected > SECTOR - 2:
        raise NtfsLogFileError(
            "log page USA array does not end inside the first sector"
        )
    sequence = struct.unpack_from("<H", page, usa_offset)[0]
    fixed = bytearray(page)
    for index in range(page_size // SECTOR):
        trailer = (index + 1) * SECTOR - 2
        stored = struct.unpack_from("<H", page, trailer)[0]
        if stored != sequence:
            raise NtfsLogFileError(f"log page USA mismatch in sector {index + 1}")
        replacement = struct.unpack_from("<H", page, usa_offset + 2 + 2 * index)[0]
        struct.pack_into("<H", fixed, trailer, replacement)
    return bytes(fixed)


def _align8(value: int) -> int:
    return (value + 7) & ~7


def parse_restart_pages(stream: bytes) -> tuple[NtfsLogFileRestartArea, bytes]:
    """Return the newest validated restart area plus its USA-fixed page."""

    if len(stream) < 2 * RESTART_PAGE_SIZE:
        raise NtfsLogFileError("$LogFile is too small to hold two restart pages")
    pages = []
    for index in range(2):
        raw = stream[index * RESTART_PAGE_SIZE : (index + 1) * RESTART_PAGE_SIZE]
        try:
            area = parse_ntfs_logfile_restart_area(raw)
        except NtfsMetadataError as exc:  # geometry, version, client or USA problems
            raise NtfsLogFileError(f"$LogFile restart page {index}: {exc}") from exc
        pages.append((area, _fixup_page(raw, RSTR_MAGIC, page_size=RESTART_PAGE_SIZE)))
    keys = (
        "system_page_size",
        "log_page_size",
        "restart_offset",
        "restart_length",
        "sequence_number_bits",
        "file_size",
        "log_record_header_length",
        "log_page_data_offset",
    )
    first, second = pages[0][0], pages[1][0]
    if any(getattr(first, key) != getattr(second, key) for key in keys):
        raise NtfsLogFileError("$LogFile restart pages disagree on geometry")
    if first.version != second.version:
        raise NtfsLogFileError("$LogFile restart pages disagree on version")
    # Both drivers keep the restart area with the larger current_lsn; the second
    # page may legitimately lag behind the first one.
    return max(pages, key=lambda item: item[0].current_lsn)


def parse_clients(page: bytes, area: NtfsLogFileRestartArea) -> tuple[LogClient, ...]:
    """Read the restart area's client array (``struct CLIENT_REC``)."""

    client_offset = struct.unpack_from("<H", page, area.restart_offset + 0x16)[0]
    if client_offset < 0x30 or client_offset & 7:
        raise NtfsLogFileError("$LogFile client array offset is invalid")
    base = area.restart_offset + client_offset
    if base + CLIENT_REC_SIZE > len(page):
        raise NtfsLogFileError("$LogFile client array is outside the page")
    (
        oldest_lsn,
        restart_lsn,
        prev_client,
        next_client,
        sequence_number,
        name_bytes,
        raw_name,
    ) = CLIENT_REC.unpack_from(page, base)
    if name_bytes != 8 or raw_name[:8] != "NTFS".encode("utf-16le"):
        raise NtfsLogFileError("$LogFile client identity is invalid")
    if any(raw_name[8:]):
        raise NtfsLogFileError("$LogFile client name padding is invalid")
    if area.client_in_use_list == LFS_NO_CLIENT:
        # The log is closed: no client is in the in-use list, so there is no
        # checkpoint to read and nothing to analyse.
        return ()
    return (
        LogClient(
            index=area.client_in_use_list,
            oldest_lsn=oldest_lsn,
            restart_lsn=restart_lsn,
            prev_client=prev_client,
            next_client=next_client,
            sequence_number=sequence_number,
            name="NTFS",
        ),
    )


def _parse_restart_record(body: bytes, lsn: int) -> ClientRestartArea:
    if len(body) < NTFS_RESTART_SIZE:
        raise NtfsLogFileError(
            f"client restart record at LSN {lsn} carries {len(body)} bytes, "
            f"expected at least {NTFS_RESTART_SIZE}"
        )
    (
        major,
        minor,
        check_point_start,
        open_attr_lsn,
        attr_names_lsn,
        dirty_pages_lsn,
        transact_lsn,
        open_attr_len,
        attr_names_len,
        dirty_pages_len,
        transact_len,
    ) = NTFS_RESTART.unpack_from(body, 0)
    if major > 2:
        raise NtfsLogFileError(
            f"client restart record version {major} is outside the parsed profile"
        )
    return ClientRestartArea(
        lsn=lsn,
        major_version=major,
        minor_version=minor,
        check_point_start=check_point_start,
        open_attribute_table_lsn=open_attr_lsn,
        attribute_names_lsn=attr_names_lsn,
        dirty_page_table_lsn=dirty_pages_lsn,
        transaction_table_lsn=transact_lsn,
        open_attribute_length=open_attr_len,
        attribute_names_length=attr_names_len,
        dirty_page_length=dirty_pages_len,
        transaction_table_length=transact_len,
    )


def _requires_target_attr(operation: int) -> bool:
    if operation >= len(_ATTRIBUTE_REQUIRED) * 8:
        return False
    return bool(_ATTRIBUTE_REQUIRED[operation // 8] & (1 << (operation % 8)))


def _parse_record(
    page: bytes,
    *,
    page_index: int,
    offset: int,
    free_offset: int,
    area: NtfsLogFileRestartArea,
) -> LogRecord:
    """Validate one LFS record header and its log-record body."""

    this_lsn, prev_lsn, undo_next, data_len, seq, index, rtype, txid, flags = (
        LFS_RECORD_HDR.unpack_from(page, offset)
    )
    if not this_lsn:
        raise _EndOfRecords()
    rec_hdr_len = area.log_record_header_length
    if rtype not in (LFS_CLIENT_RECORD, LFS_CLIENT_RESTART):
        raise NtfsLogFileError(f"log record at LSN {this_lsn} has unknown type {rtype}")
    if flags & LOG_RECORD_MULTI_PAGE:
        raise NtfsLogFileError(
            f"log record at LSN {this_lsn} is flagged multi-page; records that span "
            "pages are outside the parsed profile"
        )
    if index not in (0, LFS_NO_CLIENT):
        raise NtfsLogFileError(
            f"log record at LSN {this_lsn} belongs to client {index}, not the single "
            "parsed client"
        )
    if data_len % 8:
        raise NtfsLogFileError(
            f"log record at LSN {this_lsn} has an unaligned data length {data_len}"
        )
    end = offset + rec_hdr_len + data_len
    if end > free_offset:
        raise NtfsLogFileError(
            f"log record at LSN {this_lsn} ends at {end}, past the page's free-space "
            f"offset {free_offset}"
        )
    body = page[offset + rec_hdr_len : end]

    restart = None
    redo_op = undo_op = 0
    target_attr = lcns_follow = record_off = attr_off = cluster_off = 0
    target_vcn = 0
    page_lcns: tuple[int, ...] = ()
    redo_data = undo_data = b""
    if rtype == LFS_CLIENT_RESTART:
        restart = _parse_restart_record(body, this_lsn)
    else:
        if txid and (txid - 0x18) % 0x28:
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} has transact_id {txid}, which is not a "
                "transaction-table offset"
            )
        if len(body) < LOG_REC_HDR.size:
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} is shorter than its log-record header"
            )
        (
            redo_op,
            undo_op,
            redo_off,
            redo_len,
            undo_off,
            undo_len,
            target_attr,
            lcns_follow,
            record_off,
            attr_off,
            cluster_off,
            _reserved,
            target_vcn,
        ) = LOG_REC_HDR.unpack_from(body, 0)
        # check_log_rec (fslog.c:700-720) requires room for the LCN list and
        # 8-aligned payload offsets.
        minimum = LOG_REC_HDR.size + 8 * max(1, lcns_follow)
        if data_len < minimum:
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} declares {lcns_follow} LCNs but only "
                f"{data_len} bytes of client data (< {minimum})"
            )
        if redo_off % 8 or undo_off % 8:
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} has unaligned redo/undo offsets"
            )
        if (redo_len and redo_off < LOG_REC_HDR.size) or (
            undo_len and undo_off < LOG_REC_HDR.size
        ):
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} points redo/undo inside its own header"
            )
        for name, off, length in (
            ("redo", redo_off, redo_len),
            ("undo", undo_off, undo_len),
        ):
            if length and off + length > data_len:
                raise NtfsLogFileError(
                    f"log record at LSN {this_lsn} {name} slice [{off}:{off + length}) "
                    f"leaves the {data_len}-byte body"
                )
        if not target_attr and _requires_target_attr(redo_op):
            raise NtfsLogFileError(
                f"log record at LSN {this_lsn} performs "
                f"{OPERATION_NAMES.get(redo_op, hex(redo_op))} without a target attribute"
            )
        page_lcns = struct.unpack_from(
            f"<{max(1, lcns_follow)}Q", body, LOG_REC_HDR.size
        )
        redo_data = body[redo_off : redo_off + redo_len] if redo_len else b""
        undo_data = body[undo_off : undo_off + undo_len] if undo_len else b""
    return LogRecord(
        lsn=this_lsn,
        byte_offset=offset,
        page_index=page_index,
        record_type=rtype,
        transaction_id=txid,
        sequence_number=seq,
        client_index=index,
        prev_lsn=prev_lsn,
        undo_next_lsn=undo_next,
        flags=flags,
        data_length=data_len,
        redo_op=redo_op,
        undo_op=undo_op,
        redo_data=redo_data,
        undo_data=undo_data,
        target_attr=target_attr,
        lcns_follow=lcns_follow,
        record_off=record_off,
        attr_off=attr_off,
        cluster_off=cluster_off,
        target_vcn=target_vcn,
        page_lcns=tuple(page_lcns),
        restart=restart,
    )


def _scan_records(
    stream: bytes, area: NtfsLogFileRestartArea, *, first_page: int
) -> tuple[list[LogRecord], bool]:
    page_size = area.log_page_size
    records: list[LogRecord] = []
    usable = min(area.file_size, len(stream))
    truncated = len(stream) < area.file_size
    offset = first_page
    while offset + page_size <= usable:
        raw = stream[offset : offset + page_size]
        try:
            page = _fixup_page(raw, RCRD_MAGIC, page_size=page_size)
        except _UnusedPage:
            offset += page_size
            continue
        page_index = offset // page_size
        free_offset = struct.unpack_from("<H", page, 0x18)[0]
        if free_offset & 7 or free_offset > page_size:
            raise NtfsLogFileError(
                f"log page {page_index} has an invalid free-space offset {free_offset}"
            )
        if free_offset < area.log_page_data_offset:
            raise NtfsLogFileError(
                f"log page {page_index} free-space offset {free_offset} is below the "
                f"record area start {area.log_page_data_offset}"
            )
        cursor = area.log_page_data_offset
        while cursor + area.log_record_header_length <= free_offset:
            if not any(page[cursor : cursor + area.log_record_header_length]):
                break
            try:
                record = _parse_record(
                    page,
                    page_index=page_index,
                    offset=cursor,
                    free_offset=free_offset,
                    area=area,
                )
            except _EndOfRecords:
                break
            record = replace(record, byte_offset=offset + cursor)
            records.append(record)
            if len(records) > MAX_RECORDS:
                raise NtfsLogFileError("$LogFile exceeds the parsed record budget")
            cursor = _align8(
                cursor + area.log_record_header_length + record.data_length
            )
        offset += page_size
    return records, truncated


def analyze_logfile(stream: bytes, *, cluster_size: int = 1024) -> NtfsLogFileAnalysis:
    """Parse one ``$LogFile`` DATA stream into a non-executable replay plan.

    ``cluster_size`` is recorded for reporting only; the analyzer never resolves
    a record to image bytes (see :data:`EXECUTION_BLOCKERS`).
    """

    if not isinstance(stream, bytes | bytearray):
        raise NtfsLogFileError("$LogFile stream must be bytes")
    stream = bytes(stream)
    if isinstance(cluster_size, bool) or not isinstance(cluster_size, int):
        raise NtfsLogFileError("cluster size must be an integer")
    if cluster_size < SECTOR or cluster_size & (cluster_size - 1):
        raise NtfsLogFileError(f"cluster size {cluster_size} is not a power of two")

    area, page = parse_restart_pages(stream)
    if area.log_page_size != area.system_page_size:
        raise NtfsLogFileError(
            f"$LogFile page size {area.log_page_size} differs from the system page "
            f"{area.system_page_size}; mixed geometry is outside the parsed profile"
        )
    clients = parse_clients(page, area)
    # first_page (fslog.c:1362-1363): two restart pages plus two tail copies for
    # version 1.x, plus 32 "fast" pages for version 2.x.
    first_page = (0x22 if area.version[0] >= 2 else 4) * area.log_page_size
    records, truncated = _scan_records(stream, area, first_page=first_page)
    notes: list[str] = []
    if truncated:
        notes.append(
            f"$LogFile DATA stream holds {len(stream)} bytes but the restart area "
            f"declares {area.file_size}; only the available bytes were scanned"
        )
    # LSN order, not file order, is authoritative: the log is a ring.
    records.sort(key=lambda record: record.lsn)
    seen: dict[int, LogRecord] = {}
    for record in records:
        if record.lsn in seen:
            raise NtfsLogFileError(f"two log records claim LSN {record.lsn}")
        seen[record.lsn] = record

    restart_area: ClientRestartArea | None = None
    checkpoint = 0
    if clients:
        client = clients[0]
        if client.restart_lsn:
            record = seen.get(client.restart_lsn)
            if record is None:
                raise NtfsLogFileError(
                    f"client restart LSN {client.restart_lsn} is not in the parsed log"
                )
            if record.record_type != LFS_CLIENT_RESTART or record.restart is None:
                raise NtfsLogFileError(
                    f"client restart LSN {client.restart_lsn} is not a client restart record"
                )
            restart_area = record.restart
            checkpoint = restart_area.check_point_start or restart_area.lsn
    else:
        notes.append("$LogFile has no in-use client: the log is closed")

    transactions = _analysis_pass(records, checkpoint, notes)
    redo_plan, undo_plan = _build_plans(transactions, seen, notes)

    return NtfsLogFileAnalysis(
        restart=area,
        page_size=area.log_page_size,
        first_page=first_page,
        log_size=area.file_size,
        scanned_bytes=len(stream),
        current_lsn=area.current_lsn,
        checkpoint_lsn=checkpoint,
        oldest_lsn=min((c.oldest_lsn for c in clients if c.oldest_lsn), default=0),
        clients=clients,
        records=tuple(records),
        transactions=transactions,
        redo_plan=redo_plan,
        undo_plan=undo_plan,
        restart_area=restart_area,
        notes=tuple(notes),
        truncated=truncated,
        executable=False,
        blockers=EXECUTION_BLOCKERS,
    )


def _analysis_pass(
    records: list[LogRecord], checkpoint: int, notes: list[str]
) -> tuple[Transaction, ...]:
    """Rebuild the transaction table the way the analysis pass of log_replay does."""

    active: dict[int, list] = {}
    generations: list[tuple[int, list]] = []
    for record in records:
        if record.record_type != LFS_CLIENT_RECORD:
            continue
        if checkpoint and record.lsn < checkpoint:
            continue
        txid = record.transaction_id
        entry = active.get(txid)
        if entry is None:
            # transact_id is a transaction-table slot, not a globally unique
            # transaction identifier. ForgetTransaction frees the slot, which
            # may later be allocated to another transaction (fslog.c:4834-4846).
            entry = [TRANSACTION_ACTIVE, record.lsn, record.lsn, record.lsn, [], False]
            active[txid] = entry
            generations.append((txid, entry))
        else:
            entry[2] = record.lsn
        # The kernel analysis pass advances the transaction's undo pointer to
        # each record. CLR records redirect it to their explicit undo-next LSN.
        entry[3] = record.lsn
        if record.undo_op == 0x01:  # CompensationLogRecord steers undo_next_lsn
            entry[3] = record.undo_next_lsn
        entry[4].append(record.lsn)
        if record.redo_op == 0x18:  # EndTopLevelAction
            entry[2] = record.lsn
            entry[3] = record.undo_next_lsn
        elif record.redo_op == 0x19:  # PrepareTransaction
            entry[0] = TRANSACTION_PREPARED
        elif record.redo_op == 0x1A:  # CommitTransaction
            entry[0] = TRANSACTION_COMMITTED
        elif record.redo_op == 0x1B:  # ForgetTransaction frees the entry
            entry[5] = True
            active.pop(txid, None)
    return tuple(
        Transaction(
            transaction_id=txid,
            state=_STATE_NAMES[entry[0]],
            first_lsn=entry[1],
            prev_lsn=entry[2],
            undo_next_lsn=entry[3],
            record_lsns=tuple(entry[4]),
            forgotten=entry[5],
        )
        for txid, entry in generations
    )


def _step_reasons(record: LogRecord, operation: int) -> tuple[str, ...]:
    reasons = [
        "operation "
        f"{OPERATION_NAMES.get(operation, hex(operation))} needs the open attribute "
        "table to resolve its byte target (fslog.c do_action)"
    ]
    if record.lcns_follow == 0:
        reasons.append("record carries no page LCN, so its target is unmapped")
    if record.target_attr == 0:
        reasons.append("record names no target attribute")
    return tuple(reasons)


def _build_plans(
    transactions: tuple[Transaction, ...],
    seen: dict[int, LogRecord],
    notes: list[str],
) -> tuple[tuple[ReplayStep, ...], tuple[ReplayStep, ...]]:
    redo: list[ReplayStep] = []
    undo: list[ReplayStep] = []
    for transaction in transactions:
        if transaction.forgotten:
            # fslog.c:4834-4846: a forgotten transaction is neither redone nor
            # undone.  The v1 draft of #510 would have rolled it back.
            continue
        if transaction.state == "committed":
            for lsn in transaction.record_lsns:
                record = seen[lsn]
                if record.redo_op in SKIPPABLE_OPERATIONS or not record.redo_len:
                    continue
                redo.append(
                    ReplayStep(
                        lsn=lsn,
                        transaction_id=transaction.transaction_id,
                        kind="redo",
                        operation=record.redo_op,
                        length=record.redo_len,
                        payload=record.redo_data,
                        target_attr=record.target_attr,
                        target_vcn=record.target_vcn,
                        reasons=_step_reasons(record, record.redo_op),
                    )
                )
        if not transaction.undo_candidate:
            # Prepared, committed and forgotten transactions are never rolled
            # back (fslog.c:5204-5216).
            continue
        own_lsns = set(transaction.record_lsns)
        lsn = transaction.undo_next_lsn
        visited: set[int] = set()
        chain: list[LogRecord] = []
        valid_chain = True
        while lsn:
            if lsn in visited:
                notes.append(
                    f"transaction {transaction.transaction_id} has a cyclic "
                    f"undo chain at LSN {lsn}"
                )
                valid_chain = False
                break
            if lsn not in own_lsns or lsn not in seen:
                notes.append(
                    f"transaction {transaction.transaction_id} points undo_next_lsn "
                    f"{lsn} outside its parsed record chain"
                )
                valid_chain = False
                break
            visited.add(lsn)
            record = seen[lsn]
            chain.append(record)
            lsn = record.undo_next_lsn
        if valid_chain:
            for record in chain:
                if record.undo_op in SKIPPABLE_OPERATIONS or not record.undo_len:
                    continue
                undo.append(
                    ReplayStep(
                        lsn=record.lsn,
                        transaction_id=transaction.transaction_id,
                        kind="undo",
                        operation=record.undo_op,
                        length=record.undo_len,
                        payload=record.undo_data,
                        target_attr=record.target_attr,
                        target_vcn=record.target_vcn,
                        reasons=_step_reasons(record, record.undo_op),
                    )
                )
    return tuple(redo), tuple(undo)


def _read_runs(
    handle,
    runs: tuple[NtfsRun, ...],
    *,
    cluster_size: int,
    base: int,
    offset: int,
    length: int,
) -> bytes:
    """Read one range of a nonresident stream through its whole runlist."""

    chunks: list[bytes] = []
    covered = 0
    for run in runs:
        if covered >= length:
            break
        run_start = run.vcn * cluster_size
        run_end = (run.vcn + run.length) * cluster_size
        if run_end <= offset + covered:
            continue
        if run_start > offset + covered:
            raise NtfsLogFileError(
                f"cluster {(offset + covered) // cluster_size} is a hole in the runlist"
            )
        take = min(run_end - (offset + covered), length - covered)
        # A run maps stream bytes linearly: stream offset ``run_start + d``
        # lives at volume byte ``lcn * cluster_size + d`` for the whole run.
        absolute = base + run.lcn * cluster_size + (offset + covered - run_start)
        handle.seek(absolute)
        chunk = handle.read(take)
        if len(chunk) != take:
            raise NtfsLogFileError("runlist extends past the end of the image")
        chunks.append(chunk)
        covered += take
    if covered < length:
        raise NtfsLogFileError("runlist is shorter than the declared stream size")
    return b"".join(chunks)


def read_logfile_stream(
    image_path, *, offset: int = 0, volume_size: int | None = None
) -> tuple[bytes, int, NtfsAttributeSummary]:
    """Read ``$LogFile``'s DATA stream from a volume without writing anything.

    :func:`mountfile.ntfs_reader.probe_ntfs_metadata` validates the boot sector,
    the backup boot sector, ``$MFT`` record 0 and its mirror.  ``$LogFile`` is
    record 2, fetched through ``$MFT``'s own runlist, so a *dirty* volume -- which
    :class:`mountfile.ntfs_reader.NativeNtfsMetadataImage` refuses to open -- can
    still be inspected.  Anything outside the bounded nonresident, single-stream
    profile is refused with :class:`NtfsLogFileError`.
    """

    image_path = os.fspath(image_path)
    if volume_size is None:
        size_on_disk = os.path.getsize(image_path)
        if offset < 0 or offset >= size_on_disk:
            raise NtfsLogFileError("volume offset is outside the image")
        volume_size = size_on_disk - offset
    profile = probe_ntfs_metadata(image_path, offset=offset, volume_size=volume_size)
    boot = profile.boot
    mft_data: NtfsAttributeSummary | None = None
    for attribute in profile.attributes:
        if attribute.type_code == ATTR_DATA and not attribute.name:
            mft_data = attribute
            break
    if mft_data is None or not mft_data.nonresident or not mft_data.runs:
        raise NtfsLogFileError("$MFT data stream is not a bounded nonresident runlist")
    record_size = boot.file_record_size
    if record_size < SECTOR or record_size & (record_size - 1):
        raise NtfsLogFileError("$MFT record size is not a power-of-two sector multiple")
    record_start = LOGFILE_RECORD_NUMBER * record_size
    with open(image_path, "rb") as handle:
        raw_record = _read_runs(
            handle,
            mft_data.runs,
            cluster_size=boot.cluster_size,
            base=offset,
            offset=record_start,
            length=record_size,
        )
        try:
            header = parse_ntfs_file_record_header(raw_record)
            attributes = parse_ntfs_attribute_stream(
                header,
                cluster_count=boot.cluster_count,
                # Real $LogFile records, like the metafiles the reader already
                # walks, keep eight bytes of padding after the end marker.
                allow_end_marker_padding=True,
            )
        except NtfsMetadataError as exc:
            raise NtfsLogFileError(f"$LogFile MFT record is invalid: {exc}") from exc
        if any(a.type_code == ATTR_ATTRIBUTE_LIST for a in attributes):
            raise NtfsLogFileError(
                "$LogFile record uses $ATTRIBUTE_LIST; cross-record attribute "
                "resolution is outside the parsed profile"
            )
        logfile: NtfsAttributeSummary | None = None
        for attribute in attributes:
            if attribute.type_code == ATTR_DATA and not attribute.name:
                logfile = attribute
                break
        if logfile is None:
            raise NtfsLogFileError("$LogFile has no unnamed DATA attribute")
        if not logfile.nonresident or not logfile.runs:
            raise NtfsLogFileError("$LogFile DATA stream is not nonresident")
        declared = logfile.data_size or logfile.allocated_size or 0
        if declared < 2 * RESTART_PAGE_SIZE:
            raise NtfsLogFileError("$LogFile DATA stream is too small to parse")
        size = min(declared, MAX_LOG_BYTES)
        raw = _read_runs(
            handle,
            logfile.runs,
            cluster_size=boot.cluster_size,
            base=offset,
            offset=0,
            length=size,
        )
    return raw, boot.cluster_size, logfile


def analyze_image(
    image_path, *, offset: int = 0, volume_size: int | None = None
) -> NtfsLogFileAnalysis:
    """Read-only analysis of the ``$LogFile`` of an NTFS image file."""

    stream, cluster_size, _attribute = read_logfile_stream(
        image_path, offset=offset, volume_size=volume_size
    )
    return analyze_logfile(stream, cluster_size=cluster_size)


__all__ = [
    "BAAD_MAGIC",
    "BOUNDED_OVERLAY_OPERATIONS",
    "CHKD_MAGIC",
    "CLIENT_REC",
    "EXECUTION_BLOCKERS",
    "LFS_RECORD_HDR",
    "LOG_REC_HDR",
    "NTFS_RESTART",
    "NtfsLogFileAnalysis",
    "NtfsLogFileError",
    "NtfsReplayOverlay",
    "NtfsReplayOverlayError",
    "RCRD_MAGIC",
    "RSTR_MAGIC",
    "ClientRestartArea",
    "LogClient",
    "LogRecord",
    "OPERATION_NAMES",
    "ReplayStep",
    "ReplayTarget",
    "OverlayProvenance",
    "SKIPPABLE_OPERATIONS",
    "Transaction",
    "analyze_image",
    "analyze_logfile",
    "parse_clients",
    "parse_restart_pages",
    "replay_analysis_to_overlay",
    "read_logfile_stream",
]
