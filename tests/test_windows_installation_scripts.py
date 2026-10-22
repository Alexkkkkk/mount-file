# SPDX-License-Identifier: MIT
"""Packaging and mock-only PowerShell tests; never execute a real installation."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_installer_matches_source():
    source = ROOT / "scripts/install-windows-components.ps1"
    packaged = ROOT / "mountfile/data/install-windows-components.ps1"
    assert source.read_bytes() == packaged.read_bytes()
    assert source.read_bytes().startswith(b"\xef\xbb\xbf")


def test_windows_installers_are_shipped_and_verified():
    script = (ROOT / "scripts/install-windows-components.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "Get-AuthenticodeSignature" in script
    assert "Get-FileHash" in script and "SHA256" in script
    assert "MountFileTools" in script
    assert "--unregister" not in script
    assert "--set-default" not in script
    assert "Restart-Computer" not in script
    assert "--mount" not in script
    assert "Format-Volume" not in script
    assert "-NoRestart" in script
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"data/*.ps1"' in pyproject
    assert (ROOT / "install.bat").is_file()
    assert (ROOT / "install-components.bat").is_file()


def test_launcher_uses_venv_and_first_run_bootstrap():
    launcher = (ROOT / "run.bat").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in launcher
    assert "scripts\\install-windows.ps1" in launcher
    assert "3010" in launcher
    assert '"%PYTHON%" "%~dp0main.py" %*' in launcher


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_powershell_mock_suite():
    result = subprocess.run(
        [
            shutil.which("pwsh"),
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts/test-windows-installers.ps1"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, output
    assert "8 mock assertions passed" in output
