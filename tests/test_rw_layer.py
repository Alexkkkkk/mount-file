# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""End-to-end tests for the RW layer (issue #87, Task 2.1 — PR #121).

The FAT32 fixture is the synthetic-but-structurally-complete volume built by
``tests/test_fat32.py``, so these tests need no mkfs/dosfstools.  They cover the
native adapter against the real ``mountfile/fat32.py`` contract, the read-only
gate, ``--dry-run``, the verification engine, the JSONL audit export, the CLI
and the MCP stdio handler.
"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fat32 import make_fat32_image  # noqa: E402

from mountfile.fat32 import Fat32  # noqa: E402
from mountfile_universal_rw import FsType, Mode, RWError  # noqa: E402
from mountfile_rw_complete import (  # noqa: E402
    DryRunFat32,
    Fat32Adapter,
    VerificationEngine,
    create_rw_full,
    mcp_handle,
)

CLI = REPO_ROOT / "mountfile_rw_complete.py"


def _image(tmp_path, name="rw.img"):
    image = tmp_path / name
    make_fat32_image(image)
    return image


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _native_read(image, path):
    with open(image, "rb") as handle:
        return Fat32(handle).read_file(path)


def _write(tmp_path, path, data):
    """Round-trip helper: build an image, write one file through the adapter."""
    image = _image(tmp_path)
    engine = create_rw_full(image, mode=Mode.RW)
    engine.open()
    try:
        engine.create_file(path, io.BytesIO(data), overwrite=True)
    finally:
        engine.close()
    return image


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *[str(a) for a in args]],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


# ------------------------------------------------------------- native adapter
def test_adapter_round_trip_through_the_native_engine(tmp_path):
    image = _image(tmp_path)
    engine = create_rw_full(image, mode=Mode.RW)
    assert isinstance(engine, Fat32Adapter)
    engine.open()
    try:
        engine.create_dir("/EVIDENCE")
        engine.create_file("/EVIDENCE/README.TXT", io.BytesIO(b"collected 2026-10-18"))
        engine.rename("/EVIDENCE/README.TXT", "/EVIDENCE/RENAMED.TXT")
    finally:
        engine.close()

    assert _native_read(image, "/EVIDENCE/RENAMED.TXT") == b"collected 2026-10-18"
    with pytest.raises(Exception):
        _native_read(image, "/EVIDENCE/README.TXT")


def test_stat_reports_native_sizes_and_missing_paths(tmp_path):
    image = _write(tmp_path, "/NOTE.TXT", b"12345")
    engine = create_rw_full(image, mode=Mode.RO)
    engine.open()
    try:
        info = engine.stat("/NOTE.TXT")
        assert info.size == 5
        assert info.is_dir is False
        assert info.mtime == 0.0  # нативный движок timestamps не хранит
        with pytest.raises(FileNotFoundError):
            engine.stat("/MISSING.TXT")
    finally:
        engine.close()


def test_overwrite_is_opt_in(tmp_path):
    image = _write(tmp_path, "/NOTE.TXT", b"first")
    engine = create_rw_full(image, mode=Mode.RW)
    engine.open()
    try:
        with pytest.raises(FileExistsError):
            engine.create_file("/NOTE.TXT", io.BytesIO(b"second"))
        engine.create_file("/NOTE.TXT", io.BytesIO(b"second"), overwrite=True)
    finally:
        engine.close()
    assert _native_read(image, "/NOTE.TXT") == b"second"


def test_write_file_at_offset_keeps_the_rest_of_the_file(tmp_path):
    image = _write(tmp_path, "/NOTE.TXT", b"AAAA-BBBB")
    engine = create_rw_full(image, mode=Mode.RW)
    engine.open()
    try:
        engine.write_file("/NOTE.TXT", io.BytesIO(b"XXXX"), offset=5)
    finally:
        engine.close()
    assert _native_read(image, "/NOTE.TXT") == b"AAAA-XXXX"


def test_cross_directory_rename_is_rejected_explicitly(tmp_path):
    image = _image(tmp_path)
    engine = create_rw_full(image, mode=Mode.RW)
    engine.open()
    try:
        engine.create_dir("/A")
        engine.create_dir("/B")
        engine.create_file("/A/F.TXT", io.BytesIO(b"x"))
        with pytest.raises(RWError):
            engine.rename("/A/F.TXT", "/B/F.TXT")
    finally:
        engine.close()


# ----------------------------------------------------------------- read-only
def test_read_only_mode_is_gated_and_leaves_the_image_untouched(tmp_path):
    image = _write(tmp_path, "/NOTE.TXT", b"ro")
    before = _sha(image)
    engine = create_rw_full(image, mode=Mode.RO)
    engine.open()
    try:
        assert engine.stat("/NOTE.TXT").size == 2
        with pytest.raises(RWError):
            engine.create_file("/BLOCKED.TXT", io.BytesIO(b"nope"))
        with pytest.raises(RWError):
            engine.create_dir("/BLOCKED")
        with pytest.raises(RWError):
            engine.delete("/NOTE.TXT")
    finally:
        engine.close()
    assert _sha(image) == before
    assert b"BLOCKED.TXT" not in Path(image).read_bytes()


