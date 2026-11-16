# SPDX-License-Identifier: MIT
"""Emit test-execution evidence for the writer failure matrix.

A cell is considered exercised only when its declared pytest node IDs occur in
JUnit XML and those cases passed.  Test execution is not checker execution,
filesystem parity, or product acceptance; each is reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA = "mountfile.failure-matrix/v2"
VALIDATION_SCHEMA = "mountfile.failure-matrix-validation/v1"
SEAM_RE = re.compile(r'_fault\(\s*"([^"]+)"')
STATES = ("short_write", "injected_exception", "process_kill", "cancellation")


def _case(
    module: str,
    name: str,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "module": module,
        "name": name,
        "include": list(include),
        "exclude": list(exclude),
    }


# These are test selectors, not declarations of completed acceptance. Parameter
# selectors narrow evidence to a particular FAT width or a real process-kill seam.
TESTS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "fat12": {
        "short_write": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_short_write_rolls_back_and_preserves_sentinels",
            )
        ],
        "injected_exception": [
            _case(
                "test_fat1216_writer",
                "test_real_fat1216_failed_write_rolls_back_and_checker_stays_clean",
                include=("[12-1474560]",),
            )
        ],
        "process_kill": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_process_kill_before_atomic_replace_never_publishes",
                include=("[12]",),
            )
        ],
    },
    "fat16": {
        "injected_exception": [
            _case(
                "test_fat1216_writer",
                "test_real_fat1216_failed_write_rolls_back_and_checker_stays_clean",
                include=("[16-33554432]",),
            )
        ],
        "process_kill": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_process_kill_before_atomic_replace_never_publishes",
                include=("[16]",),
            )
        ],
    },
    "fat32": {
        "short_write": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_short_write_and_cancellation_restore_selected_extent",
            )
        ],
        "injected_exception": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_injected_exception_and_external_suffix_change_are_bounded",
            )
        ],
        "process_kill": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_process_kill_before_atomic_replace_never_publishes",
            )
        ],
        "cancellation": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_short_write_and_cancellation_restore_selected_extent",
            )
        ],
    },
    "ext2": {
        "short_write": [
            _case(
                "test_ext2_native",
                "test_native_ext2_short_rollback_write_is_not_claimed_as_restored",
            )
        ],
        "injected_exception": [
            _case(
                "test_ext2_native",
                "test_native_ext2_staging_failure_does_not_modify_original",
            )
        ],
        "process_kill": [
            _case(
                "test_ext2_native",
                "test_native_ext2_recovers_after_process_dies_mid_commit",
            ),
            _case(
                "test_ext2_native",
                "test_native_ext2_rolls_back_commit_if_process_dies_before_marker_cleanup",
            ),
        ],
        "cancellation": [
            _case(
                "test_native_writer_failure_matrix",
                "test_ext2_cancellation_leaves_original_and_repeated_recovery_is_idempotent",
            )
        ],
    },
    "exfat": {
        "short_write": [
            _case(
                "test_native_writer_failure_matrix",
                "test_exfat_short_write_exception_and_cancellation_never_publish",
            )
        ],
        "injected_exception": [
            _case(
                "test_native_writer_failure_matrix",
                "test_exfat_short_write_exception_and_cancellation_never_publish",
            )
        ],
        "process_kill": [
            _case(
                "test_native_writer_failure_matrix",
                "test_exfat_process_kill_before_atomic_replace_never_publishes",
            )
        ],
        "cancellation": [
            _case(
                "test_native_writer_failure_matrix",
                "test_exfat_short_write_exception_and_cancellation_never_publish",
            )
        ],
    },
    "ext4": {
        "process_kill": [
            _case(
                "test_ext4_writer_fault_coverage",
                "test_ext4_process_kill_preserves_source_until_atomic_publish",
                exclude=("[helper:",),
            )
        ],
    },
    "ntfs": {
        "process_kill": [
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_publish",
                exclude=("[helper:",),
            )
        ],
    },
}

CHECKERS: dict[str, dict[str, Any]] = {
    "fat12": {
        "name": "fsck.fat",
        "authority": "independent_checker",
        "tests": [
            _case(
                "test_fat1216_writer",
                "test_real_fat1216_failed_write_rolls_back_and_checker_stays_clean",
                include=("[12-1474560]",),
            )
        ],
    },
    "fat16": {
        "name": "fsck.fat",
        "authority": "independent_checker",
        "tests": [
            _case(
                "test_fat1216_writer",
                "test_real_fat1216_failed_write_rolls_back_and_checker_stays_clean",
                include=("[16-33554432]",),
            )
        ],
    },
    "fat32": {
        "name": "fsck.fat",
        "authority": "not_exercised_by_mapped_case",
        "tests": [],
    },
    "ext2": {
        "name": "e2fsck",
        "authority": "independent_checker",
        "tests": [
            _case(
                "test_ext2_native",
                "test_native_ext2_recovers_after_process_dies_mid_commit",
            )
        ],
    },
    "exfat": {
        "name": "fsck.exfat",
        "authority": "not_exercised_by_mapped_case",
        "tests": [],
    },
    "ext4": {
        "name": "e2fsck -fn",
        "authority": "independent_checker",
        "tests": [
            _case(
                "test_ext4_writer_fault_coverage",
                "test_ext4_process_kill_preserves_source_until_atomic_publish",
                include=("[publish:done]",),
            )
        ],
    },
    "ntfs": {
        "name": "ntfsfix -n",
        "authority": "secondary_only_not_chkdsk_parity",
        "tests": [
            _case(
                "test_ntfs_crash_oracle_matrix",
                "test_ntfs_crash_oracle_matrix",
                include=(f"[{scenario}]",),
            )
            for scenario in (
                "committed-resident",
                "committed-nonresident",
                "active-transaction",
                "active-nonresident",
                "dirty-bit-only",
            )
        ],
    },
}

PROFILE_OPERATIONS = {
    "fat12": "write_file",
    "fat16": "write_file",
    "fat32": "write_file",
    "ext2": "write_file",
    "exfat": "write_file",
    "ext4": "write_file",
    "ntfs": "write_file",
}


def _collect_seams(native_write_path: Path) -> list[str]:
    try:
        text = native_write_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(set(SEAM_RE.findall(text)))


def _load_junit(
    path: Path | None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    if path is None or not path.is_file():
        return [], None
    raw = path.read_bytes()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        return [], {
            "file": path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": "unavailable",
            "error": str(error)[:200],
            "testcases": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
        }
    cases: list[dict[str, str]] = []
    for element in root.iter("testcase"):
        classname = element.attrib.get("classname", "")
        name = element.attrib.get("name", "")
        node_id = f"{classname}::{name}" if classname else name
        if element.find("failure") is not None or element.find("error") is not None:
            status = "failed"
        elif element.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        cases.append(
            {
                "node_id": node_id,
                "module": classname.rsplit(".", 1)[-1],
                "name": name,
                "status": status,
            }
        )
    summary = {
        "file": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "available",
        "testcases": len(cases),
        "passed": sum(case["status"] == "passed" for case in cases),
        "failed": sum(case["status"] == "failed" for case in cases),
        "skipped": sum(case["status"] == "skipped" for case in cases),
    }
    return cases, summary


def _matches(case: dict[str, str], selector: dict[str, Any]) -> bool:
    if case["module"] != selector["module"]:
        return False
    name = case["name"]
    prefix = selector["name"]
    if name != prefix and not name.startswith(prefix + "["):
        return False
    node_id = case["node_id"]
    if not all(fragment in node_id for fragment in selector.get("include", [])):
        return False
    if any(fragment in node_id for fragment in selector.get("exclude", [])):
        return False
    return True


def _evaluate(
    selectors: list[dict[str, Any]], cases: list[dict[str, str]], junit_status: str
) -> dict[str, Any]:
    if junit_status == "not_run":
        return {"status": "not_run", "evidence": [], "missing_selectors": selectors}
    if junit_status != "available":
        return {"status": "unavailable", "evidence": [], "missing_selectors": selectors}
    if not selectors:
        return {"status": "not_proven", "evidence": [], "missing_selectors": []}
    matched_by_selector = [
        [case for case in cases if _matches(case, selector)] for selector in selectors
    ]
    matched = [case for group in matched_by_selector for case in group]
    missing = [
        selector for selector, group in zip(selectors, matched_by_selector) if not group
    ]
    evidence = sorted({case["node_id"] for case in matched})
    statuses = [case["status"] for case in matched]
    if "failed" in statuses:
        status = "failed"
    elif "skipped" in statuses:
        status = (
            "unavailable"
            if not any(value == "passed" for value in statuses)
            else "partial"
        )
    elif missing:
        status = "partial" if matched else "not_proven"
    else:
        status = "passed"
    return {"status": status, "evidence": evidence, "missing_selectors": missing}


def build_report(root: Path, junitxml: Path | None = None) -> dict[str, Any]:
    cases, junit_summary = _load_junit(junitxml)
    junit_status = junit_summary["status"] if junit_summary else "not_run"
    cells: list[dict[str, Any]] = []
    for profile, operation in PROFILE_OPERATIONS.items():
        for state in STATES:
            selectors = TESTS.get(profile, {}).get(state, [])
            execution = _evaluate(selectors, cases, junit_status)
            cells.append(
                {
                    "profile": profile,
                    "operation": operation,
                    "state": state,
                    "test_status": execution["status"],
                    "test_evidence": execution["evidence"],
                    "expected_test_selectors": selectors,
                    "missing_test_selectors": execution["missing_selectors"],
                    "checker_status": "not_claimed_per_cell",
                    "acceptance_status": "not_accepted",
                }
            )
    checker_evidence: list[dict[str, Any]] = []
    for profile, spec in CHECKERS.items():
        result = _evaluate(spec["tests"], cases, junit_status)
        status = result["status"]
        checker_evidence.append(
            {
                "profile": profile,
                "checker": spec["name"],
                "authority": spec["authority"],
                "status": status,
                "evidence": result["evidence"],
                "missing_test_selectors": result["missing_selectors"],
            }
        )
    summary = {
        status: sum(cell["test_status"] == status for cell in cells)
        for status in (
            "passed",
            "failed",
            "partial",
            "unavailable",
            "not_proven",
            "not_run",
        )
    }
    overall = (
        "tests_failed"
        if summary["failed"] or (junit_summary and junit_summary["failed"])
        else (
            "execution_recorded"
            if junit_status == "available"
            else "junit_unavailable" if junit_status == "unavailable" else "not_run"
        )
    )
    return {
        "schema": SCHEMA,
        "verdict": overall,
        "acceptance": "not_claimed",
        "production_ready": False,
        "fault_seams": _collect_seams(root / "mountfile" / "native_write.py"),
        "junit_report": junit_summary,
        "test_status_counts": summary,
        "cells": cells,
        "checker_evidence": checker_evidence,
        "limitations": [
            "A passing pytest case records test execution only; it does not establish complete operation-by-phase coverage.",
            "Checker evidence is separate from test evidence and is limited to the exact JUnit cases listed.",
            "ntfsfix -n is secondary evidence only and is not Windows/chkdsk parity.",
            "This artifact does not establish product-level ext4 or NTFS write support.",
        ],
    }


def validate_report(
    report: dict[str, Any], root: Path, junitxml: Path | None = None
) -> dict[str, Any]:
    blockers: list[str] = []
    if report.get("schema") != SCHEMA:
        blockers.append("schema")
    if not isinstance(report.get("cells"), list) or len(report["cells"]) != len(
        PROFILE_OPERATIONS
    ) * len(STATES):
        blockers.append("cells")
    try:
        expected = build_report(root, junitxml)
    except (OSError, ET.ParseError, ValueError):
        expected = None
        blockers.append("junit_unreadable")
    if expected is not None and report != expected:
        blockers.append("report_does_not_match_junit_and_declared_selectors")
    return {
        "schema": VALIDATION_SCHEMA,
        "verdict": "pass" if not blockers else "blocked",
        "acceptance": "not_evaluated",
        "blockers": sorted(set(blockers)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.validate:
        report = json.loads(args.validate.read_text(encoding="utf-8"))
        result = validate_report(report, args.repo_root, args.junitxml)
    else:
        result = build_report(args.repo_root, args.junitxml)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if result.get("verdict") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
