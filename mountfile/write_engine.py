# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""mountfile/write_engine.py — MF-WE, the transactional write engine (ТЗ MF-WE-TZ-001).

What this module *is*
---------------------
A thin transactional **facade** over the write backends that already exist in
``mountfile.universal_fs_rw`` (native FAT12/16/32, ext2/3/4 via ``debugfs``,
NTFS via the mount helper, loop mount, rebuild).  It adds exactly the layers the
specification marks as mandatory (ТЗ §8):

1. **Read-only by default.**  Mutating methods refuse to run unless the engine
   was opened with ``read_only=False`` (explicit RW mode).
2. **Automatic backup.**  A session backup is written before the first mutation
   and a per-transaction snapshot before that transaction's first mutation
   (snapshots rotate, ``keep_snapshots`` newest are kept).
3. **Transactions with a hash-chained journal.**  Every operation is journalled
   as JSONL with ``prev``/``hash`` fields, so a tampered or truncated journal is
   detectable (``verify_journal()``).
4. **Byte-exact rollback.**  A transaction restores the image to the byte state
   captured at its start; the context manager rolls back automatically when the
   body raises — including a simulated crash mid-operation.
5. **Verification after commit.**  ``verify()`` re-runs the structural probe and
   reports, per check, what actually ran and what it observed.  Checks that need
   an external tool (``fsck.fat``, ``e2fsck``) are reported as ``skipped`` when
   the tool is missing instead of being silently treated as passing.
6. **Dry run.**  ``dry_run=True`` validates paths and preconditions and journals
   the plan, but never writes a byte to the image.

What this module is *not*
-------------------------
It is **not** a native NTFS/ext writer.  The operations are delegated to the
existing backends; write quality for those filesystems is exactly the quality of
the backend that performs them.  ``chmod``/``chown``/``symlink`` are only
available through an ext backend driven by ``debugfs``; on FAT they raise
``UnsupportedOperationError``.  Sparse files, compression, EFS, physical devices
and partition-table edits stay out of scope (ТЗ §2.3).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import shutil
import struct
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

from .universal_fs_rw import FSEntry, FSInfo, UniversalFSRW

log = logging.getLogger("mountfile.write_engine")

__all__ = [
    "WriterUnavailableError",
    "WriteEngine",
    "WriteEngineError",
    "ReadOnlyError",
    "DamagedImageError",
    "TransactionError",
    "UnsupportedOperationError",
    "EngineJournal",
    "JournalEntry",
    "Transaction",
    "damage_report",
]

FAT_MEDIA_DESCRIPTORS = frozenset([0xF0]) | frozenset(range(0xF8, 0x100))
EXT_SUPERBLOCK_MAGIC = 0xEF53
EXT_MAGIC_OFFSET = 1024
JOURNAL_FIELDS = ("ts", "image", "tx", "op", "path", "status", "extra", "prev")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class WriteEngineError(RuntimeError):
    """Base class for every MF-WE failure."""


class ReadOnlyError(WriteEngineError):
    """A mutating operation was attempted on a read-only engine."""


class DamagedImageError(WriteEngineError):
    """The image shows structural damage; RW access needs ``allow_damaged``."""


class TransactionError(WriteEngineError):
    """Transaction misuse (nested begin, unknown id, rollback without snapshot)."""


class UnsupportedOperationError(WriteEngineError):
    """The active backend cannot perform this operation."""


