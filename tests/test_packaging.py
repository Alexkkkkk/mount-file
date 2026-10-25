# SPDX-License-Identifier: MIT
"""Offline wheel smoke tests for the supported commands and package resources."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_exposes_supported_entrypoint_and_windows_resource(tmp_path):
    """Build/install a wheel without index access and exercise its real CLI."""
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "README.md", source / "README.md")
    shutil.copy2(ROOT / "LICENSE", source / "LICENSE")
    shutil.copytree(ROOT / "mountfile", source / "mountfile")
    for module in (
        "mountfile_rw_complete.py",
        "mountfile_rw_v2.py",
        "mountfile_universal_rw.py",
    ):
        shutil.copy2(ROOT / module, source / module)

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheels),
            ".",
        ],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    wheel_paths = list(wheels.glob("mountfile-*.whl"))
    assert len(wheel_paths) == 1
    wheel_path = wheel_paths[0]
    resource_name = "mountfile/data/install-windows-components.ps1"
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        assert resource_name in names
        assert "install_mountfile.py" not in names
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = wheel.read(entry_points_name).decode("utf-8")
        assert "mountfile = mountfile.__main__:main" in entry_points
        assert "mountfile-install" not in entry_points
        assert (
            wheel.read(resource_name)
            == (ROOT / "scripts/install-windows-components.ps1").read_bytes()
        )

    target = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(target),
            str(wheel_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    smoke_code = """
from importlib.resources import files
from mountfile.__main__ import main
resource = files('mountfile').joinpath('data', 'install-windows-components.ps1')
assert resource.is_file()
assert resource.read_bytes()
try:
    main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError('the standard CLI help did not exit')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target)
    smoke = subprocess.run(
        [sys.executable, "-c", smoke_code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    assert "usage:" in smoke.stdout.lower()
