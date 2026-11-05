# SPDX-License-Identifier: MIT
"""Installation audit: mock OS operations, real wheel/build smoke elsewhere."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import install_mountfile as installer
from mountfile import ollama_setup, updater

ROOT = Path(__file__).resolve().parents[1]
# Loop devices, udisksctl and /proc/mounts exist only where os.geteuid does;
# the same tests must not turn into AttributeError noise on Windows runners.
POSIX_EUID = hasattr(os, "geteuid")
requires_posix_euid = pytest.mark.skipif(
    not POSIX_EUID, reason="loop/udisksctl paths require a POSIX os.geteuid"
)


@pytest.fixture(scope="module")
def standalone():
    spec = importlib.util.spec_from_file_location(
        "audit_standalone", ROOT / "mountfile.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_verifies_compiled_qt_and_real_cli_import():
    probes = dict(installer.checks_for_profile(minimal=True, dev=False))
    assert "QtWidgets" in probes["PyQt6"]
    assert "mountfile.__main__" in probes["mountfile"]
    for code in probes.values():
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


def test_full_profile_checks_all_runtime_extras():
    names = dict(installer.checks_for_profile(minimal=False, dev=False))
    assert {
        "fastapi",
        "uvicorn",
        "httpx",
        "mcp",
        "pydantic",
        "openai",
        "regex",
        "dateutil",
        "yara",
    } <= names.keys()
    assert "pytsk3" not in names
    assert "legacy-tsk" not in installer.selected_extras(minimal=False, dev=False)
    legacy_names = dict(
        installer.checks_for_profile(minimal=False, dev=False, legacy_tsk=True)
    )
    assert "pytsk3" in legacy_names
    assert installer.selected_extras(minimal=True, dev=False, legacy_tsk=True) == [
        "legacy-tsk"
    ]


@requires_posix_euid
def test_linux_loop_setup_is_read_only_and_does_not_create_ignored_target(
    standalone, monkeypatch, tmp_path
):
    backend = standalone.LinuxBackend()
    monkeypatch.setattr(standalone.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(standalone.shutil, "which", lambda _: "udisksctl")
    calls = []

    def run(argv):
        calls.append(argv)
        return (
            "Set up /dev/loop7\n"
            if argv[1] == "loop-setup"
            else "Mounted /dev/loop7 at /media/user/disk.\n"
        )

    monkeypatch.setattr(backend, "_run", run)
    target = tmp_path / "ignored-target"
    assert backend.mount(tmp_path / "evidence.img", target) == Path("/media/user/disk")
    assert "--read-only" in calls[0]
    assert not target.exists()


@requires_posix_euid
def test_linux_mount_failure_detaches_loop_device(standalone, monkeypatch, tmp_path):
    backend = standalone.LinuxBackend()
    monkeypatch.setattr(standalone.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(standalone.shutil, "which", lambda _: "udisksctl")
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[1] == "loop-setup":
            return "Set up /dev/loop9\n"
        if argv[1] == "mount":
            raise standalone.MountError("unknown filesystem")
        return ""

    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(standalone.MountError, match="unknown filesystem"):
        backend.mount(tmp_path / "image.img", tmp_path / "mount")
    assert calls[-1] == ["udisksctl", "loop-delete", "-b", "/dev/loop9"]


@requires_posix_euid
def test_linux_unmount_resolves_mountpoint_to_loop_and_cleans_up(
    standalone, monkeypatch, tmp_path
):
    backend = standalone.LinuxBackend()
    target = tmp_path / "disk name"
    monkeypatch.setattr(standalone.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(standalone.shutil, "which", lambda _: "udisksctl")
    monkeypatch.setattr(
        backend,
        "list_mounted",
        lambda: [{"device": "/dev/loop4", "target": str(target)}],
    )
    calls = []
    monkeypatch.setattr(backend, "_run", lambda argv: calls.append(argv))
    backend.unmount(target)
    assert calls == [
        ["udisksctl", "unmount", "-b", "/dev/loop4"],
        ["udisksctl", "loop-delete", "-b", "/dev/loop4"],
    ]


@requires_posix_euid
def test_linux_unmount_does_not_use_parent_or_user_device(
    standalone, monkeypatch, tmp_path
):
    backend = standalone.LinuxBackend()
    monkeypatch.setattr(standalone.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(standalone.shutil, "which", lambda _: "udisksctl")
    monkeypatch.setattr(
        backend,
        "list_mounted",
        lambda: [{"device": "/dev/loop4", "target": str(tmp_path)}],
    )
    monkeypatch.setattr(
        backend, "_run", lambda _: pytest.fail("must not unmount a parent")
    )
    with pytest.raises(standalone.MountError):
        backend.unmount(tmp_path / "child")


def test_linux_proc_mounts_decodes_space_and_backslash(standalone, monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_a, **_k: r"/dev/loop5 /media/disk\040name\134x ext4 ro 0 0" + "\n",
    )
    assert standalone.LinuxBackend().list_mounted() == [
        {"device": "/dev/loop5", "target": "/media/disk name\\x", "fs": "ext4"}
    ]


@requires_posix_euid
def test_linux_root_mount_uses_read_only_and_argument_terminator(
    standalone, monkeypatch, tmp_path
):
    monkeypatch.setattr(standalone.os, "geteuid", lambda: 0)
    calls = []
    backend = standalone.LinuxBackend()
    monkeypatch.setattr(backend, "_run", lambda argv: calls.append(argv))
    image, target = tmp_path / "-image.img", tmp_path / "-target"
    assert backend.mount(image, target) == target
    assert calls == [["mount", "-o", "loop,ro", "--", str(image), str(target)]]


def test_linux_run_uses_stable_locale(standalone, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(standalone.subprocess, "run", run)
    assert standalone.LinuxBackend._run(["udisksctl", "mount"]) == "ok"
    assert seen["env"]["LC_ALL"] == "C"


def test_linux_backend_without_geteuid_reports_human_error(standalone, monkeypatch):
    """Off POSIX the loop backend must refuse with MountError, not AttributeError."""
    monkeypatch.delattr(standalone.os, "geteuid", raising=False)
    backend = standalone.LinuxBackend()
    with pytest.raises(standalone.MountError, match="Linux"):
        backend.mount(Path("/tmp/image.img"), Path("/tmp/target"))
    with pytest.raises(standalone.MountError, match="Linux"):
        backend.unmount(Path("/tmp/target"))


def test_mac_mount_info_uses_plist_not_whitespace_columns(standalone, monkeypatch):
    payload = {
        "images": [
            {
                "image-path": "/data/disk image.iso",
                "system-entities": [
                    {"dev-entry": "/dev/disk2s1", "mount-point": "/Volumes/My Disk"}
                ],
            }
        ]
    }
    monkeypatch.setattr(
        standalone.LinuxBackend, "_run", lambda argv: plistlib.dumps(payload).decode()
    )
    assert standalone.MacBackend().list_mounted() == [
        {
            "device": "/dev/disk2s1",
            "target": "/Volumes/My Disk",
            "image": "/data/disk image.iso",
        }
    ]


@pytest.mark.parametrize("backend", ["LinuxBackend", "MacBackend", "WindowsBackend"])
def test_missing_os_tool_is_human_mount_error(
    standalone, monkeypatch, tmp_path, backend
):
    # Windows has no os.geteuid; every backend must still fail with a human
    # MountError instead of leaking AttributeError/FileNotFoundError.
    if POSIX_EUID:
        monkeypatch.setattr(standalone.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        standalone.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("tool absent")),
    )
    with pytest.raises(standalone.MountError):
        getattr(standalone, backend)().mount(tmp_path / "image.iso", tmp_path / "mount")


def test_windows_ps_binds_literal_arguments_without_injection(standalone, monkeypatch):
    calls = []
    path = "C:\\Images\\O'Brien & тест.iso"

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "E\n", "")

    monkeypatch.setattr(standalone.subprocess, "run", run)
    assert standalone.WindowsBackend._ps("param($p) Write-Output $p", path) == "E\n"
    argv = calls[0]
    assert argv[-2] == "-EncodedCommand"
    decoded = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert (
        "& { param($p) Write-Output $p } 'C:\\Images\\O''Brien & тест.iso'" in decoded
    )
    assert "$ErrorActionPreference = 'Stop'" in decoded
    assert "-Access ReadOnly" in standalone.WindowsBackend.PS_MOUNT


@pytest.mark.parametrize("letter", ["", "AB", "E\nF", "??"])
def test_windows_mount_requires_one_drive_letter(
    standalone, monkeypatch, tmp_path, letter
):
    backend = standalone.WindowsBackend()
    monkeypatch.setattr(backend, "_ps", lambda *_: letter)
    with pytest.raises(standalone.MountError, match="drive letter"):
        backend.mount(tmp_path / "image.iso", tmp_path)


def test_windows_unmount_resolves_attached_image_not_partition_number(
    standalone, monkeypatch
):
    calls = []
    backend = standalone.WindowsBackend()
    monkeypatch.setattr(backend, "_ps", lambda *args: calls.append(args))
    backend.unmount(Path("E:\\"))
    script, letter = calls[0]
    assert letter == "E"
    assert "MSFT_DiskImage" in script
    assert "Get-Volume" in script
    assert "Dismount-DiskImage -ImagePath $images[0].ImagePath" in script
    with pytest.raises(standalone.MountError):
        backend.unmount(Path("E:\\user-data"))
    assert len(calls) == 1


def test_windows_list_handles_single_json_record(standalone, monkeypatch):
    backend = standalone.WindowsBackend()
    monkeypatch.setattr(
        backend,
        "_ps",
        lambda *_: json.dumps(
            {"DevicePath": "device", "ImagePath": "disk.iso", "Target": "E:\\"}
        ),
    )
    assert backend.list_mounted() == [
        {"device": "device", "target": "E:\\", "image": "disk.iso"}
    ]


def test_standalone_cli_unsupported_platform_is_not_traceback(
    standalone, monkeypatch, capsys
):
    monkeypatch.setattr(standalone, "setup_logging", lambda **_: None)
    monkeypatch.setattr(
        standalone,
        "get_backend",
        lambda: (_ for _ in ()).throw(standalone.MountError("Unsupported platform")),
    )
    assert standalone.cli_main(["--list"]) == 1
    assert "Error: Unsupported platform" in capsys.readouterr().err


@pytest.mark.parametrize(
    "platform_name,machine,expected",
    [
        ("linux", "x86_64", "MountFile-linux-x86-64"),
        ("linux", "aarch64", "MountFile-linux-arm64"),
        ("darwin", "arm64", "MountFile-macos-arm64"),
        ("win32", "AMD64", "MountFile.exe"),
    ],
)
def test_update_selection_rejects_wrong_os_arch_and_archives(
    monkeypatch, platform_name, machine, expected
):
    monkeypatch.setattr(updater.sys, "platform", platform_name)
    monkeypatch.setattr(updater.platform, "machine", lambda: machine)
    names = [
        "MountFile-macos-arm64",
        "MountFile-linux-x86-64",
        "MountFile-linux-arm64",
        "MountFile.exe",
        "MountFile-linux-x86-64.tar.gz",
        "MountFile-macos-arm64.dmg",
    ]
    assert (
        updater.select_asset({"assets": [{"name": name} for name in names]})["name"]
        == expected
    )


def test_update_linux_does_not_fallback_to_macos_binary(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    assert updater.select_asset({"assets": [{"name": "MountFile-macos-arm64"}]}) is None


def test_checksum_fallback_verifies_not_dummy_success(monkeypatch):
    monkeypatch.setattr(updater, "_olma_sha", None)
    assert updater._verify_sha256(b"abc", updater.hashlib.sha256(b"abc").hexdigest())
    assert not updater._verify_sha256(b"abc", "0" * 64)
    assert not updater._verify_sha256(b"abc", None)


def test_safe_download_fallback_enforces_bound(monkeypatch):
    monkeypatch.setattr(updater, "_olma_dl", None)
    monkeypatch.setattr(updater, "_request", lambda *_: io.BytesIO(b"123456"))
    with pytest.raises(OSError, match="лимит"):
        updater._safe_download("https://example.invalid", max_size=5)


@pytest.mark.parametrize("name", ["../x", "..", ".", r"..\x", "C:x", "bad\nname"])
def test_update_download_rejects_paths_before_network(monkeypatch, tmp_path, name):
    monkeypatch.setattr(
        updater, "_request", lambda *_: pytest.fail("must reject before downloading")
    )
    with pytest.raises(ValueError, match="имя"):
        updater.download_asset({"name": name}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_update_download_failure_preserves_previous_file_and_cleans_partial(
    monkeypatch, tmp_path
):
    previous = tmp_path / "MountFile.exe"
    previous.write_bytes(b"old")
    monkeypatch.setattr(updater, "_request", lambda *_: io.BytesIO(b"short"))
    with pytest.raises(OSError):
        updater.download_asset({"name": "MountFile.exe", "size": 100}, str(tmp_path))
    assert previous.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MountFile.exe"]


def test_update_rejects_non_network_scheme():
    with pytest.raises(ValueError, match="HTTP"):
        updater._request("file:///etc/passwd")


def test_git_upgrade_propagates_command_failure_and_accepts_worktree(
    monkeypatch, tmp_path
):
    (tmp_path / ".git").write_text("gitdir: /no-real-repository\n")
    monkeypatch.setenv("MOUNTFILE_GIT_PULL", "1")
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 42, "", "permission denied"),
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        updater.git_upgrade(str(tmp_path))


def test_source_update_opens_release_without_binary_download(monkeypatch):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(updater, "git_upgrade", lambda _: None)
    monkeypatch.setattr(
        updater,
        "download_asset",
        lambda *_a, **_k: pytest.fail("source must not download binary"),
    )
    opened = []
    monkeypatch.setattr(updater.webbrowser, "open", opened.append)
    worker = updater.Installer(
        {"url": "https://example.invalid/release", "asset": {"name": "unused"}}
    )
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed
    assert opened == ["https://example.invalid/release"]


def test_windows_swap_paths_escape_percent_and_preserve_metacharacters(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(updater.tempfile, "tempdir", str(tmp_path))
    script = updater._windows_swap_script(
        r"C:\new%TEMP% & x.exe", r"C:\тест!\MountFile.exe", 5, str(tmp_path / "log")
    )
    # The swap script is written as UTF-8 (it runs `chcp 65001`), so reading it
    # must not fall back to the Windows ANSI code page.
    text = Path(script).read_text(encoding="utf-8")
    assert "DisableDelayedExpansion" in text
    assert "chcp 65001" in text
    assert 'move /Y "C:\\new%%TEMP%% & x.exe"' in text
    assert 'left at "C:\\new%%TEMP%% & x.exe"' in text
    assert "тест!" in text
    assert (
        updater._windows_swap_script("new", "target", 5, str(tmp_path / "log"))
        != script
    )


@pytest.mark.parametrize("model", ["-h", "--help", "-version", "model;whoami"])
def test_ollama_setup_rejects_cli_options(monkeypatch, model):
    monkeypatch.setattr(ollama_setup, "is_windows", lambda: True)
    monkeypatch.setattr(
        ollama_setup.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("unsafe model must not launch"),
    )
    with pytest.raises(ollama_setup.OllamaSetupError):
        ollama_setup.launch_windows_ollama_setup(model)


def test_headless_updater_signals_are_per_instance_and_worker_runs(monkeypatch):
    monkeypatch.setitem(sys.modules, "mountfile.qt_compat", None)
    spec = importlib.util.spec_from_file_location(
        "mountfile._audit_headless_updater", ROOT / "mountfile/updater.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module.HAVE_QT
    monkeypatch.setattr(module, "check", lambda _: None)
    first, second = module.Updater(current_version="1"), module.Updater(
        current_version="2"
    )
    statuses = []
    first.status.connect(statuses.append)
    second.run()
    assert statuses == []
    first.start()
    assert first.wait(2000)
    assert first.isFinished()
    assert statuses and "Обновлений нет" in statuses[-1]
    assert module.Installer({}).parent() is None


@pytest.mark.skipif(os.name == "nt", reason="bash build script")
@pytest.mark.parametrize("failure", ["", "qt", "build"])
def test_unix_build_script_checks_real_exit_and_preserves_spec(tmp_path, failure):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "build_exe.sh", source / "build_exe.sh")
    spec = source / "MountFile-linux-x86-64.spec"
    spec.write_text("hand-maintained spec\n")
    (source / "dist").mkdir()
    (source / "dist" / "AuditBuild").write_bytes(b"old executable")
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        + """
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:1] == ['-c']:
    if 'QtWidgets' in args[1] and os.environ['FAILURE'] == 'qt':
        sys.exit(31)
    if args[1] == 'import pytsk3':
        sys.exit(1)
