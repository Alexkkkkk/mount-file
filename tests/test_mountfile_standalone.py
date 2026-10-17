# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""
Tests for the standalone single-file build: ``mountfile.py``.

The module is loaded straight from its path with ``importlib`` instead of by
name, because the repository also ships a ``mountfile/`` package — a plain
``import mountfile`` would resolve to the package, not to this file.

Nothing here mounts a real image. The tests cover the parts that must work on
any machine and in CI: metadata, per-user paths, atomic IO, state migration
and persistence, mount-point naming, the single-instance lock, logging and the
whole CLI surface (including its exit codes).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "mountfile.py"

pytestmark = pytest.mark.skipif(
    not TARGET.is_file(), reason="standalone mountfile.py is not present"
)

IS_LINUX = sys.platform.startswith("linux")


def _load():
    """Load the standalone module under a private name (never 'mountfile')."""
    spec = importlib.util.spec_from_file_location("mountfile_standalone", TARGET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mf():
    return _load()


@pytest.fixture()
def isolated_home(mf, tmp_path, monkeypatch):
    """Redirect every per-user path (config, logs, temp) into a scratch dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(mf.tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(mf.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    return tmp_path


# --------------------------------------------------------------------- meta
def test_metadata(mf):
    assert mf.APP_NAME == "MountFile"
    assert mf.APP_DISPLAY == "Mount File"
    assert mf.APP_VERSION == "3.0.0"
    assert mf.APP_REPO.startswith("https://github.com/")
    assert isinstance(mf.SCHEMA_VERSION, int)


def test_platform_flags_are_consistent(mf):
    flags = [mf.IS_WIN, mf.IS_MAC, mf.IS_LINUX]
    assert flags.count(True) <= 1
    assert (sys.platform == "win32") == mf.IS_WIN
    assert sys.platform.startswith("linux") == mf.IS_LINUX


# -------------------------------------------------------------------- paths
def test_paths_are_created_under_the_user_config_dir(mf, isolated_home):
    data = mf.Paths.data()
    logs = mf.Paths.logs()
    tmp = mf.Paths.tmp()
    assert data.is_dir() and logs.is_dir() and tmp.is_dir()
    assert data.name == mf.APP_NAME
    assert logs == data / "logs"
    assert mf.Paths.state_file() == data / "state.json"
    assert mf.Paths.lock_file().name == f"{mf.APP_NAME}.lock"


# ----------------------------------------------------------------- atomic IO
def test_atomic_json_roundtrip(mf, tmp_path):
    path = tmp_path / "nested" / "state.json"
    mf.AtomicIO.write_json(path, {"b": 1, "a": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": [1, 2], "b": 1}
    assert mf.AtomicIO.read_json(path) == {"a": [1, 2], "b": 1}


def test_atomic_write_replaces_and_leaves_no_temp_files(mf, tmp_path):
    path = tmp_path / "state.json"
    mf.AtomicIO.write_bytes(path, b"first")
    mf.AtomicIO.write_bytes(path, b"second")
    assert path.read_bytes() == b"second"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state.json")]
    assert leftovers == []


def test_read_json_falls_back_on_missing_and_corrupt_files(mf, tmp_path):
    path = tmp_path / "state.json"
    assert mf.AtomicIO.read_json(path, default={"d": 1}) == {"d": 1}
    path.write_text("{ this is not json", encoding="utf-8")
    assert mf.AtomicIO.read_json(path, default=None) is None


def test_write_text_is_utf8(mf, tmp_path):
    path = tmp_path / "note.txt"
    mf.AtomicIO.write_text(path, "образ")
    assert path.read_text(encoding="utf-8") == "образ"


# -------------------------------------------------------------------- state
def test_state_migration_filters_junk_and_merges_defaults(mf):
    data = mf.State._migrate(
        {
            "schema": 0,
            "session/recent_images": ["/a.img", 5, None, "/b.iso"],
            "session/recent_mounts": [{"target": "/mnt/a"}, {"nope": 1}, "x"],
            "unknown/key": "kept",
        }
    )
    assert data["schema"] == mf.SCHEMA_VERSION
    assert data["session/recent_images"] == ["/a.img", "/b.iso"]
    assert data["session/recent_mounts"] == [{"target": "/mnt/a"}]
    assert data["unknown/key"] == "kept"
    assert data["ui/theme"] == "system"
    assert data["stats/opens"] == 0


def test_state_remembers_and_forgets_mounts(mf, isolated_home):
    state = mf.State()
    image = isolated_home / "disk.img"

    state.remember_open(image)
    assert state.get("session/recent_images")[0] == str(image)
    assert state.get("stats/opens") == 1

    state.remember_mount(str(image), "/mnt/one")
    state.remember_mount(str(image), "/mnt/two")
    assert [m["target"] for m in state.get("session/recent_mounts")] == [
        "/mnt/two",
        "/mnt/one",
    ]

    state.remember_mount(str(image), "/mnt/two")  # re-mount: moves up, no dupe
    assert len(state.get("session/recent_mounts")) == 2

    state.forget_mount("/mnt/two")
    assert [m["target"] for m in state.get("session/recent_mounts")] == ["/mnt/one"]


def test_state_survives_a_new_instance(mf, isolated_home):
    first = mf.State()
    first.set("ui/theme", "dark")
    first.flush()

    on_disk = mf.AtomicIO.read_json(mf.Paths.state_file())
    assert on_disk["ui/theme"] == "dark"
    assert on_disk["schema"] == mf.SCHEMA_VERSION
    assert mf.State().get("ui/theme") == "dark"


def test_state_caps_and_deduplicates_recent_images(mf, isolated_home):
    state = mf.State()
    for i in range(mf.State.MAX_RECENT + 5):
        state.remember_open(isolated_home / f"img{i}.img")

    items = state.get("session/recent_images")
    assert len(items) == mf.State.MAX_RECENT
    assert items[0].endswith(f"img{mf.State.MAX_RECENT + 4}.img")

    state.remember_open(isolated_home / f"img{mf.State.MAX_RECENT + 4}.img")
    assert len(state.get("session/recent_images")) == mf.State.MAX_RECENT
    assert state.get("session/recent_images").count(items[0]) == 1
    assert state.get("session/recent_images")[0] == items[0]


# ------------------------------------------------------------------ backends
def test_backend_matches_the_running_platform(mf):
    backend = mf.get_backend()
    assert isinstance(backend, mf.MountBackend)
    if mf.IS_WIN:
        assert isinstance(backend, mf.WindowsBackend)
    elif mf.IS_MAC:
        assert isinstance(backend, mf.MacBackend)
    else:
        assert isinstance(backend, mf.LinuxBackend)


def test_mount_error_is_a_plain_exception(mf):
    assert issubclass(mf.MountError, Exception)
    with pytest.raises(mf.MountError):
        raise mf.MountError("boom")


@pytest.mark.skipif(not IS_LINUX, reason="Linux mount-point layout")
def test_default_mount_point_linux(mf):
    assert str(mf.default_mount_point(Path("/data/My Disk.img"))) == (
        f"/tmp/{mf.APP_NAME}/My_Disk"
    )
    long_name = "x" * 120 + ".img"
    assert len(mf.default_mount_point(Path(long_name)).name) <= 40


# ------------------------------------------------------------------- logging
def test_setup_logging_writes_a_rotating_file(mf, isolated_home):
    mf.setup_logging(verbose=False)
    mf.log.info("standalone smoke line")
    for handler in list(mf.logging.getLogger().handlers):
        handler.flush()

    log_file = mf.Paths.logs() / "app.log"
    assert log_file.is_file()
    assert "standalone smoke line" in log_file.read_text(encoding="utf-8")


# ------------------------------------------------------------ single instance
def test_single_instance_lock_is_exclusive(mf, isolated_home):
    first = mf.SingleInstance()
    second = mf.SingleInstance()
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()
        second.release()

    third = mf.SingleInstance()
    try:
        assert third.acquire() is True, "the lock must be reusable after release"
    finally:
        third.release()


# ----------------------------------------------------------------------- GUI
def test_qt_stylesheets_are_present_when_qt_is_available(mf):
    if not mf.HAS_QT:
        pytest.skip("PyQt6 is not installed")
    assert "#1e1f22" in mf.DARK_QSS
    assert mf.LIGHT_QSS.strip()
    assert hasattr(mf, "MainWindow") and hasattr(mf, "CommandPalette")
    assert hasattr(mf, "MountWorker")


# ----------------------------------------------------------------------- CLI
def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(TARGET), *args],
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_cli_version_is_reported():
    result = _run_cli("--version")
    assert result.returncode == 0
    assert "Mount File 3.0.0" in (result.stdout + result.stderr)


def test_cli_help_lists_the_commands():
    result = _run_cli("--help")
    assert result.returncode == 0
    for flag in ("--to", "--unmount", "--list", "--open"):
        assert flag in result.stdout


def test_cli_reports_a_missing_image():
    result = _run_cli(str(ROOT / "definitely-not-here.img"))
    assert result.returncode == 1
    assert "File not found" in result.stderr


@pytest.mark.skipif(not IS_LINUX, reason="the Linux backend owns --list")
def test_cli_list_never_crashes():
    result = _run_cli("--list")
    assert result.returncode == 0
    assert "mounted" in result.stdout.lower() or "/dev/loop" in result.stdout


@pytest.mark.skipif(not IS_LINUX, reason="the Linux backend owns --unmount")
def test_cli_unmount_reports_a_human_error():
    result = _run_cli("--unmount", "/tmp/definitely-not-mounted-by-mountfile")
    assert result.returncode == 1
    assert "Error:" in result.stderr


def test_cli_requires_a_value_for_unmount():
    result = _run_cli("--unmount")
    assert result.returncode == 2
    assert "expected one argument" in (result.stdout + result.stderr).lower()


def test_cli_routes_positional_arguments_to_the_cli(mf):
    """A bare path must never fall through to the GUI branch."""
    source = TARGET.read_text(encoding="utf-8")
    assert "cli_triggers" in source
    assert 'not a.startswith("-")' in source or "not a.startswith('-')" in source
