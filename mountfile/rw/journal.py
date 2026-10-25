"""Byte-exact, checksummed transaction journal shared by RW adapters."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
import warnings
from pathlib import Path
from typing import Optional


class Journal:
    """SQLite journal with verifiable record links and byte-range recovery.

    The record chain detects edits to linked rows and removal/reordering of
    interior rows. It is not an authenticated log: an attacker able to rewrite
    the complete database can recompute the chain, so export an external anchor
    when tamper evidence is required.
    """

    _PENDING_STATUSES = (
        "pending",
        "recovery_required",
        "legacy_pending",
        "legacy_recovery_required",
    )

    def __init__(self, image_path: Path):
        self.image_path = Path(image_path)
        self.db_path = self.image_path.with_suffix(
            self.image_path.suffix + ".rw-journal.sqlite"
        )
        self.backup_dir = self.image_path.with_suffix(
            self.image_path.suffix + ".rw-backups"
        )
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                   tx_id TEXT PRIMARY KEY,
                   op TEXT NOT NULL,
                   path TEXT NOT NULL,
                   backup_path TEXT,
                   sha256 TEXT,
                   prev_hash TEXT NOT NULL,
                   created_at REAL NOT NULL,
                   offset INTEGER,
                   length INTEGER,
                   committed INTEGER NOT NULL DEFAULT 0,
                   status TEXT NOT NULL DEFAULT 'pending',
                   record_hash TEXT
               )"""
        )
        self._migrate()
        self._current: Optional[str] = None
        self._last_hash = self._get_last_hash()
        legacy = self._conn.execute(
            "SELECT COUNT(*) FROM journal WHERE status LIKE 'legacy_%'"
        ).fetchone()[0]
        unknown = self._conn.execute(
            "SELECT COUNT(*) FROM journal WHERE status='legacy_unknown'"
        ).fetchone()[0]
        if legacy:
            warnings.warn(
                "%d legacy journal record(s) lack verifiable chain metadata; "
                "%d have unknown commit state and will not be auto-restored. "
                "Inspect the journal before relying on recovery." % (legacy, unknown),
                RuntimeWarning,
                stacklevel=2,
            )

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(journal)")}
        had_committed = "committed" in columns
        had_status = "status" in columns
        had_record_hash = "record_hash" in columns
        if "offset" not in columns:
            self._conn.execute("ALTER TABLE journal ADD COLUMN offset INTEGER")
        if "length" not in columns:
            self._conn.execute("ALTER TABLE journal ADD COLUMN length INTEGER")
        if "committed" not in columns:
            self._conn.execute(
                "ALTER TABLE journal ADD COLUMN committed INTEGER NOT NULL DEFAULT 0"
            )
        if "status" not in columns:
            self._conn.execute(
                "ALTER TABLE journal ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
            )
            if had_committed:
                self._conn.execute(
                    "UPDATE journal SET status = CASE WHEN committed=1 "
                    "THEN 'legacy_committed' ELSE 'legacy_pending' END"
                )
            else:
                # The old schema never recorded commit state; guessing could
                # either restore a successful write or discard a real recovery.
                self._conn.execute(
                    "UPDATE journal SET status='legacy_unknown', committed=1"
                )
        if "record_hash" not in columns:
            self._conn.execute("ALTER TABLE journal ADD COLUMN record_hash TEXT")
            if had_status:
                self._conn.execute(
                    "UPDATE journal SET status = CASE "
                    "WHEN committed=1 THEN 'legacy_committed' "
                    "ELSE 'legacy_pending' END"
                )
            elif had_committed:
                self._conn.execute(
                    "UPDATE journal SET status = CASE WHEN committed=1 "
                    "THEN 'legacy_committed' ELSE 'legacy_pending' END"
                )
        self._conn.commit()

    @staticmethod
    def _record_hash(
        tx_id: str,
        op: str,
        path: str,
        sha256: Optional[str],
        prev_hash: str,
        offset: Optional[int],
        length: Optional[int],
        created_at: float,
    ) -> str:
        record = {
            "tx_id": tx_id,
            "op": op,
            "path": path,
            "sha256": sha256,
            "prev_hash": prev_hash,
            "offset": offset,
            "length": length,
            "created_at": created_at,
        }
        payload = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _get_last_hash(self) -> str:
        row = self._conn.execute(
            "SELECT record_hash FROM journal WHERE record_hash IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row and row[0] else "0" * 64

    def _refresh_record_hash(self, tx_id: str) -> str:
        row = self._conn.execute(
            "SELECT tx_id, op, path, sha256, prev_hash, offset, length, created_at "
            "FROM journal WHERE tx_id=?",
            (tx_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("transaction disappeared from the journal")
        digest = self._record_hash(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
        )
        self._conn.execute(
            "UPDATE journal SET record_hash=? WHERE tx_id=?", (digest, tx_id)
        )
        self._last_hash = digest
        return digest

    def verify_chain(self) -> dict:
        """Validate every new-format record and any available snapshot bytes."""
        rows = self._conn.execute(
            "SELECT tx_id, op, path, sha256, prev_hash, offset, length, created_at, "
            "record_hash, status, backup_path FROM journal ORDER BY rowid"
        ).fetchall()
        previous = "0" * 64
        checked = 0
        legacy_unverifiable = 0
        for row in rows:
            if row[8] is None and str(row[9]).startswith("legacy_"):
                legacy_unverifiable += 1
                continue
            if row[8] is None:
                return {
                    "valid": False,
                    "checked": checked,
                    "legacy_unverifiable": legacy_unverifiable,
                    "error": "record is missing its integrity hash: %s" % row[0],
                }
            if row[4] != previous:
                return {
                    "valid": False,
                    "checked": checked,
                    "legacy_unverifiable": legacy_unverifiable,
                    "error": "broken previous-hash link: %s" % row[0],
                }
            expected = self._record_hash(*row[:8])
            if expected != row[8]:
                return {
                    "valid": False,
                    "checked": checked,
                    "legacy_unverifiable": legacy_unverifiable,
                    "error": "record contents do not match integrity hash: %s" % row[0],
                }
            if row[10]:
                backup = Path(row[10])
                if not backup.is_file() or row[5] is None or row[6] is None:
                    return {
                        "valid": False,
                        "checked": checked,
                        "legacy_unverifiable": legacy_unverifiable,
                        "error": "snapshot metadata is invalid: %s" % row[0],
                    }
                if backup.stat().st_size != int(row[6]):
                    return {
                        "valid": False,
                        "checked": checked,
                        "legacy_unverifiable": legacy_unverifiable,
                        "error": "snapshot length mismatch: %s" % row[0],
                    }
                if self._hash_file(backup) != row[3]:
                    return {
                        "valid": False,
                        "checked": checked,
                        "legacy_unverifiable": legacy_unverifiable,
                        "error": "snapshot checksum mismatch: %s" % row[0],
                    }
            previous = row[8]
            checked += 1
        return {
            "valid": True,
            "checked": checked,
            "legacy_unverifiable": legacy_unverifiable,
            "error": None,
        }

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _remove_snapshot(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            warnings.warn(
                "Could not remove transaction snapshot %s: %s" % (path, exc),
                RuntimeWarning,
                stacklevel=2,
            )

    def begin(self, op: str, path: str) -> str:
        if self._current is not None:
            raise RuntimeError("a journal transaction is already active")
        chain = self.verify_chain()
        if not chain["valid"]:
            raise RuntimeError("journal integrity check failed: %s" % chain["error"])
        unresolved = self._conn.execute(
            "SELECT tx_id FROM journal WHERE status IN (%s) LIMIT 1"
            % ",".join("?" for _ in self._PENDING_STATUSES),
            self._PENDING_STATUSES,
        ).fetchone()
        if unresolved:
            raise RuntimeError(
                "journal recovery is required before another mutation (%s)"
                % unresolved[0]
            )
        tx_id = str(uuid.uuid4())
        self._current = tx_id
        self._conn.execute(
            "INSERT INTO journal(tx_id, op, path, prev_hash, created_at, committed, "
            "status) VALUES (?,?,?,?,?,0,'pending')",
            (tx_id, op, path, self._last_hash, time.time()),
        )
        self._refresh_record_hash(tx_id)
        self._conn.commit()
        return tx_id

    def backup_region(self, offset: int, length: int) -> Path:
        if self._current is None:
            raise RuntimeError("begin() must be called before backup_region()")
        if offset < 0 or length < 0 or offset + length > self.image_path.stat().st_size:
            raise ValueError("backup region is outside the image")
        existing = self._conn.execute(
            "SELECT backup_path FROM journal WHERE tx_id=?", (self._current,)
        ).fetchone()
        if existing and existing[0]:
            raise RuntimeError(
                "only one byte-range snapshot is allowed per transaction"
            )
        backup = self.backup_dir / (self._current + ".bin")
        digest = hashlib.sha256()
        remaining = length
        try:
            with self.image_path.open("rb") as source, backup.open("xb") as target:
                source.seek(offset)
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise RuntimeError(
                            "short read while creating transaction snapshot"
                        )
                    target.write(block)
                    digest.update(block)
                    remaining -= len(block)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            self._remove_snapshot(backup)
            raise
        self._conn.execute(
            "UPDATE journal SET backup_path=?, sha256=?, offset=?, length=? "
            "WHERE tx_id=?",
            (str(backup), digest.hexdigest(), offset, length, self._current),
        )
        self._refresh_record_hash(self._current)
        self._conn.commit()
        return backup

    def commit(self) -> None:
        tx_id = self._current
        if tx_id is None:
            return
        row = self._conn.execute(
            "SELECT backup_path FROM journal WHERE tx_id=?", (tx_id,)
        ).fetchone()
        self._conn.execute(
            "UPDATE journal SET committed=1, status='committed', backup_path=NULL "
            "WHERE tx_id=?",
            (tx_id,),
        )
        self._conn.commit()
        self._current = None
        if row and row[0]:
            self._remove_snapshot(Path(row[0]))

    def cancel(self, tx_id: str, *, snapshot_failed: bool = False) -> None:
        row = self._conn.execute(
            "SELECT backup_path, status FROM journal WHERE tx_id=?", (tx_id,)
        ).fetchone()
        if row and row[0] and not snapshot_failed:
            raise RuntimeError("a transaction with a byte snapshot must use rollback()")
        status = (
            "legacy_rolled_back"
            if row and row[1].startswith("legacy_")
            else "rolled_back"
        )
        self._conn.execute(
            "UPDATE journal SET committed=1, status=?, backup_path=NULL WHERE tx_id=?",
            (status, tx_id),
        )
        self._conn.commit()
        if row and row[0]:
            self._remove_snapshot(Path(row[0]))
        if tx_id == self._current:
            self._current = None

    def rollback(self, tx_id: Optional[str] = None) -> bool:
        tid = tx_id or self._current
        if not tid:
            return False
        row = self._conn.execute(
            "SELECT backup_path, sha256, offset, length, status, committed "
            "FROM journal WHERE tx_id=?",
            (tid,),
        ).fetchone()
        if not row or row[4] not in self._PENDING_STATUSES or row[5]:
            if tid == self._current:
                self._current = None
            return False
        backup_path, checksum, offset, length = row[:4]
        if not backup_path or offset is None or length is None:
            self._mark_recovery_required(tid)
            return False
        backup = Path(backup_path)
        if (
            not backup.is_file()
            or backup.stat().st_size != int(length)
            or int(offset) + int(length) > self.image_path.stat().st_size
            or self._hash_file(backup) != checksum
        ):
            self._mark_recovery_required(tid)
            return False

        with backup.open("rb") as source, self.image_path.open("r+b") as image:
            image.seek(int(offset))
            remaining = int(length)
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    self._mark_recovery_required(tid)
                    return False
                if image.write(block) != len(block):
                    self._mark_recovery_required(tid)
                    return False
                remaining -= len(block)
            image.flush()
            os.fsync(image.fileno())

        status = (
            "legacy_rolled_back" if str(row[4]).startswith("legacy_") else "rolled_back"
        )
        self._conn.execute(
            "UPDATE journal SET committed=1, status=?, backup_path=NULL WHERE tx_id=?",
            (status, tid),
        )
        self._conn.commit()
        self._remove_snapshot(backup)
        if tid == self._current:
            self._current = None
        return True

    def _mark_recovery_required(self, tx_id: str) -> None:
        row = self._conn.execute(
            "SELECT status FROM journal WHERE tx_id=?", (tx_id,)
        ).fetchone()
        status = (
            "legacy_recovery_required"
            if row and str(row[0]).startswith("legacy_")
            else "recovery_required"
        )
        self._conn.execute("UPDATE journal SET status=? WHERE tx_id=?", (status, tx_id))
        self._conn.commit()
        if tx_id == self._current:
            self._current = None

    def uncommitted(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT tx_id, op, path, offset, length, backup_path, created_at, status "
            "FROM journal WHERE status IN (%s) ORDER BY rowid"
            % ",".join("?" for _ in self._PENDING_STATUSES),
            self._PENDING_STATUSES,
        ).fetchall()
        return [
            {
                "tx_id": row[0],
                "op": row[1],
                "path": row[2],
                "offset": row[3],
                "length": row[4],
                "backup": row[5],
                "created_at": row[6],
                "status": row[7],
            }
            for row in rows
        ]

    def legacy_unverifiable(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT tx_id, op, path, created_at FROM journal "
            "WHERE status LIKE 'legacy_%' ORDER BY rowid"
        ).fetchall()
        return [
            {"tx_id": row[0], "op": row[1], "path": row[2], "created_at": row[3]}
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()