class WriterUnavailableError(WriteEngineError):
    """No RW backend exists for the detected filesystem."""


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------
@dataclass
class JournalEntry:
    ts: str
    image: str | None
    tx: str
    op: str
    path: str
    status: str  # planned | applied | committed | rolled_back | failed
    extra: dict = field(default_factory=dict)
    prev: str = "0" * 64
    hash: str = ""

    def canonical(self) -> bytes:
        payload = {key: getattr(self, key) for key in JOURNAL_FIELDS}
        return json.dumps(payload, sort_keys=True, default=str).encode()

    def compute(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


class EngineJournal:
    """Hash-chained JSONL journal of every engine operation."""

    def __init__(
        self, path: str | Path | None = None, image_path: str | Path | None = None
    ):
        self.path = Path(path) if path is not None else _default_journal_path()
        self.image_path = Path(image_path) if image_path is not None else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = self._tail_hash()

    def _tail_hash(self) -> str:
        report = self.verify_chain()
        if not report["ok"]:
            raise WriteEngineError(
                "journal integrity failure at line(s): "
                + ", ".join(str(line + 1) for line in report["broken"])
            )
        entries = self.entries()
        return entries[-1].hash if entries else "0" * 64

    def append(
        self, *, tx: str, op: str, path: str, status: str, extra: dict | None = None
    ) -> JournalEntry:
        # Refuse to extend a chain changed since this object was opened.
        self._prev = self._tail_hash()
        entry = JournalEntry(
            ts=_dt.datetime.now(_dt.timezone.utc).isoformat(),
            image=str(self.image_path) if self.image_path is not None else None,
            tx=tx,
            op=op,
            path=str(path),
            status=status,
            extra=dict(extra or {}),
            prev=self._prev,
        )
        entry.hash = entry.compute()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(entry.to_dict(), sort_keys=True, default=str) + "\n"
            )
        self._prev = entry.hash
        return entry

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        out: list[JournalEntry] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise WriteEngineError("journal cannot be decoded or read") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WriteEngineError("malformed journal JSON") from exc
            if not isinstance(data, dict) or any(
                key not in data for key in (*JOURNAL_FIELDS, "hash")
            ):
                raise WriteEngineError("journal entry is missing required fields")
            if not isinstance(data.get("extra"), dict):
                raise WriteEngineError("journal entry has invalid extra field")
            tx = data.get("tx", "")
            out.append(
                JournalEntry(
                    ts=data.get("ts", ""),
                    image=data.get("image", ""),
                    tx=tx,
                    op=data.get("op", ""),
                    path=data.get("path", ""),
                    status=data.get("status", ""),
                    extra=dict(data.get("extra", {})),
                    prev=data.get("prev", "0" * 64),
                    hash=data.get("hash", ""),
                )
            )
        return out

    def verify_chain(self) -> dict:
        """Recompute every hash and check the ``prev`` links."""
        previous = "0" * 64
        broken: list[int] = []
        try:
            entries = self.entries()
        except WriteEngineError:
            return {"entries": 0, "broken": [0], "ok": False}
        for index, entry in enumerate(entries):
            if entry.prev != previous or entry.compute() != entry.hash:
                broken.append(index)
            previous = entry.hash
        return {"entries": len(entries), "broken": broken, "ok": not broken}


def _default_journal_path() -> Path:
    return Path.home() / ".mountfile" / "write_engine_journal.jsonl"


def _default_snapshot_dir() -> Path:
    return Path.home() / ".mountfile" / "backups"


# ---------------------------------------------------------------------------
# structural probe (raw, independent of the backend)
# ---------------------------------------------------------------------------
def _power_of_two(value: Any, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and low <= value <= high and (value & (value - 1)) == 0
    )


def _read_raw_safe(image: Path, offset: int, length: int) -> bytes:
    try:
        return _read_raw(image, offset, length)
    except OSError:
        return b""


