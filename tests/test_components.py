# SPDX-License-Identifier: MIT
"""Tests for optional system-component installation entry points."""

import os
import sys

import pytest

from mountfile import components


def test_windows_installer_uses_packaged_data_in_frozen_build(monkeypatch, tmp_path):
    package_root = tmp_path / "bundle O'Brien" / "mountfile"
    script = package_root / "data" / components.WINDOWS_INSTALLER
    script.parent.mkdir(parents=True)
    script.write_text("# packaged installer\n", encoding="utf-8")
    monkeypatch.setattr(components.resources, "files", lambda package: package_root)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\\MountFile\\MountFile.exe")

    command = components.windows_install_command()
    quoted_path = str(script).replace("'", "''")

    assert command == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-NoExit",
        "-Command",
        f"& '{quoted_path}'; Write-Host ('MountFile installer exit code: ' + $LASTEXITCODE)",
    ]
    assert sys.executable not in command


def test_windows_installer_launches_nonblocking_new_console(monkeypatch):
    calls = []
    process = object()
    monkeypatch.setattr(components, "is_windows", lambda: True)
    monkeypatch.setattr(
        components, "windows_installer_path", lambda: r"C:\MountFile\installer.ps1"
    )
    monkeypatch.setattr(components.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(components.subprocess, "Popen", fake_popen)

    assert (
        components.launch_windows_component_installer(skip_imdisk=True, skip_ext=True)
        is process
    )
    assert calls == [
        (
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-NoExit",
                "-Command",
                "& 'C:\\MountFile\\installer.ps1' -SkipImDisk -SkipExt; "
                "Write-Host ('MountFile installer exit code: ' + $LASTEXITCODE)",
            ],
            {"creationflags": 16},
        )
    ]


def test_installation_hint_is_safe_on_non_windows(monkeypatch):
    monkeypatch.setattr(components, "is_windows", lambda: False)

    assert "python3 install_mountfile.py" in components.installation_hint()


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt_compat = pytest.importorskip(
        "mountfile.qt_compat", exc_type=ImportError, reason="Qt not installed"
    )
    return qt_compat.QApplication.instance() or qt_compat.QApplication([])


def test_components_action_is_in_configuration_menu(monkeypatch, qapp):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    from mountfile.tc_window import MainWindow

    window = MainWindow()
    try:
        configuration = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text() == "Конфигурация"
        )
        assert configuration is not None
        assert "Установить компоненты…" in [
            action.text() for action in configuration.actions()
        ]
    finally:
        window.close()
