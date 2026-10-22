# SPDX-License-Identifier: MIT
"""Windows WSL fallback tests for the transactional ext writer."""

from types import SimpleNamespace

from mountfile import ext_debugfs_writer


def test_find_debugfs_falls_back_to_default_wsl(monkeypatch):
    monkeypatch.setattr(
        ext_debugfs_writer.shutil,
        "which",
        lambda name: "C:/Windows/System32/wsl.exe" if name == "wsl.exe" else None,
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="/usr/sbin/debugfs\n", stderr="")

    monkeypatch.setattr(ext_debugfs_writer.subprocess, "run", fake_run)

    found, wsl = ext_debugfs_writer._find_debugfs(windows=True)

    assert found == "/usr/sbin/debugfs"
    assert wsl == "C:/Windows/System32/wsl.exe"
    assert calls[0][0] == [
        "C:/Windows/System32/wsl.exe",
        "--exec",
        "sh",
        "-lc",
        "command -v debugfs",
    ]


def test_wsl_runner_converts_host_paths_and_invokes_debugfs(monkeypatch, tmp_path):
    writer = ext_debugfs_writer.Ext234ImageWriter.__new__(
        ext_debugfs_writer.Ext234ImageWriter
    )
    writer.debugfs = "/usr/sbin/debugfs"
    writer.wsl_executable = "wsl.exe"
    writer._wsl_path_cache = {}
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[2] == "wslpath":
            return SimpleNamespace(
                returncode=0,
                stdout="/mnt/c/Users/Test/filesystem.ext\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="Filesystem OK", stderr="")

    monkeypatch.setattr(ext_debugfs_writer.subprocess, "run", fake_run)
    command = writer._run(tmp_path / "filesystem.ext", "ls -l /", write=True)

    assert command == "Filesystem OK\n"
    assert calls[-1] == [
        "wsl.exe",
        "--exec",
        "/usr/sbin/debugfs",
        "-w",
        "-R",
        "ls -l /",
        "/mnt/c/Users/Test/filesystem.ext",
    ]
