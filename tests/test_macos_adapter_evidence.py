# SPDX-License-Identifier: MIT
"""Issue #510, E1: macOS read-only adapter contract evidence gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.macos_adapter_evidence import (
    REQUIRED_MACOS_ADAPTER_TESTS,
    build_macos_adapter_evidence,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_JUNIT_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="macos-adapter-contract" tests="{total}">
{cases}
</testsuite>
"""


def _case(name: str, *, failure: bool = False) -> str:
    if failure:
        return f'  <testcase name="{name}" classname="macos"><failure message="boom"/></testcase>'
    return f'  <testcase name="{name}" classname="macos"/>'


def _write_junit(tmp_path: Path, failing: set[str] = frozenset()) -> Path:
    """Emit one JUnit entry per declared case, expanded for parametrization."""

    names: list[str] = []
    for base, count in REQUIRED_MACOS_ADAPTER_TESTS.items():
        for index in range(count):
            names.append(base if count == 1 else f"{base}[param{index}]")
    cases = "\n".join(
        _case(name, failure=name.split("[", 1)[0] in failing) for name in names
    )
    path = tmp_path / "junit-macos.xml"
    path.write_text(
        _JUNIT_TEMPLATE.format(total=len(names), cases=cases), encoding="utf-8"
    )
    return path


def test_full_passing_macos_run_is_a_typed_pass(tmp_path):
    report = build_macos_adapter_evidence(_write_junit(tmp_path))
    assert report["status"] == "pass"
    assert report["blockers"] == []
    assert all(probe["status"] == "pass" for probe in report["probes"].values())


def test_failed_or_missing_cases_are_blocked_not_collected(tmp_path):
    failing = _write_junit(tmp_path, failing={"test_apfs_constructor_never_mounts"})
    report = build_macos_adapter_evidence(failing)
    assert report["status"] == "blocked"
    assert any("never_mounts" in blocker for blocker in report["blockers"])

    partial = tmp_path / "junit-partial.xml"
    names = list(REQUIRED_MACOS_ADAPTER_TESTS)[:-1]
    cases = "\n".join(_case(name) for name in names)
    partial.write_text(
        _JUNIT_TEMPLATE.format(total=len(names), cases=cases), encoding="utf-8"
    )
    report = build_macos_adapter_evidence(partial)
    assert report["status"] == "blocked"
    assert any("not collected" in blocker for blocker in report["blockers"])


def test_missing_report_is_not_collected_and_never_a_pass(tmp_path):
    report = build_macos_adapter_evidence(None)
    assert report["status"] == "not_collected"


def test_cli_exit_codes_follow_the_verdict(tmp_path):
    passing = _write_junit(tmp_path)
    ok = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "macos_adapter_evidence.py"),
            "--junitxml",
            str(passing),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["status"] == "pass"

    missing = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "macos_adapter_evidence.py"),
            "--junitxml",
            str(tmp_path / "absent.xml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["status"] == "not_collected"
