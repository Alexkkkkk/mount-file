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
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Launched as ``python scripts/failure_matrix_artifact.py`` Python puts only
# ``scripts/`` on sys.path; add the repository root before the package import.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.acceptance import checker_row  # noqa: E402

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


def _staged_short_write_cases(profile: str) -> list[dict[str, Any]]:
    """Short-write cells for the staged ext4 and NTFS writers.

    Selectors point at ``tests/test_writer_fault_injection_acceptance.py``: the
    data primitive reports a truncated write, publication is refused, the source
    stays byte-identical and the image remains writable afterwards.
    """

    module = "test_writer_fault_injection_acceptance"
    return [
        _case(
            module,
            "test_short_write_at_the_data_primitive_refuses_publication",
            include=(profile,),
        ),
        _case(
            module,
            "test_short_write_refusal_leaves_image_writable",
            include=(profile,),
        ),
    ]


def _staged_injection_cases(kind: str, profile: str) -> list[dict[str, Any]]:
    """Injected-exception / cancellation cells for staged ext4 and NTFS writers.

    Selectors point at ``tests/test_writer_fault_injection_acceptance.py``, which
    raises at every declared ``FAULT_POINTS`` boundary.  A green cell means those
    cases ran and passed for that profile; it is not checker execution and it is
    not acceptance.
    """

    module = "test_writer_fault_injection_acceptance"
    shared = (profile, kind)
    return [
        _case(
            module,
            "test_injected_failure_at_each_boundary_preserves_source_or_publishes_fully",
            include=shared,
        ),
        _case(
            module,
            "test_refused_write_leaves_image_writable",
            include=shared,
        ),
    ]


# These are test selectors, not declarations of completed acceptance. Parameter
# selectors narrow evidence to a particular FAT width or a real process-kill seam.
TESTS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "fat12": {
        "short_write": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_short_write_rolls_back_and_preserves_sentinels",
                include=("[12]",),
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
        "cancellation": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_cancellation_during_write_preserves_source_and_cleans_stage",
                include=("[12]",),
            )
        ],
    },
    "fat16": {
        "short_write": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_short_write_rolls_back_and_preserves_sentinels",
                include=("[16]",),
            )
        ],
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
        "cancellation": [
            _case(
                "test_fat1216_writer",
                "test_fat1216_cancellation_during_write_preserves_source_and_cleans_stage",
                include=("[16]",),
            )
        ],
    },
    "fat32": {
        "short_write": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_staged_failure_preserves_full_image_hash",
                include=("[short_write]",),
            )
        ],
        "injected_exception": [
            _case(
                "test_native_writer_failure_matrix",
                "test_fat32_staged_failure_preserves_full_image_hash",
                include=("[injected_exception]",),
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
                "test_fat32_staged_failure_preserves_full_image_hash",
                include=("[cancellation]",),
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
        "short_write": _staged_short_write_cases("ext4"),
        "injected_exception": _staged_injection_cases("injected_exception", "ext4"),
        "process_kill": [
            _case(
                "test_ext4_writer_fault_coverage",
                "test_ext4_process_kill_preserves_source_until_atomic_publish",
                exclude=("[helper:",),
            )
        ],
        "cancellation": _staged_injection_cases("cancellation", "ext4"),
    },
    "ntfs": {
        "short_write": _staged_short_write_cases("ntfs"),
        "injected_exception": [
            _case(
                "test_ntfs_replay_executor",
                "test_allocation_phase_failure_preserves_source_and_cleans_stage",
            )
        ]
        + _staged_injection_cases("injected_exception", "ntfs"),
        "process_kill": [
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                exclude=("[helper:", "[ntfs:replay:"),
            ),
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                include=("[ntfs:replay:mft_record:applied]",),
            ),
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                include=("[ntfs:replay:stream_write:applied]",),
            ),
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                include=("[ntfs:replay:restart_pages:applied]",),
            ),
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                include=("[ntfs:replay:volume_marker:applied]",),
            ),
            _case(
                "test_ntfs_writer_fault_coverage",
                "test_ntfs_process_kill_preserves_source_until_atomic_replace",
                include=("[ntfs:replay:mft_mirror:applied]",),
            ),
            _case(
                "test_ntfs_replay",
                "test_native_replay_sigkill_matrix_reaches_each_declared_phase",
            ),
            _case(
                "test_ntfs_replay_executor",
                "test_allocation_phase_sigkill_preserves_source",
            ),
        ],
        "cancellation": _staged_injection_cases("cancellation", "ntfs"),
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
    "fat12": ("write_file",),
    "fat16": ("write_file",),
    "fat32": ("write_file",),
    "ext2": ("write_file",),
    "exfat": ("write_file",),
    "ext4": ("write_file", "create_file"),
    "ntfs": ("write_file",),
}

# Operation-specific selectors must never inherit another operation's cases.
# The create_file phase test checks the SHA-256 and staging cleanup on every
# declared injected-exception point; the other failure states remain not proven
# until their own create_file-specific test selectors are declared.
OPERATION_TESTS: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
    "ext4": {
        "create_file": {
            "injected_exception": [
                _case(
                    "test_native_operation_matrix",
                    "test_ext4_create_file_phase_failure_preserves_the_image",
                )
            ],
        },
    },
}


