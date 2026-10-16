# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Autotests for the build and the entry point (what CI checks before packaging).

Covers the things a broken build breaks first:

* the entry point answers ``--version`` / ``--diag`` with exit code 0;
* the pytsk3 layer actually reads a filesystem image produced by the
  system tool ``mkfs.vfat`` (fixture is created on the fly, no binaries
  are committed to the repo);
* the tc_style formatting helpers used by the interface;
* the main window still builds headless and fits the monitor work area
  (QT_QPA_PLATFORM=offscreen).

Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_build_ci.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# NOTE: MOUNTFILE_NO_UPDATE is deliberately NOT set here. A module-level
# setdefault leaks into the whole pytest session, so the updater ends up
# disabled for every other test file as well and
# tests/test_updater.py::test_updater_thread_never_raises_offline receives
# an empty status list (assert ([])) and fails. It is set per-test instead,
# in the GUI test that actually starts the update thread.

import pytest

from mountfile import __version__


def _has_qt():
    """True when a Qt binding (PyQt6/PyQt5) is importable."""
    try:
        __import__("mountfile.qt_compat")
        return True
    except ImportError:
        return False


requires_qt = pytest.mark.skipif(
    not _has_qt(), reason="no Qt binding (PyQt6/PyQt5) installed"
)

# --------------------------------------------------------------- entry point


def _run_entry(*args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_version_flag_exits_zero_and_prints_version():
    r = _run_entry("--version")
    assert r.returncode == 0, r.stderr
    assert __version__ in r.stdout
    assert "Mount File" in r.stdout


@requires_qt
def test_diag_flag_exits_zero_and_reports_qt():
    r = _run_entry("--diag")
    assert r.returncode == 0, r.stderr
    assert "qt_compat: OK" in r.stdout
    assert "mountfile package: OK" in r.stdout


# ------------------------------------------------------------------ tc_style


@requires_qt
def test_tc_style_size_formatting():
    from mountfile import tc_style

    assert tc_style.fmt_size(1234567) == "1 234 567"
    assert tc_style.fmt_size(0) == "0"
    assert tc_style.fmt_kb(2048) == "2 Кб"


# ------------------------------------------------- pytsk3 against mkfs.vfat

needs_mkfs_vfat = pytest.mark.skipif(
    shutil.which("mkfs.vfat") is None or shutil.which("mcopy") is None,
    reason="mkfs.vfat / mcopy (dosfstools, mtools) not installed",
)
needs_pytsk3 = pytest.mark.skipif(
    __import__("mountfile.vfs", fromlist=["pytsk3"]).pytsk3 is None,
    reason="pytsk3 is not installed",
)


def _make_vfat_image(tmp_path):
    """FAT32 volume image with one file, built by the system mkfs.vfat."""
    img = str(tmp_path / "ci-vfat.img")
    subprocess.run(
        ["mkfs.vfat", "-C", img, "65536", "-F", "32", "-S", "512"],
        check=True,
        capture_output=True,
    )
    src = tmp_path / "HELLO.TXT"
    src.write_bytes(b"Mount File CI fixture\n")
    subprocess.run(
        ["mcopy", "-i", img, str(src), "::/HELLO.TXT"],
        check=True,
        capture_output=True,
    )
    return img


@needs_mkfs_vfat
@needs_pytsk3
def test_tsk_reads_mkfs_vfat_image(tmp_path):
    from mountfile.vfs import TskImage

    img = _make_vfat_image(tmp_path)
    tsk = TskImage(img, offset=0)
    names = [e["name"] for e in tsk.list_dir("/")]
    assert "HELLO.TXT" in names
    assert tsk.file_size("/HELLO.TXT") == len(b"Mount File CI fixture\n")
    assert tsk.read_file("/HELLO.TXT") == b"Mount File CI fixture\n"


@needs_mkfs_vfat
@needs_pytsk3
def test_tsk_read_range(tmp_path):
    from mountfile.vfs import TskImage

    tsk = TskImage(_make_vfat_image(tmp_path), offset=0)
    assert tsk.read_file_range("/HELLO.TXT", 11, 2) == b"CI"


# ------------------------------------------------------- GUI builds headless


@requires_qt
def test_main_window_fills_screen_workarea(monkeypatch):
    # Scoped to this test only: MainWindow.__init__ starts the update thread,
    # so the network check is switched off here and nowhere else.
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")

    from mountfile.qt_compat import QApplication
    from mountfile.tc_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    app.processEvents()
    geo = win.screen_geometry()
    assert geo is not None
    assert (win.width(), win.height()) == (
        geo.width(),
        geo.height(),
    ), "main window must fill the monitor work area"
    assert (win.minimumWidth(), win.minimumHeight()) == (640, 400)

    # F11: into fullscreen and back to the maximized window
    win.toggle_fullscreen()
    app.processEvents()
    assert win.isFullScreen()
    win.toggle_fullscreen()
    app.processEvents()
    assert not win.isFullScreen()

    win._close_writer()
    win.close()


@requires_qt
def test_viewer_dialog_scales_with_screen():
    from mountfile.qt_compat import QApplication
    from mountfile.tc_window import ViewerDialog

    app = QApplication.instance() or QApplication([])
    dlg = ViewerDialog(None, "ci.bin", 2048, b"\x00" * 16)
    app.processEvents()
    assert dlg.width() >= 400 and dlg.height() >= 300
    dlg.close()
