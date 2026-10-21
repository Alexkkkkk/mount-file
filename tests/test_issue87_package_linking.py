# SPDX-License-Identifier: MIT
"""Regression tests for the issue #87 package integration bridge."""

import hashlib

from mountfile.dfir import UnallocatedExtractor
from mountfile.dfir_unallocated_scan import UnallocatedExtractor as ScannerExtractor
from mountfile.universal_fs_rw import JournalV2


def test_journal_v2_is_available_from_the_package(tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"0123456789abcdef")
    original = image.read_bytes()

    journal = JournalV2(image)
    try:
        tx_id = journal.begin("write_file", "/evidence.bin")
        journal.backup_region(4, 4)
        with image.open("r+b") as stream:
            stream.seek(4)
            stream.write(b"XXXX")
        assert journal.uncommitted()[0]["tx_id"] == tx_id
        assert journal.rollback() is True
        assert image.read_bytes() == original
    finally:
        journal.close()


def test_journal_v2_migrates_legacy_schema(tmp_path):
    image = tmp_path / "legacy.img"
    image.write_bytes(b"legacy")
    journal = JournalV2(image)
    try:
        columns = {
            row[1] for row in journal._conn.execute("PRAGMA table_info(journal)")
        }
        assert {"offset", "length", "committed"} <= columns
    finally:
        journal.close()


def test_unallocated_extractor_is_exported_without_eager_legacy_import():
    assert UnallocatedExtractor is ScannerExtractor


def test_package_extractor_preserves_image_read_only(tmp_path):
    image = tmp_path / "raw.img"
    image.write_bytes(b"raw evidence")
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    extractor = UnallocatedExtractor(image)
    # The facade is lazy; constructing it must not inspect or mutate evidence.
    assert extractor.image_path == image
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
