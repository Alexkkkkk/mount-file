#!/usr/bin/env python3
"""Build a portable, content-addressed native read-path evidence report."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.capabilities import get_native_capability_matrix
from mountfile.native_parity import run_parity_matrix
from scripts.checker_matrix import build_report as build_checker_inventory
from tests.native_parity_fixtures import build_native_reader_cases

_READ_OPERATIONS = ("list", "stat", "read-file", "read-range", "extract")
_REQUIRED_ADAPTER_TESTS = {
    "macos": {
        "test_detect_macos_filesystem_signatures": 4,
        "test_detection_honors_filesystem_offset": 1,
        "test_vfs_does_not_silently_fallback_for_macos_filesystems": 2,
        "test_hfs_requires_optional_pytsk3": 1,
        "test_apfs_missing_tool_is_explicit_and_does_not_mount": 1,
        "test_apfs_constructor_never_mounts": 1,
        "test_apfs_status_is_read_only_and_unmounted_without_side_effects": 1,
        "test_apfs_status_tracks_owned_mount_process_and_close": 1,
    },
    "windows_installer_contract": {
        "test_packaged_installer_matches_source": 1,
        "test_windows_installers_are_shipped_and_verified": 1,
        "test_launcher_uses_venv_and_first_run_bootstrap": 1,
    },
}


def _junit_statuses(junit_path: Path) -> dict[str, list[dict[str, str]]]:
    """Read exact pytest testcase outcomes; parameterized cases stay distinct."""
    root = ET.parse(junit_path).getroot()
    results: dict[str, list[dict[str, str]]] = {}
    for testcase in root.iter("testcase"):
        raw_name = testcase.attrib.get("name", "")
        name = raw_name.split("[", 1)[0]
        if testcase.find("failure") is not None:
            status = "failed"
            detail = (testcase.findtext("failure") or "")[-1000:]
        elif testcase.find("error") is not None:
            status = "error"
            detail = (testcase.findtext("error") or "")[-1000:]
        elif testcase.find("skipped") is not None:
            status = "skipped"
            detail = testcase.find("skipped").attrib.get("message", "")
        else:
            status = "pass"
            detail = ""
        results.setdefault(name, []).append(
            {
                "status": status,
                "detail": detail,
                "classname": testcase.attrib.get("classname", ""),
            }
        )
    return results


def _adapter_group_report(
    required: dict[str, int], results: dict[str, list[dict[str, str]]]
) -> dict[str, object]:
    probes = []
    blockers = []
    for test_name, expected_count in required.items():
        observed = results.get(test_name, [])
        statuses = [item["status"] for item in observed]
        if len(observed) < expected_count:
            status = "missing"
            blockers.append(f"missing:{test_name}:{expected_count}")
        elif any(value in {"failed", "error"} for value in statuses):
            status = "failed"
            blockers.append(f"failed:{test_name}")
        elif any(value == "skipped" for value in statuses):
            status = "unavailable"
            blockers.append(f"skipped:{test_name}")
        else:
            status = "pass"
        probes.append(
            {
                "test": test_name,
                "expected_cases": expected_count,
                "observed_cases": len(observed),
                "case_statuses": statuses,
                "status": status,
            }
        )
    return {
        "status": "pass" if not blockers else "blocked",
        "probes": probes,
        "blockers": blockers,
    }


def _platform_adapter_evidence(
    *,
    junit_path: Path | None,
    windows_evidence_path: Path | None,
    runner_platform: str,
) -> dict[str, object]:
    if junit_path is None or not junit_path.is_file():
        return {
            "status": "not_collected",
            "blockers": ["pytest JUnit report was not provided"],
            "macos_read_only_adapter_contract": {"status": "not_collected"},
            "windows_installer_adapter": {"status": "not_collected"},
        }
    try:
        results = _junit_statuses(junit_path)
    except (OSError, ET.ParseError) as exc:
        return {
            "status": "blocked",
            "blockers": [f"invalid JUnit report: {type(exc).__name__}"],
            "macos_read_only_adapter_contract": {"status": "blocked"},
            "windows_installer_adapter": {"status": "blocked"},
        }

    macos_group = _adapter_group_report(_REQUIRED_ADAPTER_TESTS["macos"], results)
    windows_contract = _adapter_group_report(
        _REQUIRED_ADAPTER_TESTS["windows_installer_contract"], results
    )
    blockers = list(macos_group["blockers"]) + list(windows_contract["blockers"])

    if runner_platform == "windows":
        if windows_evidence_path is None or not windows_evidence_path.is_file():
            windows_actual: dict[str, object] = {
                "status": "missing",
                "blockers": ["Windows PowerShell adapter artifact is missing"],
            }
        else:
            try:
                loaded = json.loads(
                    windows_evidence_path.read_text(encoding="utf-8-sig")
                )
                if not isinstance(loaded, dict):
                    raise ValueError("Windows adapter report must be a JSON object")
                windows_actual = loaded
                checks = windows_actual.get("checks", [])
                required_check_ids = {
                    "component-installer-dry-run",
                    "source-bootstrap-dry-run",
                }
                observed_check_ids = (
                    {item.get("id") for item in checks if isinstance(item, dict)}
                    if isinstance(checks, list)
                    else set()
                )
                files = windows_actual.get("files", [])
                valid_hashes = (
                    isinstance(files, list)
                    and len(files) >= 3
                    and all(
                        isinstance(item, dict)
                        and isinstance(item.get("sha256"), str)
                        and len(item["sha256"]) == 64
                        and set(item["sha256"].lower()) <= set("0123456789abcdef")
                        for item in files
                    )
                )
                valid = (
                    windows_actual.get("schema")
                    == "mountfile.platform-adapter-evidence/v1"
                    and windows_actual.get("verdict") == "pass"
                    and isinstance(checks, list)
                    and len(checks) >= 11
                    and windows_actual.get("check_count") == len(checks)
                    and all(
                        isinstance(item, dict) and item.get("status") == "pass"
                        for item in checks
                    )
                    and required_check_ids.issubset(observed_check_ids)
                    and valid_hashes
                    and windows_actual.get("system_changes_performed") is False
                )
                if not valid:
                    windows_actual["status"] = "blocked"
                    blockers.append("windows-installer-evidence-invalid")
                else:
                    windows_actual["status"] = "pass"
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                windows_actual = {
                    "status": "blocked",
                    "blockers": [
                        f"invalid Windows adapter artifact: {type(exc).__name__}"
                    ],
                }
                blockers.append("windows-installer-evidence-invalid")
        if windows_actual["status"] != "pass":
            blockers.extend(windows_actual.get("blockers", []))
    else:
        windows_actual = {
            "status": "not_applicable",
            "reason": "Windows installer smoke is executed only on Windows runners",
        }

    return {
        "status": "pass" if not blockers else "blocked",
        "macos_read_only_adapter_contract": {
            **macos_group,
            "scope": "mocked signature, no-fallback, read-only and lifecycle contracts",
            "real_hfs_apfs_read": "not_claimed",
        },
        "windows_installer_contract": windows_contract,
        "windows_installer_adapter": windows_actual,
        "blockers": blockers,
    }


def build_evidence_report(
    *,
    junit_path: Path | None = None,
    windows_evidence_path: Path | None = None,
) -> dict[str, object]:
    """Generate real/synthetic fixtures and run all portable VFS read checks."""
    with tempfile.TemporaryDirectory(prefix="mountfile-native-read-evidence-") as tmp:
        cases = build_native_reader_cases(Path(tmp))
        parity = run_parity_matrix(cases)

    case_by_profile = {str(item["profile_id"]): item for item in parity.cases}
    matrix = get_native_capability_matrix()
    profiles = []
    for profile in matrix["profiles"]:
        if profile["interface"] != "vfs":
            continue
        profile_id = profile["profile_id"]
        case = case_by_profile.get(profile_id)
        operations = {}
        for operation in _READ_OPERATIONS:
            decision = profile["operations"].get(operation)
            if decision is None:
                operations[operation] = {
                    "status": "not_claimed",
                    "reason": "operation is not declared by this profile",
                }
                continue
            if decision["state"] != "available":
                operations[operation] = {
                    "status": "not_claimed",
                    "reason": "operation is not available in this profile",
                }
                continue
            if operation not in _READ_OPERATIONS:
                operations[operation] = {
                    "status": "not_claimed",
                    "reason": "this artifact covers read-only VFS operations only",
                }
                continue
            native_operation_status = (
                case["operations"].get(operation) if case is not None else None
            )
            operations[operation] = {
                "status": native_operation_status or "not_claimed",
                "fixture_case": case["case"] if case is not None else None,
            }
        profiles.append(
            {
                "profile_id": profile_id,
                "filesystem": profile["filesystem"],
                "backend": profile["backend"],
                "execution_status": (
                    case["native_status"] if case is not None else "not_claimed"
                ),
                "fixture": case["fixture"] if case is not None else None,
                "backend_observed": case["backend"] if case is not None else None,
                "operations": operations,
                "independent_oracle": (
                    case["independent_oracle"]
                    if case is not None
                    else {
                        "status": "not_claimed",
                        "reason": "no portable fixture was declared for this profile",
                    }
                ),
                "failure": case.get("failure") if case is not None else None,
            }
        )

    expected_profiles = {
        profile["profile_id"]
        for profile in matrix["profiles"]
        if profile["interface"] == "vfs"
    }
    evidenced_profiles = {
        profile["profile_id"]
        for profile in profiles
        if profile["execution_status"] == "pass"
        and all(
            profile["operations"].get(operation, {}).get("status") == "pass"
            for operation in _READ_OPERATIONS
        )
    }
    overall_pass = parity.ok and evidenced_profiles == expected_profiles
    checker_report = build_checker_inventory()
    adapter_report = _platform_adapter_evidence(
        junit_path=junit_path,
        windows_evidence_path=windows_evidence_path,
        runner_platform=platform.system().lower(),
    )
    gate_pass = overall_pass and adapter_report["status"] == "pass"
    return {
        "schema": "mountfile.native-read-evidence/v1",
        "report": "mountfile-native-read-path-evidence",
        "source_revision": os.environ.get("GITHUB_SHA"),
        "runner": {
            "platform": platform.system().lower(),
            "platform_release": platform.release(),
            "python": platform.python_version(),
        },
        "scope": {
            "interface": "vfs",
            "operations": list(_READ_OPERATIONS),
            "legacy_fallback": "disabled",
            "independent_parity": "not_claimed_by_this_report",
        },
        "summary": {
            "verdict": "pass" if overall_pass else "blocked",
            "cases_run": parity.ran,
            "case_failures": parity.failures,
            "checks": parity.checks,
            "profiles_expected": sorted(expected_profiles),
            "profiles_with_passed_read_contract": sorted(evidenced_profiles),
        },
        "profiles": profiles,
        "external_checker_inventory": checker_report["entries"],
        "platform_adapter_evidence": adapter_report,
        "gate": {"verdict": "pass" if gate_pass else "blocked"},
        "limitations": [
            "A passing native read-contract fixture validates expected content and API behavior, not independent filesystem-format parity.",
            "External checker availability and versions are listed separately; inventory does not imply a checker ran on these fixtures.",
            "Writer-operation evidence is published separately by Linux checker and recovery acceptance artifacts.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--junitxml", type=Path, required=True)
    parser.add_argument("--windows-installer-evidence", type=Path)
    args = parser.parse_args()
    report = build_evidence_report(
        junit_path=args.junitxml,
        windows_evidence_path=args.windows_installer_evidence,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["gate"]["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
