# SPDX-License-Identifier: MIT
"""Contract tests for the machine-readable toolchain provenance record."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "toolchain_provenance.py"


@pytest.fixture(scope="module")
def payload():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_provenance_record_has_the_documented_shape(payload):
    assert payload["schema"] == "mountfile.toolchain-provenance/1"
    assert payload["mountfile_version"]
    assert set(payload["python"]) == {"version", "implementation", "compiler"}
    assert {"system", "release", "machine"} == set(payload["platform"])
    assert set(payload["lockfiles"]) == {
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
    }
    assert payload["libraries"] and payload["tools"]


def test_provenance_never_upgrades_a_missing_component_to_a_pass(payload):
    # Absence is reported as null (the "unavailable" state), not as a version.
    for name, value in payload["libraries"].items():
        assert value is None or isinstance(value, str), name
    for name, value in payload["tools"].items():
        assert value is None or isinstance(value, str), name


def test_provenance_hashes_are_sha256_hex(payload):
    digests = [value for value in payload["lockfiles"].values() if value is not None]
    assert digests, "at least pyproject.toml must be hashed"
    for digest in digests:
        assert len(digest) == 64
        int(digest, 16)


def test_provenance_output_file_is_never_overwritten(tmp_path, payload):
    target = tmp_path / "provenance.json"
    first = subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(target)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == payload["schema"]
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "-o", str(target)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert second.returncode == 1
    assert "already exists" in second.stderr