def _operation_selectors(
    profile: str, operation: str, state: str
) -> list[dict[str, Any]]:
    if operation == "write_file":
        return TESTS.get(profile, {}).get(state, [])
    return OPERATION_TESTS.get(profile, {}).get(operation, {}).get(state, [])


def _collect_seams(repo_root: Path) -> list[str]:
    seams: set[str] = set()
    for source in (
        repo_root / "mountfile" / "native_write.py",
        repo_root / "mountfile" / "ntfs_replay.py",
    ):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        seams.update(SEAM_RE.findall(text))
    return sorted(seams)


def _run_provenance() -> dict[str, Any]:
    """Record whether this artifact came from the required scheduled trigger.

    A manual ``workflow_dispatch`` run is useful diagnostic evidence, but it
    must never be mistaken for one of the consecutive nightly runs required
    by the acceptance tracker.  Missing GitHub metadata is typed as unknown
    rather than being treated as a scheduled run.
    """
    event = os.environ.get("GITHUB_EVENT_NAME") or None
    run_id = os.environ.get("GITHUB_RUN_ID") or None
    sha = os.environ.get("GITHUB_SHA") or None
    return {
        "event": event,
        "run_id": run_id,
        "sha": sha,
        "schedule_eligible": event == "schedule",
    }


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
    return not any(fragment in node_id for fragment in selector.get("exclude", []))


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
        selector
        for selector, group in zip(selectors, matched_by_selector, strict=True)
        if not group
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
    for profile, operations in PROFILE_OPERATIONS.items():
        for operation in operations:
            for state in STATES:
                selectors = _operation_selectors(profile, operation, state)
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
    c6_row = checker_row.build_row(
        captured_at=os.environ.get("MOUNTFILE_CAPTURED_AT", "unavailable")
    )
    c6_row["test_evidence"] = checker_row.select_cases(
        checker_row.C6_DECLARED_TEST_IDS, cases
    )
    return {
        "schema": SCHEMA,
        "verdict": overall,
        "acceptance": "not_claimed",
        "production_ready": False,
        "run_provenance": _run_provenance(),
        "fault_seams": _collect_seams(root),
        "junit_report": junit_summary,
        "test_status_counts": summary,
        "cells": cells,
        "c6_checker_row": c6_row,
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
    expected_cell_count = sum(
        len(operations) for operations in PROFILE_OPERATIONS.values()
    ) * len(STATES)
    if not isinstance(report.get("cells"), list) or len(report["cells"]) != (
        expected_cell_count
    ):
        blockers.append("cells")
    try:
        expected = build_report(root, junitxml)
    except (OSError, ET.ParseError, ValueError):
        expected = None
        blockers.append("junit_unreadable")
    if expected is not None and report != expected:
        blockers.append("report_does_not_match_junit_and_declared_selectors")
    row = report.get("c6_checker_row")
    if not isinstance(row, dict):
        blockers.append("c6_checker_row_missing")
        c6 = {"present": False}
    else:
        evidence = row.get("test_evidence") or {}
        c6 = {
            "present": True,
            "outcome": row.get("outcome"),
            "checker_state": row.get("checker_state"),
            "authoritative": row.get("authoritative"),
            "exit_code": row.get("exit_code"),
            "test_status": evidence.get("status"),
            "declared_test_ids": len(checker_row.C6_DECLARED_TEST_IDS),
        }
    return {
        "schema": VALIDATION_SCHEMA,
        "verdict": "pass" if not blockers else "blocked",
        "acceptance": "not_evaluated",
        "blockers": sorted(set(blockers)),
        "c6_checker_row": c6,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument(
        "--c6-checker-state",
        default="auto",
        choices=("auto", *checker_row.CHECKER_STATES),
        help="Force the C6 authoritative-checker state and exit with its code.",
    )
    parser.add_argument(
        "--inject-unknown",
        action="store_true",
        help="Emit a C6 row whose difference cannot be attributed (must fail).",
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.c6_checker_state != "auto" or args.inject_unknown:
        # ``--inject-unknown`` must reach the parity classifier, so it forces an
        # available checker: on a host without chkdsk the row would otherwise
        # stop at ``unavailable`` and never exercise the unknown branch.
        forced_state = (
            "available"
            if args.inject_unknown and args.c6_checker_state == "auto"
            else args.c6_checker_state
        )
        c6_only = checker_row.build_row(
            checker_state=forced_state,
            differences={"mft": True} if args.inject_unknown else {"mft": False},
            unattributed=(
                ("mft:resident_attribute_flags",) if args.inject_unknown else ()
            ),
            captured_at=os.environ.get("MOUNTFILE_CAPTURED_AT", "unavailable"),
        )
        print(
            json.dumps(c6_only, ensure_ascii=False, indent=2, sort_keys=True),
            end="",
        )
        return checker_row.exit_code(c6_only)
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
