import hashlib
import io
import sqlite3

import pytest

from mountfile.rw.universal import FileStat, Journal, Mode, RWError, UniversalRW


def test_universal_journal_restores_the_recorded_byte_range(tmp_path):
    image = tmp_path / "disk.img"
    original = b"0123456789abcdef"
    image.write_bytes(original)
    journal = Journal(image)
    try:
        tx_id = journal.begin("write_file", "/evidence.bin")
        journal.backup_region(4, 6)
        with image.open("r+b") as stream:
            stream.seek(4)
            stream.write(b"CHANGE")

        assert journal.rollback(tx_id) is True
        assert image.read_bytes() == original
        assert journal.uncommitted() == []
    finally:
        journal.close()


def test_universal_journal_keeps_pending_record_when_snapshot_is_missing(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"forensic image")
    journal = Journal(image)
    try:
        tx_id = journal.begin("rename", "/old -> /new")

        assert journal.rollback(tx_id) is False
        assert [row["tx_id"] for row in journal.uncommitted()] == [tx_id]
    finally:
        journal.close()


def test_universal_journal_rejects_corrupt_backup_without_clearing_record(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"forensic image")
    journal = Journal(image)
    try:
        tx_id = journal.begin("write_file", "/evidence.bin")
        backup = journal.backup_region(0, 4)
        backup.write_bytes(b"BAD!")

        assert journal.rollback(tx_id) is False
        assert [row["tx_id"] for row in journal.uncommitted()] == [tx_id]
    finally:
        journal.close()


class FakeRW(UniversalRW):
    def __init__(self, image_path, *, fail_after_partial_write=False, atomic=False):
        super().__init__(image_path, mode=Mode.RW)
        self.fail_after_partial_write = fail_after_partial_write
        self.atomic_mutations = atomic
        self.mutation_called = False

    def _open_backend(self):
        pass

    def _close_backend(self):
        pass

    def _create_file(self, path, data, overwrite):
        raise NotImplementedError

    def _create_dir(self, path):
        raise NotImplementedError

    def _delete(self, path, recursive):
        raise NotImplementedError

    def _rename(self, src, dst):
        raise NotImplementedError

    def _write_file(self, path, data, offset):
        self.mutation_called = True
        with self.image_path.open("r+b") as stream:
            stream.seek(offset)
            stream.write(data.read()[:5])
        if self.fail_after_partial_write:
            raise RuntimeError("backend failed after a partial image write")

    def _stat(self, path):
        return FileStat(path, 0, 0.0, False)


class MountedFakeRW(FakeRW):
    def __init__(self, image_path, *, fail_after_partial_write=False):
        super().__init__(image_path, fail_after_partial_write=fail_after_partial_write)
        self.lifecycle = []

    def _open_backend(self):
        self.lifecycle.append("open")

    def _close_backend(self):
        self.lifecycle.append("close")


def test_universal_rw_restores_full_image_after_partial_backend_failure(tmp_path):
    image = tmp_path / "disk.img"
    original = b"original image bytes"
    image.write_bytes(original)
    rw = FakeRW(image, fail_after_partial_write=True)
    try:
        with pytest.raises(RuntimeError, match="partial image write"):
            rw.write_file("/file.bin", io.BytesIO(b"replacement payload"))

        assert rw.mutation_called is True
        assert image.read_bytes() == original
        assert rw.journal.uncommitted() == []
        assert list(rw.journal.backup_dir.iterdir()) == []
    finally:
        rw.close()


def test_mounted_backend_detaches_before_restore_and_reopens(tmp_path):
    image = tmp_path / "disk.img"
    original = b"original image bytes"
    image.write_bytes(original)
    rw = MountedFakeRW(image, fail_after_partial_write=True)
    rw.open()
    try:
        with pytest.raises(RuntimeError, match="partial image write"):
            rw.write_file("/file.bin", io.BytesIO(b"replacement payload"))

        assert image.read_bytes() == original
        assert rw.lifecycle == ["open", "close", "open"]
    finally:
        rw.close()
    assert rw.lifecycle == ["open", "close", "open", "close"]


def test_universal_rw_success_removes_transaction_snapshot(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"original image bytes")
    rw = FakeRW(image)
    try:
        rw.write_file("/file.bin", io.BytesIO(b"replacement payload"))

        assert rw.mutation_called is True
        assert list(rw.journal.backup_dir.iterdir()) == []
        assert rw.journal.uncommitted() == []
    finally:
        rw.close()


