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

import hashlib
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


@needs_mkfs_vfat
@needs_pytsk3
def test_native_fat_matches_pytsk_on_real_mkfs_vfat_image(tmp_path):
    from mountfile.fat_reader import NativeFatImage
    from mountfile.vfs import TskImage

    img = _make_vfat_image(tmp_path)
    volume_size = os.path.getsize(img)
    native = NativeFatImage(img, offset=0, volume_size=volume_size)
    tsk = TskImage(img, offset=0, volume_size=volume_size)
    try:
        native_entries = {
            entry["name"].casefold(): entry for entry in native.list_dir("/")
        }
        # Sleuth Kit exposes FAT metadata as synthetic $FAT/$MBR/$OrphanFiles
        # entries; compare the user-visible directory namespace only.
        tsk_entries = {
            entry["name"].casefold(): entry
            for entry in tsk.list_dir("/")
            if not entry["name"].startswith("$")
        }
        assert native_entries.keys() == tsk_entries.keys()
        assert native_entries["hello.txt"]["is_dir"] is False
        assert tsk_entries["hello.txt"]["is_dir"] is False

        expected = b"Mount File CI fixture\n"
        assert native.file_size("/HELLO.TXT") == tsk.file_size("/HELLO.TXT")
        assert native.read_file("/HELLO.TXT") == expected
        assert tsk.read_file("/HELLO.TXT") == expected
        assert (
            native.read_file_range("/HELLO.TXT", 11, 2)
            == tsk.read_file_range("/HELLO.TXT", 11, 2)
            == b"CI"
        )
    finally:
        native.close()
        tsk.close()


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


# ------------------------------------------------------- unified launcher
#
# mountfile_all.py drives every feature in one run; if its CLI/API calls
# drift away from the real modules these tests fail instead of the user
# getting a wall of warnings.

LAUNCHER = os.path.join(ROOT, "mountfile_all.py")


def _run_launcher(*args):
    """Run mountfile_all.py offline and return the completed process."""
    env = dict(os.environ)
    env["MOUNTFILE_NO_UPDATE"] = "1"  # the update check must not hit the network
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, LAUNCHER, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
    )


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def test_launcher_is_importable_and_compiles():
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", LAUNCHER],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr


def test_qt_is_base_dependency_and_pytsk3_is_explicit_legacy_extra():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as stream:
        project = stream.read()
    required = project.split("[project.optional-dependencies]", maxsplit=1)[0]
    assert '"PyQt6>=6.11.0"' in required
    assert '"pytsk3>=20260715"' not in required
    assert "legacy-tsk = [" in project
    assert '"pytsk3>=20260715"' in project

    with open(os.path.join(ROOT, "install_mountfile.py"), encoding="utf-8") as stream:
        installer = stream.read()
    assert 'BASE_DEPS = ["PyQt6>=6.11.0"]' in installer
    assert '"legacy-tsk"' in installer


def test_launcher_help_exits_zero():
    r = _run_launcher("--help")
    assert r.returncode == 0, r.stderr
    assert "--cli-only" in r.stdout
    assert "--allow-write" in r.stdout


def test_launcher_cheatsheet_lists_every_cli_command():
    from mountfile.cli import COMMANDS

    r = _run_launcher("--cheatsheet")
    assert r.returncode == 0, r.stderr
    for cmd in COMMANDS:
        assert cmd in r.stdout, "the cheatsheet does not document %r" % cmd


def test_launcher_api_only_survives_missing_images():
    """No image/dump given: every section must skip, never raise."""
    r = _run_launcher(
        "--api-only", "--img", "definitely-missing.img", "--dump", "missing.bin"
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stdout
    assert "Python API" in r.stdout


def test_launcher_cli_only_cli_and_api_refuse_gracefully():
    r = _run_launcher(
        "--cli-only", "--img", "definitely-missing.img", "--dump", "missing.bin"
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stdout


@needs_mkfs_vfat
def test_launcher_api_only_is_read_only_by_default(tmp_path):
    """API demonstration must leave evidence bytes untouched by default."""
    img = _make_vfat_image(tmp_path)
    before = _sha256(img)
    r = _run_launcher("--api-only", "--img", img, "--dump", "missing.bin")
    assert r.returncode == 0, r.stderr
    assert "--allow-write" in r.stdout
    assert _sha256(img) == before


@needs_mkfs_vfat
def test_launcher_cli_only_is_read_only_by_default(tmp_path):
    """CLI demo must not write to the image unless explicitly requested."""
    img = _make_vfat_image(tmp_path)
    before = _sha256(img)
    r = _run_launcher("--cli-only", "--img", img, "--dump", "missing.bin")
    assert r.returncode == 0, r.stderr
    assert "--allow-write" in r.stdout
    assert _sha256(img) == before


@needs_mkfs_vfat
def test_launcher_api_only_writes_only_with_explicit_opt_in(tmp_path):
    """The API write demo remains available behind an explicit opt-in."""
    img = _make_vfat_image(tmp_path)
    r = _run_launcher(
        "--api-only", "--allow-write", "--img", img, "--dump", "missing.bin"
    )
    assert r.returncode == 0, r.stderr
    assert "FAT32" in r.stdout
    assert "Traceback" not in r.stdout

    # the demo must clean up after itself: no api_test/ and no hello.txt left
    from mountfile.fat32 import Fat32

    with open(img, "r+b") as fh:
        fs = Fat32(fh, 0)
        names = [e.name for e in fs.list_dir(fs.find_dir("/"))]
    assert "API_TEST" not in [n.upper() for n in names]
    assert "HELLO.TXT" in [n.upper() for n in names], names


def test_pyinstaller_entrypoints_collect_package_data():
    """Every packaged build must include icons and RT809 XOR tables."""
    build_files = (
        "build_exe.sh",
        "build_exe.bat",
        "Makefile",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "MountFile-linux-x86-64.spec",
    )
    for relative in build_files:
        with open(os.path.join(ROOT, relative), encoding="utf-8") as stream:
            text = stream.read()
        assert (
            "collect-data mountfile" in text
            or "'mountfile')" in text
            or "collect_data_files('mountfile')" in text
        ), relative

    with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as stream:
        makefile = stream.read()
    assert "rm -rf build dist\n" in makefile
    assert "rm -rf build dist *.spec" not in makefile

    for table in ("XOR_MTK_00.bin", "XOR_MST_00.bin"):
        path = os.path.join(ROOT, "mountfile", "data", table)
        assert os.path.isfile(path) and os.path.getsize(path) > 0