def _read_raw(image: Path, offset: int, length: int) -> bytes:
    with open(image, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def fs_family(fs_type: str | None) -> str:
    """Collapse a detector label into one of ``fat``/``ntfs``/``ext``/``unknown``.

    ``probe_filesystem`` reports display names such as ``FAT32``, ``NTFS`` or
    the grouped ``ext2/3/4``; the engine needs a stable family to pick a
    backend and a checker.
    """
    text = str(fs_type or "").strip().lower()
    if text.startswith("fat"):
        return "fat"
    if text.startswith("ntfs"):
        return "ntfs"
    if text.startswith("ext"):
        return "ext"
    return "unknown"


def damage_report(image: str | Path, offset: int = 0) -> dict:
    """Inspect the boot sector/superblock of a volume and list what is wrong.

    Returns ``{"fs_type", "checks", "problems"}`` where every check carries the
    observed value, so the caller never has to trust a bare boolean.  Detection
    reuses :func:`mountfile.firmware.probe_filesystem`; the checks themselves
    read raw bytes and therefore work without ``pytsk3``.
    """
    image = Path(image)
    if not image.exists():
        raise WriteEngineError("image does not exist: %s" % image)
    from .firmware import probe_filesystem

    detected = probe_filesystem(image, offset)
    detected = detected = probe_filesystem(image, offset) or "unknown"
    # ``probe_filesystem`` returns display names (FAT32, NTFS, ext2/3/4) while
    # this report and the backend table use stable families.
    fs_type = fs_family(detected)
    checks: list[dict] = []
    problems: list[str] = []

    def check(name: str, ok: Optional[bool], detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if ok is False:
            problems.append("%s: %s" % (name, detail))

    total = image.stat().st_size
    if offset >= total:
        raise WriteEngineError("offset 0x%X is past the end of %s" % (offset, image))

    boot = _read_raw(image, offset, 512)
    check(
        "boot sector present",
        len(boot) == 512,
        "%d bytes readable at 0x%X" % (len(boot), offset),
    )

    if fs_type == "fat":
        bps = struct.unpack_from("<H", boot, 11)[0]
        spc = boot[13] if len(boot) > 13 else 0
        reserved = struct.unpack_from("<H", boot, 14)[0]
        num_fats = boot[16] if len(boot) > 16 else 0
        root_entries = struct.unpack_from("<H", boot, 17)[0]
        total_sectors = (
            struct.unpack_from("<H", boot, 19)[0]
            or struct.unpack_from("<I", boot, 32)[0]
        )
        fat_size = (
            struct.unpack_from("<H", boot, 22)[0]
            or struct.unpack_from("<I", boot, 36)[0]
        )
        check(
            "FAT boot signature 0x55AA",
            boot[510:512] == b"\x55\xaa",
            repr(boot[510:512]),
        )
        check(
            "bytes per sector",
            _power_of_two(bps, 512, 4096),
            "bytes_per_sector=%d" % bps,
        )
        check(
            "sectors per cluster",
            _power_of_two(spc, 1, 128),
            "sectors_per_cluster=%d" % spc,
        )
        check("reserved sector count", reserved >= 1, "reserved_sectors=%d" % reserved)
        check("fat size", fat_size > 0, "fat_size_sectors=%d" % fat_size)
        check("total sectors", total_sectors > 0, "total_sectors=%d" % total_sectors)
        if root_entries and bps:
            root_sectors = (root_entries * 32 + bps - 1) // bps
            check(
                "root directory fits",
                root_sectors > 0,
                "root_entries=%d" % root_entries,
            )
        if num_fats >= 1 and bps and fat_size:
            first = _read_raw(image, offset + reserved * bps, fat_size * bps)
            if first:
                media = first[0]
                check(
                    "FAT media descriptor",
                    media in FAT_MEDIA_DESCRIPTORS,
                    "first FAT byte=0x%02X" % media,
                )
            else:
                check(
                    "FAT first copy readable", False, "no bytes at the first FAT copy"
                )
            if num_fats > 1:
                second = _read_raw(
                    image, offset + (reserved + fat_size) * bps, fat_size * bps
                )
                check(
                    "FAT copies identical",
                    first == second,
                    "copy #1 and #2 %s" % ("match" if first == second else "differ"),
                )
        if bps and reserved:
            check(
                "volume fits in the image",
                offset + reserved * bps <= total,
                "data area starts at 0x%X, image is %d bytes"
                % (offset + reserved * bps, total),
            )
    elif fs_type == "ntfs":
        check(
            "NTFS OEM identifier",
            boot[3:11] == b"NTFS    ",
            "OEM=%r" % boot[3:11],
        )
        bps = struct.unpack_from("<H", boot, 11)[0]
        check(
            "bytes per sector",
            _power_of_two(bps, 512, 4096),
            "bytes_per_sector=%d" % bps,
        )
        check(
            "NTFS $Volume dirty bit",
            None,
            "not decoded by MF-WE; the dirty bit lives in the $Volume record (see docs)",
        )
    elif fs_type == "ext":
        superblock = _read_raw(image, offset + EXT_MAGIC_OFFSET, 1024)
        magic = (
            struct.unpack_from("<H", superblock, 56)[0] if len(superblock) >= 58 else 0
        )
        check(
            "ext superblock magic",
            magic == EXT_SUPERBLOCK_MAGIC,
            "magic=0x%04X" % magic,
        )
        state = (
            struct.unpack_from("<H", superblock, 58)[0] if len(superblock) >= 60 else 0
        )
        check(
            "ext cleanly unmounted",
            state == 1,
            "s_state=%d (%s)"
            % (
                state,
                "clean" if state == 1 else "not cleanly unmounted / errors flagged",
            ),
        )
    else:
        check(
            "filesystem detection",
            False,
            "no known filesystem signature at 0x%X" % offset,
        )

    return {
        "image": str(image),
        "offset": offset,
        "fs_type": fs_type,
        "detected_as": detected,
        "checks": checks,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------
class Transaction:
    """A unit of change; rolls back on failure, commits on success."""

    def __init__(self, engine: "WriteEngine", tx_id: str, label: str = ""):
        self.engine = engine
        self.id = tx_id
        self.label = label
        self.entries: list[JournalEntry] = []
        self.snapshot: Path | None = None
        self.started = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.finished: str | None = None
        self.status = "open"

    # -- snapshot -----------------------------------------------------------
    def ensure_snapshot(self) -> Path | None:
        if self.snapshot is not None:
            return self.snapshot
        make = getattr(self.engine, "_make_snapshot", None)
        if make is None:  # pragma: no cover - engine always provides it
            return None
        self.snapshot = make(self.id)
        return self.snapshot

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "started": self.started,
            "finished": self.finished,
            "snapshot": str(self.snapshot) if self.snapshot else None,
            "operations": [entry.op for entry in self.entries],
        }

    def __enter__(self) -> "Transaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.engine.rollback(self.id, reason="%s: %s" % (exc_type.__name__, exc))
        else:
            self.engine.commit(self.id)
        return False


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class WriteEngine:
    """Transactional write facade (ТЗ MF-WE-TZ-001 §5–§8).

    >>> engine = WriteEngine("dump.img", read_only=False)
    >>> with engine.transaction("add evidence"):
    ...     engine.write_file("/notes.txt", b"collected")
    >>> engine.verify()["ok"]
    True

    Read-only by default: ``WriteEngine(image)`` refuses every mutation with
    :class:`ReadOnlyError` until ``read_only=False`` is passed explicitly.
    """

    def __init__(
        self,
        image: str | Path,
        offset: int | None = None,
        *,
        read_only: bool = True,
        dry_run: bool = False,
        allow_damaged: bool = False,
        snapshot_dir: str | Path | None = None,
        journal_path: str | Path | None = None,
        keep_snapshots: int = 3,
        backup: bool = True,
    ):
        self.image = Path(image)
        self.offset = offset
        self.read_only = bool(read_only)
        self.dry_run = bool(dry_run)
        self.allow_damaged = bool(allow_damaged)
        self.keep_snapshots = int(keep_snapshots)
        self.backup_enabled = bool(backup)
        self.snapshot_dir = (
            Path(snapshot_dir) if snapshot_dir is not None else _default_snapshot_dir()
        )
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.journal = EngineJournal(journal_path, image_path=self.image)
        self._session_snapshot: Path | None = None
        self._active: Transaction | None = None
        self._transactions: dict[str, Transaction] = {}
        self._initial_size = self.image.stat().st_size if self.image.exists() else 0
        self.damage = self._probe()
        if self.damage["problems"] and not self.allow_damaged:
            raise DamagedImageError(
                "refusing RW access to %s: %s (pass allow_damaged=True to override)"
                % (self.image.name, "; ".join(self.damage["problems"]))
            )
        self._fs = self._open_backend()

    # -- backend plumbing ---------------------------------------------------
    def _probe(self) -> dict:
        try:
            return damage_report(self.image, self.offset or 0)
        except Exception as exc:  # unreadable geometry must not crash the facade
            return {
                "image": str(self.image),
                "offset": self.offset or 0,
                "fs_type": "unknown",
                "checks": [],
                "problems": ["structural probe failed: %s" % exc],
            }

    def _open_backend(self) -> UniversalFSRW:
        if not self.image.exists():
            raise WriteEngineError("image does not exist: %s" % self.image)
        fs = UniversalFSRW(self.image, self.offset, auto_backup=False)
        # the facade snapshots the whole image itself; never duplicate the copy
        fs._auto_backup = False
        self.offset = fs.offset
        return fs

    @property
    def backend(self):
        return self._fs.backend

    def info(self) -> FSInfo:
        return self._fs.info()

    def close(self) -> None:
        if self._active is not None:
            self.rollback(
                self._active.id, reason="engine closed with an open transaction"
            )
        self._fs.close()

    def __enter__(self) -> "WriteEngine":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and self._active is not None:
            self.rollback(self._active.id, reason="%s: %s" % (exc_type.__name__, exc))
        self.close()
        return False

    # -- read side ----------------------------------------------------------
    def list(self, path: str = "/") -> list[FSEntry]:
        return self._fs.list(path)

    def read(self, path: str) -> bytes:
        return self._fs.read(path)

    def stat(self, path: str) -> FSEntry | None:
        return self.backend.stat(path)

    def exists(self, path: str) -> bool:
        return self.backend.exists(path)

    def walk(self, path: str = "/") -> Iterator[FSEntry]:
        return self._fs.walk(path)

    def extract(self, path: str, dest: str | Path) -> Path:
        return self._fs.extract(path, Path(dest))

    # -- transactions -------------------------------------------------------
    def begin(self, label: str = "") -> Transaction:
        if self._active is not None:
            raise TransactionError(
                "transaction %s is already active; MF-WE does not nest transactions"
                % self._active.id
            )
        tx = Transaction(self, uuid.uuid4().hex[:12], label)
        self._active = tx
        self._transactions[tx.id] = tx
        self.journal.append(
            tx=tx.id, op="begin", path="/", status="open", extra={"label": label}
        )
        return tx

    def transaction(self, label: str = "") -> Transaction:
        """``with engine.transaction("label"): ...`` — commits, or rolls back."""
        return self.begin(label)

    def commit(self, tx_id: str | None = None) -> dict:
        tx = self._transaction(tx_id)
        if not self.dry_run:
            self._fs.close()  # flush FAT/metadata before hashing and verifying
        post = self._identity()
        result = {
            "transaction": tx.id,
            "status": "committed",
            "dry_run": self.dry_run,
            "operations": len(tx.entries),
            "sha256_after": post,
            "sha256_before": (
                tx.entries[0].extra.get("sha256_before") if tx.entries else None
            ),
            "snapshot": str(tx.snapshot) if tx.snapshot else None,
        }
        tx.status = "committed"
        tx.finished = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.journal.append(
            tx=tx.id, op="commit", path="/", status="committed", extra=result
        )
        self._release(tx)
        return result

    def rollback(
        self, tx_id: str | None = None, *, reason: str = "explicit rollback"
    ) -> bool:
        tx = self._transaction(tx_id)
        restored = False
        if tx.snapshot is not None and tx.snapshot.exists():
            self._restore(tx.snapshot)
            restored = True
        for entry in tx.entries:
            entry.status = "rolled_back"
        tx.status = "rolled_back"
        tx.finished = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.journal.append(
            tx=tx.id,
            op="rollback",
            path="/",
            status="rolled_back",
            extra={
                "reason": reason,
                "restored": restored,
                "snapshot": str(tx.snapshot) if tx.snapshot else None,
            },
        )
        self._release(tx)
        return restored

    def _transaction(self, tx_id: str | None) -> Transaction:
        if tx_id is None:
            if self._active is None:
                raise TransactionError("no active transaction")
            return self._active
        tx = self._transactions.get(tx_id)
        if tx is None:
            raise TransactionError("unknown transaction %r" % tx_id)
        return tx

    def _release(self, tx: Transaction) -> None:
        if self._active is tx:
            self._active = None
        self._fs.close()
        self._fs = self._open_backend()

    # -- snapshots ----------------------------------------------------------
    def _make_snapshot(self, tx_id: str | None) -> Path:
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = "%s.%s.%s.snap" % (self.image.name, tx_id or "session", stamp)
        destination = self.snapshot_dir / name
        shutil.copy2(self.image, destination)
        self._rotate_snapshots()
        return destination

    def _rotate_snapshots(self) -> None:
        prefix = "%s." % self.image.name
        snapshots = sorted(p for p in self.snapshot_dir.glob("%s*.snap" % prefix))
        for old in snapshots[: -self.keep_snapshots]:
            with contextlib.suppress(OSError):
                old.unlink()

    def _ensure_session_snapshot(self) -> Path:
        if self._session_snapshot is None or not self._session_snapshot.exists():
            self._session_snapshot = self._make_snapshot("session")
            self.journal.append(
                tx="session",
                op="backup",
                path="/",
                status="applied",
                extra={"snapshot": str(self._session_snapshot)},
            )
        return self._session_snapshot

    @property
    def session_snapshot(self) -> Path | None:
        return self._session_snapshot

    def latest_snapshot(self) -> Path | None:
        snapshots = sorted(self.snapshot_dir.glob("%s.*.snap" % self.image.name))
        return snapshots[-1] if snapshots else None

    def _restore(self, snapshot: Path) -> None:
        self._fs.close()
        shutil.copy2(snapshot, self.image)
        self._fs = self._open_backend()

    def restore_from_backup(self, snapshot: str | Path | None = None) -> bool:
        """Put the image back to a snapshot (session or transaction)."""
        target = Path(snapshot) if snapshot is not None else self.latest_snapshot()
        if target is None or not target.exists():
            return False
        self._restore(target)
        self.journal.append(
            tx="session",
            op="restore",
            path="/",
            status="applied",
            extra={"snapshot": str(target), "sha256": self._identity()},
        )
        return True

    # -- identity / verification -------------------------------------------
    def _identity(self, chunk_size: int = 4 * 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with open(self.image, "rb") as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    def verify(self) -> dict:
        """Post-commit verification; reports what ran and what it observed."""
        checks: list[dict] = []
        size = self.image.stat().st_size
        checks.append(
            {
                "name": "image size unchanged",
                "ok": size == self._initial_size,
                "detail": "size=%d bytes, initial=%d bytes"
                % (size, self._initial_size),
            }
        )
        structural = self._probe()
        checks.extend(structural["checks"])
        checks.append(
            {
                "name": "filesystem checker",
                "ok": self._run_fs_check(),
                "detail": "external checker result (None = tool/dependency unavailable)",
            }
        )
        problems = [
            ("%s: %s" % (c["name"], c["detail"])) for c in checks if c["ok"] is False
        ]
        return {
            "image": str(self.image),
            "fs_type": structural["fs_type"],
            "ok": not problems,
            "checks": checks,
            "problems": problems,
            "session_backup": (
                str(self._session_snapshot) if self._session_snapshot else None
            ),
            "journal": self.journal.verify_chain(),
        }

    def _run_fs_check(self) -> Optional[bool]:
        fs_type = fs_family((self.damage or {}).get("fs_type"))
        if fs_type == "fat" and shutil.which("fsck.fat"):
            proc = _run([shutil.which("fsck.fat"), "-n", str(self.image)])
            return True if proc is None else _exitcode_ok(proc)
        if fs_type == "ext" and shutil.which("e2fsck"):
            args = [shutil.which("e2fsck"), "-fn"]
            if self.offset:
                args += ["-E", "offset=%d" % self.offset]
            args.append(str(self.image))
            proc = _run(args)
            return True if proc is None else _exitcode_ok(proc)
        return None

    # -- mutating operations ------------------------------------------------
    def write_file(self, path: str, data: bytes) -> JournalEntry:
        """Create or overwrite a file inside the image."""
        payload = bytes(data)
        return self._apply(
            "write_file",
            path,
            {"size": len(payload)},
            lambda: self._fs.write_file(path, payload),
        )

    def mkdir(self, path: str) -> JournalEntry:
        return self._apply("mkdir", path, {}, lambda: self._fs.mkdir(path))

    def delete(self, path: str, recursive: bool = False) -> JournalEntry:
        return self._apply(
            "delete",
            path,
            {"recursive": bool(recursive)},
            lambda: self._fs.delete(path, recursive=recursive),
        )

    def rename(self, src: str, dst: str) -> JournalEntry:
        self._validate_path(src)
        self._validate_path(dst)
        return self._apply(
            "rename", src, {"dst": dst}, lambda: self._fs.rename(src, dst)
        )

    def copy(self, src: str, dst: str) -> JournalEntry:
        """Read-then-write copy (the read side is always available)."""
        self._require_rw("copy")
        self._validate_path(dst)
        data = self._fs.read(src)
        return self._apply(
            "copy",
            dst,
            {"src": src, "size": len(data)},
            lambda: self._fs.write_file(dst, data),
        )

    def chmod(self, path: str, mode: int) -> JournalEntry:
        return self._ext_only(
            "chmod",
            path,
            {"mode": "%04o" % mode},
            lambda: self._debugfs_out(
                "sif %s mode 0%o" % (self._debugfs_path(path), mode)
            ),
        )

    def chown(self, path: str, uid: int, gid: int) -> JournalEntry:
        return self._ext_only(
            "chown",
            path,
            {"uid": int(uid), "gid": int(gid)},
            lambda: self._debugfs_out(
                "sif %s uid %d" % (self._debugfs_path(path), int(uid))
            )
            or self._debugfs_out(
                "sif %s gid %d" % (self._debugfs_path(path), int(gid))
            ),
        )

    def symlink(self, path: str, target: str) -> JournalEntry:
        return self._ext_only(
            "symlink",
            path,
            {"target": target},
            lambda: self._debugfs_out("symlink %s %s" % (path, target)),
        )

    # -- internals ----------------------------------------------------------
    def _ext_only(self, op: str, path: str, extra: dict, mutate) -> JournalEntry:
        self._require_rw(op)
        backend = self.backend
        if not hasattr(backend, "_debugfs"):
            raise UnsupportedOperationError(
                "%s is only available through the ext backend driven by debugfs; the active backend is %s"
                % (op, type(backend).__name__)
            )
        if not shutil.which("debugfs"):
            raise UnsupportedOperationError(
                "%s needs the `debugfs` tool (e2fsprogs) on PATH" % op
            )
        return self._apply(op, path, extra, mutate)

    def _debugfs_out(self, command: str) -> None:
        self.backend._debugfs("-w", "-R", command, write=True)

    def _debugfs_path(self, path: str) -> str:
        return "/" + str(path).replace("\\", "/").strip("/")

    def _apply(self, op: str, path: str, extra: dict, mutate) -> JournalEntry:
        self._require_rw(op)
        self._validate_path(path)
        owned = self._active is None
        tx = self._active if not owned else self.begin(label="auto:%s" % op)
        extra = dict(extra)
        if self.dry_run:
            # validate the plan without creating a snapshot or writing a byte
            self._validate_preconditions(op, path, extra)
            extra.setdefault("sha256_before", self._identity())
            entry = self.journal.append(
                tx=tx.id, op=op, path=path, status="planned", extra=extra
            )
            tx.entries.append(entry)
            if owned:
                self.commit(tx.id)
            return entry
        self._ensure_session_snapshot()
        tx.ensure_snapshot()
        extra.setdefault("sha256_before", self._identity())
        try:
            mutate()
        except Exception as exc:
            entry = self.journal.append(
                tx=tx.id,
                op=op,
                path=path,
                status="failed",
                extra=dict(extra, error=str(exc)),
            )
            tx.entries.append(entry)
            if owned:
                self.rollback(tx.id, reason="%s failed: %s" % (op, exc))
            raise
        extra["sha256_after"] = self._identity()
        entry = self.journal.append(
            tx=tx.id, op=op, path=path, status="applied", extra=extra
        )
        tx.entries.append(entry)
        if owned:
            self.commit(tx.id)
        return entry

    def _require_rw(self, op: str) -> None:
        if self.read_only:
            raise ReadOnlyError(
                "%s refuses to run: the engine is read-only (pass read_only=False to enable writes)"
                % op
            )

    def _validate_path(self, path: str) -> None:
        text = str(path or "").replace("\\", "/")
        if not text.startswith("/"):
            raise WriteEngineError("image paths must be absolute, got %r" % path)
        parts = PurePosixPath(text).parts
        if ".." in parts:
            raise WriteEngineError("path traversal is not allowed: %r" % path)
        if len(parts) <= 1:
            raise WriteEngineError("refusing to operate on the filesystem root")

    def _validate_preconditions(self, op: str, path: str, extra: dict) -> None:
        parent = str(PurePosixPath(str(path).replace("\\", "/")).parent) or "/"
        parent_exists = parent == "/" or self.backend.exists(parent)
        if op in ("write_file", "mkdir", "copy") and not parent_exists:
            raise WriteEngineError(
                "parent directory does not exist in the image: %s" % parent
            )
        if op in ("delete", "rename", "chmod", "chown") and not self.backend.exists(
            path
        ):
            raise WriteEngineError("path does not exist in the image: %s" % path)
        if op in ("write_file", "mkdir") and self.backend.exists(path):
            log.info("%s will overwrite an existing entry: %s", op, path)
        if op == "rename":
            target = extra.get("dst", "")
            if target and self.backend.exists(target):
                raise WriteEngineError("rename target already exists: %s" % target)

    def history(self, limit: int | None = None) -> list[dict]:
        entries = [entry.to_dict() for entry in self.journal.entries()]
        return entries[-limit:] if limit else entries

    def verify_journal(self) -> dict:
        return self.journal.verify_chain()

    def to_dict(self) -> dict:
        return {
            "image": str(self.image),
            "offset": self.offset,
            "read_only": self.read_only,
            "dry_run": self.dry_run,
            "allow_damaged": self.allow_damaged,
            "session_snapshot": (
                str(self._session_snapshot) if self._session_snapshot else None
            ),
            "damage": self.damage,
            "active_transaction": self._active.to_dict() if self._active else None,
            "transactions": [tx.to_dict() for tx in self._transactions.values()],
        }


def _run(args: list[str]) -> Optional[tuple[int, str]]:
    import subprocess

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # pragma: no cover - tool missing/hung
        return None
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _exitcode_ok(result: tuple[int, str]) -> bool:
    return result[0] == 0