def test_universal_rw_snapshot_failure_cancels_before_mutation(tmp_path, monkeypatch):
    image = tmp_path / "disk.img"
    image.write_bytes(b"original image bytes")
    rw = FakeRW(image)
    snapshot_error = OSError("snapshot unavailable")

    def fail_snapshot(offset, length):
        assert (offset, length) == (0, image.stat().st_size)
        raise snapshot_error

    monkeypatch.setattr(rw.journal, "backup_region", fail_snapshot)
    try:
        with pytest.raises(OSError, match="snapshot unavailable") as raised:
            rw.write_file("/file.bin", io.BytesIO(b"replacement payload"))

        assert raised.value is snapshot_error
        assert rw.mutation_called is False
        assert image.read_bytes() == b"original image bytes"
        assert rw.journal.uncommitted() == []
    finally:
        rw.close()


def test_atomic_backend_rollback_failed_marker_keeps_pending_record(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"original image bytes")
    rw = FakeRW(image, atomic=True)

    class RollbackFailedError(Exception):
        rollback_failed = True

    def fail_with_rollback_marker(path, data, offset):
        raise RollbackFailedError("native rollback failed")

    rw._write_file = fail_with_rollback_marker
    try:
        with pytest.raises(RWError, match="remains pending"):
            rw.write_file("/file.bin", io.BytesIO(b"payload"))

        assert len(rw.journal.uncommitted()) == 1
        assert list(rw.journal.backup_dir.iterdir()) == []
    finally:
        rw.close()


def test_journal_hash_chain_detects_record_tampering(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"forensic image")
    journal = Journal(image)
    try:
        tx_id = journal.begin("write_file", "/evidence.bin")
        journal.backup_region(0, image.stat().st_size)
        journal.commit()
        assert journal.verify_chain()["valid"] is True

        journal._conn.execute(
            "UPDATE journal SET path=? WHERE tx_id=?", ("/tampered.bin", tx_id)
        )
        journal._conn.commit()

        report = journal.verify_chain()
        assert report["valid"] is False
        assert "integrity hash" in report["error"]
    finally:
        journal.close()


def test_legacy_journal_without_commit_state_is_not_guessed_as_pending(tmp_path):
    image = tmp_path / "legacy.img"
    image.write_bytes(b"legacy image")
    db_path = image.with_suffix(image.suffix + ".rw-journal.sqlite")
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE journal (
               tx_id TEXT PRIMARY KEY,
               op TEXT NOT NULL,
               path TEXT NOT NULL,
               backup_path TEXT,
               sha256 TEXT,
               prev_hash TEXT NOT NULL,
               created_at REAL NOT NULL
           )"""
    )
    connection.execute(
        "INSERT INTO journal(tx_id,op,path,prev_hash,created_at) VALUES(?,?,?,?,?)",
        ("old-tx", "write_file", "/evidence.bin", "0" * 64, 1.0),
    )
    connection.commit()
    connection.close()

    with pytest.warns(RuntimeWarning, match="unknown commit state"):
        journal = Journal(image)
    try:
        assert journal.uncommitted() == []
        assert [row["tx_id"] for row in journal.legacy_unverifiable()] == ["old-tx"]
        assert journal.verify_chain()["valid"] is True
    finally:
        journal.close()


def test_legacy_pending_snapshot_remains_recoverable_after_migration(tmp_path):
    image = tmp_path / "legacy-pending.img"
    original = b"0123456789abcdef"
    image.write_bytes(original)
    db_path = image.with_suffix(image.suffix + ".rw-journal.sqlite")
    backup_dir = image.with_suffix(image.suffix + ".rw-backups")
    backup_dir.mkdir()
    backup = backup_dir / "pending-tx.bin"
    snapshot = original[4:10]
    backup.write_bytes(snapshot)

    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE journal (
               tx_id TEXT PRIMARY KEY,
               op TEXT NOT NULL,
               path TEXT NOT NULL,
               backup_path TEXT,
               sha256 TEXT,
               prev_hash TEXT NOT NULL,
               created_at REAL NOT NULL,
               offset INTEGER,
               length INTEGER,
               committed INTEGER DEFAULT 0
           )"""
    )
    connection.execute(
        "INSERT INTO journal VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "pending-tx",
            "write_file",
            "/evidence.bin",
            str(backup),
            hashlib.sha256(snapshot).hexdigest(),
            "0" * 64,
            1.0,
            4,
            len(snapshot),
            0,
        ),
    )
    connection.commit()
    connection.close()
    image.write_bytes(original[:4] + b"BROKEN" + original[10:])

    with pytest.warns(RuntimeWarning, match="legacy journal record"):
        journal = Journal(image)
    try:
        assert journal.verify_chain()["valid"] is True
        assert [row["tx_id"] for row in journal.uncommitted()] == ["pending-tx"]
        backup.write_bytes(b"broken")
        assert journal.rollback("pending-tx") is False
        assert journal.uncommitted()[0]["status"] == "legacy_recovery_required"
        assert journal.verify_chain()["valid"] is True

        backup.write_bytes(snapshot)
        assert journal.rollback("pending-tx") is True
        assert image.read_bytes() == original
        assert journal.uncommitted() == []
    finally:
        journal.close()