if args[:2] == ['-m', 'PyInstaller']:
    if os.environ['FAILURE'] == 'build':
        sys.exit(47)
    pathlib.Path('dist', args[args.index('--name') + 1]).write_bytes(b'new executable')
"""
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PYTHON": str(fake),
        "NAME": "AuditBuild",
        "FAILURE": failure,
        "CALL_LOG": str(tmp_path / "calls"),
    }
    result = subprocess.run(
        ["bash", str(source / "build_exe.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == {"": 0, "qt": 31, "build": 47}[failure], (
        result.stdout + result.stderr
    )
    assert spec.read_text() == "hand-maintained spec\n"
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert not any(argv[:2] == ["-m", "pip"] for argv in calls)
    if not failure:
        build = next(argv for argv in calls if argv[:2] == ["-m", "PyInstaller"])
        assert build[build.index("--specpath") + 1] == "build/spec"
        assert build[build.index("--hidden-import") + 1] == "mountfile_dfir_tools"
    else:
        assert "Done:" not in result.stdout


def test_legacy_extra_requires_explicit_cli_option():
    assert not installer.build_parser().parse_args([]).legacy_tsk
    assert installer.build_parser().parse_args(["--legacy-tsk"]).legacy_tsk


def _isolate_data_dir(standalone, monkeypatch, tmp_path):
    """Point the app data directory at ``tmp_path`` on every platform.

    Setting ``XDG_CONFIG_HOME`` is not enough: macOS resolves
    ``~/Library/Application Support``, so a test that only sets XDG writes lock
    files and symlinks into the real user profile and fails on the next run
    with ``FileExistsError``.
    """

    data_dir = tmp_path / "appdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(data_dir))
    monkeypatch.setattr(standalone.Paths, "data", classmethod(lambda cls: data_dir))
    return data_dir


def test_posix_instance_lock_does_not_unlink_live_inode(
    standalone, monkeypatch, tmp_path
):
    if os.name == "nt":
        pytest.skip("flock is POSIX-only")
    _isolate_data_dir(standalone, monkeypatch, tmp_path)
    first, second = standalone.SingleInstance(), standalone.SingleInstance()
    try:
        assert first.acquire()
        inode = standalone.Paths.lock_file().stat().st_ino
        assert not second.acquire()
        assert second._fh is None, "failed lock attempt leaked its open file"
        first.release()
        assert standalone.Paths.lock_file().stat().st_ino == inode
        assert second.acquire()
        assert standalone.Paths.lock_file().stat().st_ino == inode
    finally:
        first.release()
        second.release()


def test_posix_instance_lock_refuses_symlink(standalone, monkeypatch, tmp_path):
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("requires POSIX O_NOFOLLOW")
    data_dir = _isolate_data_dir(standalone, monkeypatch, tmp_path)
    original = tmp_path / "must-not-touch.txt"
    original.write_text("original data")
    (data_dir / standalone.Paths.lock_file().name).symlink_to(original)
    lock = standalone.SingleInstance()
    assert not lock.acquire()
    assert original.read_text() == "original data"


def test_windows_instance_lock_failure_is_not_dummy_success(standalone, monkeypatch):
    monkeypatch.setattr(
        standalone.ctypes,
        "WinDLL",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("lock failed")),
        raising=False,
    )
    assert not standalone.SingleInstance()._win()


def test_docker_optional_legacy_preserves_read_only_security():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("read_only: true") >= 2
    assert "cap_drop:\n      - ALL" in compose
    assert "security_opt:\n      - no-new-privileges:true" in compose
    assert 'INSTALL_LEGACY_TSK: "${INSTALL_LEGACY_TSK:-0}"' in compose
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG INSTALL_LEGACY_TSK=0" in dockerfile
    assert "--only-binary=:all:" in dockerfile
