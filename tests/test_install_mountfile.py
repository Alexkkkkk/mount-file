"""Unit tests for the standalone MountFile installer.

These tests mock all side effects: no venv, pip, package manager, or PowerShell
component installer is executed while testing the control flow.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import install_mountfile as installer


def test_pip_failure_is_propagated_and_stops_before_dependency_install():
    """A failing pip bootstrap must not be turned into a successful install."""

    with patch.object(installer, "run", return_value=19) as run:
        assert installer.pip_install("/venv/bin/python", ["example>=1"]) == 19

    assert run.call_count == 1
    assert run.call_args.args[0] == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "setuptools",
        "wheel",
    ]


def test_main_does_not_print_done_when_python_install_fails(capsys):
    """A real pip error is returned and the completion banner is suppressed."""

    with (
        patch.object(installer, "ensure_venv", return_value="/venv/bin/python"),
        patch.object(installer, "pip_install", return_value=42),
    ):
        assert installer.main(["--minimal"]) == 42

    output = capsys.readouterr().out
    assert "Готово!" not in output


def test_linux_system_failure_is_propagated_without_install_command():
    """A failed apt update is returned unchanged and aborts the apt transaction."""

    with (
        patch.object(installer, "have", return_value=True),
        patch.object(installer, "is_admin", return_value=True),
        patch.object(installer, "run", return_value=31) as run,
    ):
        assert installer.install_system_linux() == 31

    assert run.call_count == 1
    assert run.call_args.args[0] == ["apt-get", "update", "-qq"]


def test_dry_run_never_starts_a_subprocess_or_creates_venv(tmp_path):
    """Dry-run only prints planned commands, including the Windows dispatcher."""

    planned_venv = tmp_path / ".venv"
    with (
        patch.object(installer, "VENV_DIR", planned_venv),
        patch.object(installer, "IS_WINDOWS", True),
        patch.object(installer.subprocess, "run") as subprocess_run,
    ):
        assert installer.ensure_venv(dry_run=True) == str(
            planned_venv / "Scripts" / "python.exe"
        )
        assert (
            installer.install_system_windows(
                dry_run=True, skip_ext=True, skip_imdisk=True
            )
            == 0
        )
        assert installer.run(["a-command-that-must-not-run"], dry_run=True) == 0

    assert not planned_venv.exists()
    subprocess_run.assert_not_called()


def test_minimal_profile_does_not_require_yara():
    """Minimal verification has only base imports and must not probe DFIR yara."""

    checks = installer.checks_for_profile(minimal=True, dev=False)
    assert [name for name, _ in checks] == ["PyQt6", "mountfile", "pytsk3"]
    assert all("yara" not in source for _, source in checks)


def test_editable_install_uses_absolute_project_path_from_any_cwd(tmp_path):
    """The editable requirement stays rooted at this checkout, not Path.cwd()."""

    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch.object(installer, "run", return_value=0) as run:
            assert (
                installer.install_project(
                    "/venv/bin/python", extras=["api", "ai", "dfir"]
                )
                == 0
            )
    finally:
        os.chdir(old_cwd)

    command = run.call_args.args[0]
    requirement = command[-1]
    assert requirement == f"{installer.PROJECT_DIR}[api,ai,dfir]"
    assert Path(requirement.split("[", 1)[0]).is_absolute()
    assert str(tmp_path) not in requirement


def test_windows_dispatches_absolute_powershell_helper_and_skip_flags():
    """Windows is delegated to the root helper with exactly the matching flags."""

    with patch.object(installer, "run", return_value=0) as run:
        assert installer.install_system_windows(skip_ext=True, skip_imdisk=True) == 0

    assert run.call_args.args[0] == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer.WINDOWS_COMPONENTS_SCRIPT),
        "-SkipExt",
        "-SkipImDisk",
    ]
    assert Path(run.call_args.args[0][5]).is_absolute()


def test_windows_3010_continues_python_install_but_is_final_status(capsys):
    """A reboot-required WSL install does not skip Python deps or print Done."""

    with (
        patch.object(installer, "IS_WINDOWS", True),
        patch.object(installer, "IS_LINUX", False),
        patch.object(installer, "IS_MAC", False),
        patch.object(
            installer,
            "install_system_components",
            return_value=installer.REBOOT_REQUIRED,
        ) as install_system,
        patch.object(
            installer, "ensure_venv", return_value="C:/work/.venv/Scripts/python.exe"
        ),
        patch.object(installer, "pip_install", return_value=0) as pip_install,
        patch.object(installer, "install_project", return_value=0) as install_project,
        patch.object(installer, "verify_installation", return_value=0) as verify,
    ):
        assert installer.main([]) == installer.REBOOT_REQUIRED

    install_system.assert_called_once()
    pip_install.assert_called_once()
    install_project.assert_called_once()
    verify.assert_called_once()
    assert "Готово!" not in capsys.readouterr().out


def test_run_returns_subprocess_failure_code():
    """The primitive exposes the command failure code without raising it away."""

    completed = subprocess.CompletedProcess(["false"], 73)
    with patch.object(installer.subprocess, "run", return_value=completed):
        assert installer.run(["false"]) == 73
