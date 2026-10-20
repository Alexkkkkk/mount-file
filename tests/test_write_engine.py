# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for MF-WE — the transactional write engine (ТЗ MF-WE-TZ-001).

The FAT32 fixtures are built from scratch by ``tests/test_fat32.py``, so these
tests need no mkfs/dosfstools on the runner.  The ext-backend test skips itself
when ``mkfs.ext4`` is unavailable.
"""

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fat32 import BPS, RESERVED, SPF, make_fat32_image

from mountfile.fat32 import Fat32
from mountfile.write_engine import (
    DamagedImageError,
    ReadOnlyError,
    TransactionError,
    UnsupportedOperationError,
    WriteEngine,
    WriteEngineError,
    damage_report,
)

def _image(tmp_path, name="fat32.img"):
    image = tmp_path / name
    make_fat32_image(image)
    return image

def _engine(tmp_path, *, read_only=False, **kwargs):
    image = _image(tmp_path)
    engine = WriteEngine(
        image,
        read_only=read_only,
        snapshot_dir=tmp_path / "snapshots",
        journal_path=tmp_path / "journal.jsonl",
        **kwargs,
    )
    return image, engine

def _sha(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

# --------------------------------------------------------------- read-only
def test_engine_is_read_only_by_default(tmp_path):
    image, engine = _engine(tmp_path, read_only=True)
    before = _sha(image)
    try:
        with pytest.raises(ReadOnlyError) as excinfo:
            engine.write_file("/BLOCKED.TXT", b"nope")
        assert "read-only" in str(excinfo.value)
        assert engine.exists("/BLOCKED.TXT") is False
    finally:
        engine.close()
    assert _sha(image) == before

@pytest.mark.parametrize("method", ["mkdir", "delete", "rename", "copy", "chmod"])
def test_every_mutation_is_gated_by_read_only_mode(tmp_path, method):
    image, engine = _engine(tmp_path, read_only=True)
    before = _sha(image)
    try:
        calls = {
            "mkdir": lambda: engine.mkdir("/NEW"),
            "delete": lambda: engine.delete("/ANY"),
            "rename": lambda: engine.rename("/A", "/B"),
            "copy": lambda: engine.copy("/A", "/B"),
            "chmod": lambda: engine.chmod("/A", 0o644),
        }
        with pytest.raises(ReadOnlyError):
            calls[method]()
    finally:
        engine.close()
    assert _sha(image) == before, "read-only mode must not change the image"

def test_read_only_engine_still_reads(tmp_path):
    image, engine = _engine(tmp_path, read_only=True)
    try:
        entries = engine.list("/")
        assert isinstance(entries, list)
        assert engine.info().fs_type.startswith("fat")
    finally:
        engine.close()

# ------------------------------------------------------- write + verify
def test_transaction_commit_writes_and_verifies(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        with engine.transaction("add evidence"):
            engine.mkdir("/EVIDENCE")
            entry = engine.write_file("/EVIDENCE/README.TXT", b"collected 2026-10-18")
            engine.rename("/EVIDENCE/README.TXT", "/EVIDENCE/RENAMED.TXT")
            assert entry.status == "applied"
        assert engine.read("/EVIDENCE/RENAMED.TXT") == b"collected 2026-10-18"
        assert engine.exists("/EVIDENCE/README.TXT") is False
        report = engine.verify()
        assert report["ok"] is True, report["problems"]
        assert report["journal"]["ok"] is True
        assert report["checks"], "verification must report observed checks"
    finally:
        engine.close()

def test_verify_reports_observed_values_not_bare_booleans(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        report = engine.verify()
        by_name = {check["name"]: check for check in report["checks"]}
        assert "FAT boot signature 0x55AA" in by_name
        assert by_name["FAT boot signature 0x55AA"]["detail"] == "b'U\\xaa'"
        assert by_name["bytes per sector"]["detail"] == "bytes_per_sector=512"
        # no external checker in this sandbox => reported as unknown, not as pass
        checker = by_name["filesystem checker"]
        assert checker["ok"] in (True, False, None)
        assert "detail" in checker
    finally:
        engine.close()

def test_delete_removes_the_file(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/GONE.BIN", b"x" * 32)
        assert engine.exists("/GONE.BIN") is True
        engine.delete("/GONE.BIN")
        assert engine.exists("/GONE.BIN") is False
    finally:
        engine.close()

def test_copy_duplicates_content(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/SRC.BIN", b"payload")
        engine.copy("/SRC.BIN", "/DST.BIN")
        assert engine.read("/DST.BIN") == b"payload"
    finally:
        engine.close()

# --------------------------------------------------------------- rollback
def test_rollback_restores_the_image_byte_for_byte(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/KEEP.TXT", b"original")
        before = _sha(image)
        with pytest.raises(RuntimeError):
            with engine.transaction("failing batch"):
                engine.write_file("/TEMP.BIN", b"volatile")
                raise RuntimeError("simulated failure mid-transaction")
        assert _sha(image) == before
        assert engine.read("/KEEP.TXT") == b"original"
        assert engine.exists("/TEMP.BIN") is False
    finally:
        engine.close()

def test_rollback_from_a_crash_between_transactions(tmp_path):
    """A crashed process leaves a snapshot and a journal entry behind."""
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/ONE.TXT", b"one")
    finally:
        engine.close()

    # Simulate a crash: mutate the image outside the engine, then roll back.
    crashing = WriteEngine(
        image,
        read_only=False,
        snapshot_dir=tmp_path / "snapshots",
        journal_path=tmp_path / "journal.jsonl",
    )
    try:
        crashing.write_file("/TWO.TXT", b"two")
        snapshot = crashing.session_snapshot
        assert snapshot is not None and snapshot.exists()
        with open(image, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\x00\x00")  # damage the boot sector like a bad kill
        assert crashing.restore_from_backup() is True
    finally:
        crashing.close()

    recovered = WriteEngine(
        image,
        read_only=False,
        snapshot_dir=tmp_path / "snapshots",
        journal_path=tmp_path / "journal.jsonl",
    )
    try:
        assert recovered.info().fs_type.startswith("fat")
        assert recovered.verify()["checks"][0]["ok"] is True
    finally:
        recovered.close()

# ----------------------------------------------------------------- dry run
def test_dry_run_never_touches_the_image(tmp_path):
    image, engine = _engine(tmp_path, dry_run=True)
    try:
        before = _sha(image)
        with engine.transaction("plan only"):
            entry = engine.write_file("/PLANNED.TXT", b"never written")
        assert entry.status == "planned"
        assert _sha(image) == before
        assert engine.exists("/PLANNED.TXT") is False
        statuses = [row["status"] for row in engine.history()]
        assert "planned" in statuses
    finally:
        engine.close()

def test_dry_run_reports_missing_parent_as_a_planning_error(tmp_path):
    image, engine = _engine(tmp_path, dry_run=True)
    try:
        before = _sha(image)
        with pytest.raises(WriteEngineError):
            engine.write_file("/NO/SUCH/DIR/FILE.TXT", b"x")
        assert _sha(image) == before
    finally:
        engine.close()

# ------------------------------------------------------------ damaged image
def _corrupt_fat(image):
    """Damage the first FAT's media descriptor without breaking detection.

    The fixture is formatted with a single FAT (``NFATS == 1``), so the
    copy-comparison check cannot fire; the media byte can, and it still leaves
    the boot sector classifiable as FAT32.
    """
    offset = RESERVED * BPS
    with open(image, "r+b") as handle:
        handle.seek(offset)
        assert handle.read(1) == b"\xf8"  # the formatter's media descriptor
        handle.seek(offset)
        handle.write(b"\x00")

def test_damaged_image_is_refused_for_rw(tmp_path):
    image = _image(tmp_path)
    _corrupt_fat(image)
    report = damage_report(image)
    assert report["fs_type"] == "fat", report
    assert any("FAT media descriptor" in problem for problem in report["problems"]), report
    with pytest.raises(DamagedImageError) as excinfo:
        WriteEngine(image, read_only=False, snapshot_dir=tmp_path / "s", journal_path=tmp_path / "j.jsonl")
    assert "allow_damaged" in str(excinfo.value)

def test_allow_damaged_override_is_explicit(tmp_path):
    image = _image(tmp_path)
    _corrupt_fat(image)
    engine = WriteEngine(
        image,
        read_only=False,
        allow_damaged=True,
        snapshot_dir=tmp_path / "s",
        journal_path=tmp_path / "j.jsonl",
    )
    try:
        assert engine.damage["problems"], "the probe must report the corrupted copy"
        assert engine.write_file("/STILL.WORKS", b"ok").status == "applied"
    finally:
        engine.close()

# -------------------------------------------------------------- journal
def test_journal_is_a_verified_hash_chain(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/A.TXT", b"a")
        engine.write_file("/B.TXT", b"b")
        assert engine.verify_journal()["ok"] is True
        rows = engine.history()
        assert len(rows) >= 2
        assert all(row["hash"] for row in rows)
        prev = "0" * 64
        for row in rows:
            assert row["prev"] == prev
            prev = row["hash"]
    finally:
        engine.close()

def test_journal_verification_detects_tampering(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/A.TXT", b"a")
        journal = tmp_path / "journal.jsonl"
        lines = journal.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["path"] = "/tampered"
        lines[0] = json.dumps(tampered, sort_keys=True, default=str)
        journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = engine.verify_journal()
        assert report["ok"] is False
        assert report["broken"]
    finally:
        engine.close()

# ------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", ["relative.txt", "", "/../escape.txt", "..\\escape.txt"])
def test_unsafe_paths_are_rejected(tmp_path, bad):
    image, engine = _engine(tmp_path)
    try:
        with pytest.raises(WriteEngineError):
            engine.write_file(bad, b"x")
    finally:
        engine.close()

def test_operating_on_the_root_is_rejected(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        with pytest.raises(WriteEngineError):
            engine.delete("/")
    finally:
        engine.close()

def test_rename_onto_an_existing_path_is_rejected_in_dry_run(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/ONE.TXT", b"one")
        engine.write_file("/TWO.TXT", b"two")
    finally:
        engine.close()
    planner = WriteEngine(
        image,
        read_only=False,
        dry_run=True,
        snapshot_dir=tmp_path / "snapshots",
        journal_path=tmp_path / "journal.jsonl",
    )
    try:
        before = _sha(image)
        with pytest.raises(WriteEngineError) as excinfo:
            planner.rename("/ONE.TXT", "/TWO.TXT")
        assert "already exists" in str(excinfo.value)
        assert _sha(image) == before
    finally:
        planner.close()

def test_transactions_do_not_nest(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.begin("outer")
        with pytest.raises(TransactionError):
            engine.begin("inner")
        engine.rollback()
    finally:
        engine.close()

def test_unknown_transaction_id_is_reported(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        with pytest.raises(TransactionError):
            engine.commit("no-such-transaction")
    finally:
        engine.close()

def test_close_rolls_back_an_open_transaction(tmp_path):
    image, engine = _engine(tmp_path)
    engine.write_file("/KEEP.TXT", b"keep")
    before = _sha(image)
    engine.begin("left open")
    engine.write_file("/OPEN.TXT", b"x")
    engine.close()
    assert _sha(image) == before, "closing with an open transaction must not persist it"
    assert engine.verify_journal()["ok"] is True

# ------------------------------------------------------- unsupported ops
def test_chmod_on_fat_reports_unsupported_operation(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/F.TXT", b"x")
        with pytest.raises(UnsupportedOperationError) as excinfo:
            engine.chmod("/F.TXT", 0o644)
        assert "debugfs" in str(excinfo.value) or "ext backend" in str(excinfo.value)
    finally:
        engine.close()

def test_to_dict_describes_the_engine_state(tmp_path):
    image, engine = _engine(tmp_path)
    try:
        engine.write_file("/F.TXT", b"x")
        state = engine.to_dict()
        assert state["read_only"] is False
        assert state["offset"] == 0
        assert state["transactions"]
    finally:
        engine.close()

# ----------------------------------------------------------- ext backend
HAVE_MKFS_EXT4 = shutil.which("mkfs.ext4") is not None and shutil.which("debugfs") is not None

@pytest.mark.skipif(not HAVE_MKFS_EXT4, reason="mkfs.ext4/debugfs not available")
def test_ext_backend_verifies_and_rolls_back(tmp_path):
    image = tmp_path / "ext4.img"
    with open(image, "wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True, capture_output=True)
    engine = WriteEngine(
        image,
        read_only=False,
        snapshot_dir=tmp_path / "s",
        journal_path=tmp_path / "j.jsonl",
    )
    try:
        with engine.transaction("ext write"):
            engine.write_file("/HELLO.TXT", b"hello ext")
        assert engine.read("/HELLO.TXT") == b"hello ext"
        report = engine.verify()
        assert report["fs_type"] == "ext"
        assert report["checks"], report
        before = _sha(image)
        with pytest.raises(RuntimeError):
            with engine.transaction("ext rollback"):
                engine.write_file("/VOLATILE.TXT", b"gone")
                raise RuntimeError("boom")
        assert _sha(image) == before
        assert engine.exists("/VOLATILE.TXT") is False
    finally:
        engine.close()

# ------------------------------------------------- writer/fat32 consistency
def _uint(data, offset, size=4):
    return int.from_bytes(data[offset : offset + size], "little")

def _fsinfo_counters(image):
    """(free clusters, next free cluster) as cached in the FSINFO sector."""
    data = Path(image).read_bytes()
    bps = _uint(data, 11, 2)
    sector = _uint(data, 48, 2)
    raw = data[sector * bps : (sector + 1) * bps]
    return _uint(raw, 488), _uint(raw, 492)

def test_written_image_leaves_fat_metadata_consistent(tmp_path):
    """The writer must not leave a volume that fsck rejects as dirty.

    Regression for two defects found while closing epic #87:

    * the FSINFO sector was never refreshed, so the externally visible free
      cluster count stayed at its pre-write value (``fsck.fat``: "Free cluster
      summary wrong") — i.e. ``WriteEngine.verify()`` could report a clean image
      that a real checker refused;
    * ``mkdir`` wrote the root cluster number (2) into the ``..`` slot of a
      directory created in the root, which the FAT specification defines as 0
      (``fsck.fat``: "Invalid '..' entry in the second slot").
    """
    image, engine = _engine(tmp_path)
    try:
        with engine.transaction("add evidence"):
            engine.mkdir("/EVIDENCE")
            engine.write_file("/EVIDENCE/README.TXT", b"collected 2026-10-18")
            engine.rename("/EVIDENCE/README.TXT", "/EVIDENCE/RENAMED.TXT")
    finally:
        engine.close()

    # 1. the cached counters must equal a recomputation from the FAT
    cached_free, cached_first = _fsinfo_counters(image)
    with open(image, "r+b") as handle:
        fs = Fat32(handle)
        real_free, real_first = fs.free_clusters()
        dir_cluster = fs.find_dir("/EVIDENCE")
        fs.f.seek(fs.cluster_offset(dir_cluster) + 32)
        dotdot = fs.f.read(32)
    assert (cached_free, cached_first) == (real_free, real_first)
    assert dotdot[0:11] == b".." + b" " * 9
    assert _uint(dotdot, 26, 2) == 0, "a directory in the root stores 0 in '..'"

    # 2. and an independent checker must agree, when one is installed
    if shutil.which("fsck.fat"):
        proc = subprocess.run(
            [shutil.which("fsck.fat"), "-n", str(image)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
