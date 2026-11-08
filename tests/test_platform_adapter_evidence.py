# SPDX-License-Identifier: MIT
"""Fail-closed aggregation tests for cross-platform adapter evidence."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from scripts.native_read_evidence import (
    _REQUIRED_ADAPTER_TESTS,
    _platform_adapter_evidence,
)


def _write_complete_junit(path, *, skipped_test=None):
    root = ET.Element("testsuite")
    for group in _REQUIRED_ADAPTER_TESTS.values():
        for test_name, count in group.items():
            for index in range(count):
                name = test_name if count == 1 else f"{test_name}[case-{index}]"
                testcase = ET.SubElement(
                    root, "testcase", {"classname": "adapter.contract", "name": name}
                )
                if test_name == skipped_test and index == 0:
                    ET.SubElement(
                        testcase,
                        "skipped",
                        {"message": "required adapter contract was skipped"},
                    )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _windows_artifact(*, include_dry_runs=True):
    checks = [{"id": f"mock-check-{index}", "status": "pass"} for index in range(9)]
    if include_dry_runs:
        checks.extend(
            [
                {"id": "component-installer-dry-run", "status": "pass"},
                {"id": "source-bootstrap-dry-run", "status": "pass"},
            ]
        )
    return {
        "schema": "mountfile.platform-adapter-evidence/v1",
        "verdict": "pass",
        "checks": checks,
        "check_count": len(checks),
        "files": [
            {"path": f"installer-{index}.ps1", "sha256": "a" * 64}
            for index in range(3)
        ],
        "system_changes_performed": False,
    }


def test_adapter_evidence_passes_only_with_all_required_junit_cases_and_windows_checks(
    tmp_path,
):
    junit = tmp_path / "test-results.xml"
    windows = tmp_path / "windows-evidence.json"
    _write_complete_junit(junit)
    windows.write_text(json.dumps(_windows_artifact()), encoding="utf-8")

    report = _platform_adapter_evidence(
        junit_path=junit,
        windows_evidence_path=windows,
        runner_platform="windows",
    )

    assert report["status"] == "pass"
    assert report["macos_read_only_adapter_contract"]["status"] == "pass"
    assert report["macos_read_only_adapter_contract"]["real_hfs_apfs_read"] == (
        "not_claimed"
    )
    assert report["windows_installer_adapter"]["status"] == "pass"


def test_adapter_evidence_blocks_a_skipped_required_mac_probe(tmp_path):
    junit = tmp_path / "test-results.xml"
    _write_complete_junit(junit, skipped_test="test_apfs_constructor_never_mounts")

    report = _platform_adapter_evidence(
        junit_path=junit,
        windows_evidence_path=None,
        runner_platform="linux",
    )

    assert report["status"] == "blocked"
    assert "skipped:test_apfs_constructor_never_mounts" in report["blockers"]


def test_windows_adapter_evidence_requires_both_installer_dry_runs(tmp_path):
    junit = tmp_path / "test-results.xml"
    windows = tmp_path / "windows-evidence.json"
    _write_complete_junit(junit)
    windows.write_text(
        json.dumps(_windows_artifact(include_dry_runs=False)), encoding="utf-8"
    )

    report = _platform_adapter_evidence(
        junit_path=junit,
        windows_evidence_path=windows,
        runner_platform="windows",
    )

    assert report["status"] == "blocked"
    assert report["windows_installer_adapter"]["status"] == "blocked"
    assert "windows-installer-evidence-invalid" in report["blockers"]
