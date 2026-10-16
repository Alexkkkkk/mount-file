# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""End-to-end GUI test: open a real FAT32 disk image in the main window.

Goes through the same code paths as a user: open_image() detects the volume,
mounts it, builds the tree; then a file is written, the tree refreshed and a
non-empty folder deleted recursively. Needs Qt but NOT pytsk3 (the built-in
FAT32 engine is used) and runs offscreen.

Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gui_end_to_end.py -v
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

qt_compat = pytest.importorskip(
    "mountfile.qt_compat", exc_type=ImportError, reason="no Qt bindings installed"
)

from test_fat32 import BPS, TOTAL_SECTORS, make_fat32_image

from mountfile.fat32 import Fat32
from mountfile.firmware import detect_volumes
from mountfile.mainwindow import MainWindow
from mountfile.qt_compat import QApplication, QModelIndex


class _Dialogs:
    """Stand-in for QMessageBox: records calls instead of blocking."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    fake = _Dialogs()
    monkeypatch.setattr("mountfile.mainwindow.QMessageBox", fake)
    return fake


def build_disk(tmp_path):
    """MBR + FAT32 volume holding one folder and one file."""
    vol = str(tmp_path / "vol.img")
    make_fat32_image(vol)
    with open(vol, "r+b") as fh:
        fs = Fat32(fh)
        fs.mkdir("/", "PHOTOS")
        fs.write_file("/PHOTOS", "holiday.jpg", b"\xff\xd8\xff" + b"x" * 900)
        fs.write_file("/", "HELLO.TXT", b"hello world")

    disk = str(tmp_path / "disk.img")
    mbr = bytearray(512)
    mbr[446 + 4] = 0x0B
    struct.pack_into("<I", mbr, 446 + 8, 2048)
    struct.pack_into("<I", mbr, 446 + 12, TOTAL_SECTORS)
    mbr[510:512] = b"\x55\xaa"
    with open(disk, "wb") as out:
        out.write(mbr)
        out.write(b"\x00" * (2048 * BPS - 512))
        with open(vol, "rb") as f:
            out.write(f.read())
    return disk


def _rows(model):
    return [model.data(model.index(r, 0)) for r in range(model.rowCount())]


def test_open_mount_write_refresh_delete(tmp_path, app, dialogs, monkeypatch):
    # Exercise the advertised no-TSK fallback even when CI installed pytsk3.
    monkeypatch.setattr("mountfile.vfs.pytsk3", None)
    disk = build_disk(tmp_path)
    assert [v.fstype for v in detect_volumes(disk)] == ["FAT32"]

    win = MainWindow()
    try:
        win.open_image(disk)

        # The partition from the table is selected automatically and mounted.
        assert win.cmb_parts.count() == 2
        assert win.cmb_parts.currentData() == 2048 * BPS
        assert win.provider is not None
        assert win.backend == "built-in FAT32 engine"  # forced fallback
        assert win.writer is not None
        assert win.btn_import.isEnabled()
        assert win.btn_rename.isEnabled()
        assert win.btn_delete.isEnabled()
        assert not dialogs.calls  # no warnings

        win.model.fetchMore(QModelIndex())
        assert _rows(win.model) == ["PHOTOS/", "HELLO.TXT"]

        # Selecting a file fills the preview/info pane.
        win.tree.setCurrentIndex(win.model.index(1, 0))
        assert "HELLO.TXT" in win.info.toPlainText()

        # A write through the window's own writer appears after a refresh.
        win.writer.write_file("/", "NEW.BIN", b"1234567")
        win._refresh_dir("/")
        assert win.model.canFetchMore(QModelIndex())
        win.model.fetchMore(QModelIndex())
        assert "NEW.BIN" in _rows(win.model)

        # Deleting a non-empty folder, exactly as the Delete button does.
        win.writer.delete("/PHOTOS", recursive=True)
        win._refresh_dir("/")
        win.model.fetchMore(QModelIndex())
        assert _rows(win.model) == ["HELLO.TXT", "NEW.BIN"]
    finally:
        win.close()


def test_volume_without_mountable_filesystem(tmp_path, app, dialogs):
    """A raw (unrecognised) volume must disable writing, not crash."""
    disk = str(tmp_path / "raw.img")
    mbr = bytearray(512)
    mbr[446 + 4] = 0x83
    struct.pack_into("<I", mbr, 446 + 8, 2048)
    struct.pack_into("<I", mbr, 446 + 12, 4096)
    mbr[510:512] = b"\x55\xaa"
    with open(disk, "wb") as f:
        f.truncate(2048 * BPS + 4096 * BPS)
        f.seek(0)
        f.write(mbr)

    win = MainWindow()
    try:
        win.open_image(disk)
        assert win.provider is None
        for btn in (win.btn_import, win.btn_mkdir, win.btn_rename, win.btn_delete):
            assert not btn.isEnabled()
        assert dialogs.calls  # user was warned
        assert win.tree.model() is None
    finally:
        win.close()


def test_write_buttons_disabled_until_a_volume_is_mounted(tmp_path, app, dialogs):
    """A failed mount must not leave live write buttons behind."""
    disk = str(tmp_path / "raw.img")
    mbr = bytearray(512)
    mbr[446 + 4] = 0x83
    struct.pack_into("<I", mbr, 446 + 8, 2048)
    struct.pack_into("<I", mbr, 446 + 12, 4096)
    mbr[510:512] = b"\x55\xaa"
    with open(disk, "wb") as f:
        f.truncate(2048 * BPS + 4096 * BPS)
        f.seek(0)
        f.write(mbr)

    win = MainWindow()
    try:
        win.open_image(disk)
        assert not win.btn_mkdir.isEnabled()
        # Re-mounting a valid FAT32 volume re-enables them.
        disk2 = build_disk(tmp_path)
        win.open_image(disk2)
        assert win.btn_mkdir.isEnabled()
    finally:
        win.close()


def test_switching_volumes_closes_previous_provider(tmp_path, app, dialogs):
    disk = build_disk(tmp_path)
    win = MainWindow()
    try:
        win.open_image(disk)
        first = win.provider
        assert first is not None
        win.cmb_parts.setCurrentIndex(0)  # "Whole image" @ 0
        assert win.provider is not first  # old provider released
        win.cmb_parts.setCurrentIndex(1)  # back to the partition
        assert win.provider is not first
        assert win.writer is not None
    finally:
        win.close()