# ------------------------------------------------------------------- dry-run
def test_dry_run_validates_without_touching_the_image(tmp_path):
    image = _image(tmp_path)
    before = _sha(image)
    engine = create_rw_full(image, mode=Mode.RW, dry_run=True)
    assert isinstance(engine, DryRunFat32)
    engine.open()
    try:
        engine.create_dir("/DRY")
        engine.create_file("/DRY/NOTE.TXT", io.BytesIO(b"plan"))
        engine.rename("/DRY/NOTE.TXT", "/DRY/OTHER.TXT")
    finally:
        engine.close()
    assert _sha(image) == before
    assert engine.plan() == [
        "mkdir /DRY",
        "create_file /DRY/NOTE.TXT",
        "rename /DRY/NOTE.TXT -> /DRY/OTHER.TXT",
    ]
    assert b"DRY" not in Path(image).read_bytes()[:4096]


# -------------------------------------------------------------- verification
def test_verification_engine_accepts_a_freshly_written_image(tmp_path):
    image = _write(tmp_path, "/EVIDENCE.TXT", b"evidence")
    report = VerificationEngine().verify(image, FsType.FAT32)
    assert report.ok, report.details
    assert report.checks["fat32_signature"] is True
    assert report.checks["fat32_label"] is True


@pytest.mark.skipif(shutil.which("fsck.fat") is None, reason="dosfstools not installed")
def test_written_image_passes_the_external_fat_checker(tmp_path):
    image = _write(tmp_path, "/EVIDENCE.TXT", b"evidence")
    proc = subprocess.run(["fsck.fat", "-n", str(image)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ------------------------------------------------------------------------ CLI
def test_cli_put_mkdir_mv_verify_journal_and_audit(tmp_path):
    image = _image(tmp_path)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload-42")

    assert _cli("mkdir", image, "/CASES").returncode == 0, _cli("mkdir", image, "/CASES").stderr
    put = _cli("put", image, payload, "/CASES/PAYLOAD.BIN")
    assert put.returncode == 0, put.stderr
    assert _native_read(image, "/CASES/PAYLOAD.BIN") == b"payload-42"

    # put into an already existing directory must not fail on re-creation
    again = _cli("put", image, payload, "/CASES/PAYLOAD.BIN")
    assert again.returncode == 0, again.stderr

    mv = _cli("mv", image, "/CASES/PAYLOAD.BIN", "/CASES/RENAMED.BIN")
    assert mv.returncode == 0, mv.stderr
    assert _native_read(image, "/CASES/RENAMED.BIN") == b"payload-42"

    verify = _cli("verify", image)
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["ok"] is True

    journal = _cli("journal", image)
    assert journal.returncode == 0, journal.stderr
    assert "create_file" in journal.stdout or "mkdir" in journal.stdout

    out = tmp_path / "audit.jsonl"
    audit = _cli("audit", image, out)
    assert audit.returncode == 0, audit.stderr
    assert out.read_text(encoding="utf-8").strip()


def test_cli_dry_run_does_not_change_the_image(tmp_path):
    image = _image(tmp_path)
    before = _sha(image)
    res = _cli("mkdir", image, "/DRY", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert _sha(image) == before


def test_cli_get_extracts_the_written_file(tmp_path):
    image = _write(tmp_path, "/EVIDENCE.TXT", b"evidence")
    dst = tmp_path / "out.bin"
    res = _cli("get", image, "/EVIDENCE.TXT", dst)
    assert res.returncode == 0, res.stderr
    assert dst.read_bytes() == b"evidence"


# ------------------------------------------------------------------------ MCP
def test_mcp_read_tools_are_read_only(tmp_path):
    image = _write(tmp_path, "/EVIDENCE.TXT", b"evidence")
    listing = mcp_handle("list_files", {"image": str(image), "path": "/"})
    names = {entry["name"] for entry in listing["files"]}
    assert "EVIDENCE.TXT" in names

    read = mcp_handle("read_file", {"image": str(image), "path": "/EVIDENCE.TXT"})
    assert read["size"] == 8
    assert read["sha256"] == hashlib.sha256(b"evidence").hexdigest()

    verify = mcp_handle("verify_image", {"image": str(image)})
    assert verify["ok"] is True

    assert "error" in mcp_handle("no_such_tool", {})


def test_mcp_stdio_answers_one_request_per_line(tmp_path):
    image = _write(tmp_path, "/EVIDENCE.TXT", b"evidence")
    request = json.dumps({"tool": "verify_image", "params": {"image": str(image)}})
    proc = subprocess.run(
        [sys.executable, str(CLI), "mcp"],
        input=request + "\n", capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True
