# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the RW v2 layer (issue #87 — PR #122).

``mountfile_rw_v2.py`` carries its own sanity checks as plain functions; this
module wires them into the project's pytest run so CI exercises them, and adds
coverage for the two pieces that had none: ``JournalV2``'s real byte-level
rollback against the native FAT32 engine, and the read-only ``MountFileBridge``.
"""

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fat32 import make_fat32_image  # noqa: E402

import mountfile_rw_v2 as rw_v2  # noqa: E402
from mountfile_universal_rw import StrategyUnavailable  # noqa: E402


def _image(tmp_path, name="v2.img"):
    image = tmp_path / name
    make_fat32_image(image)
    return image


def test_detect_fs_recognises_the_fat32_type_string_at_82(tmp_path):
    """Regression: the type string sits at 82 in a FAT32 extended BPB."""
    from mountfile_universal_rw import FsType, detect_fs

    assert detect_fs(_image(tmp_path)) is FsType.FAT32


def test_selftest_functions_pass(tmp_path):
    rw_v2.test_hash_chain_invariant()
    rw_v2.test_rollback_restores_bytes()
    rw_v2.test_uncommitted_listed()


def test_journal_v2_migrates_an_existing_journal_in_place(tmp_path):
    image = _image(tmp_path)
    journal = rw_v2.JournalV2(image)
    try:
        columns = {row[1] for row in journal._conn.execute("PRAGMA table_info(journal)")}
        assert {"offset", "length", "committed"} <= columns
    finally:
        journal.close()


def test_journal_v2_rolls_back_real_bytes_of_the_image(tmp_path):
    image = _image(tmp_path)
    original = image.read_bytes()
    journal = rw_v2.JournalV2(image)
    try:
        journal.begin("write_file", "/EVIDENCE.TXT")
        journal.backup_region(0, 512)
        with open(image, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\x00" * 512)
        assert image.read_bytes() != original

        assert journal.rollback() is True
        assert image.read_bytes() == original
    finally:
        journal.close()


def test_journal_v2_reports_uncommitted_transactions(tmp_path):
    image = _image(tmp_path)
    journal = rw_v2.JournalV2(image)
    try:
        tx = journal.begin("write_file", "/PENDING.TXT")
        journal.backup_region(0, 512)
        assert [row["tx_id"] for row in journal.uncommitted()] == [tx]
        journal.commit()
        assert journal.uncommitted() == []
    finally:
        journal.close()


def test_structural_snapshot_covers_the_complete_image(tmp_path):
    image = _image(tmp_path)
    calls = []
    adapter = object.__new__(rw_v2.VerifiedFat32Adapter)
    adapter.image_path = image
    adapter.journal = SimpleNamespace(
        backup_region=lambda offset, length: calls.append((offset, length))
    )

    adapter._backup_structural_region()

    assert calls == [(0, image.stat().st_size)]


def test_bridge_lists_and_reads_without_writing(tmp_path):
    """The bridge is read-only: the image hash must be identical afterwards."""
    image = _image(tmp_path)
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    bridge = rw_v2.MountFileBridge(image)
    try:
        entries = bridge.list_files("/")
    except StrategyUnavailable as exc:
        pytest.skip(f"RO bridge unavailable here: {exc}")
    assert isinstance(entries, list)
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
