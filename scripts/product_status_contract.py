#!/usr/bin/env python3
"""Validate conservative product-status claims across repository surfaces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.capabilities import (
    CAPABILITY_MATRIX_VERSION,
    get_native_capability_matrix,
)
from scripts.checker_matrix import build_report as build_checker_report
from scripts.recovery_matrix import build_report as build_recovery_report


def _execution_artifact_blockers(
    artifact: object,
    *,
    report_name: str,
    expected_entries: set[str],
    entry_key: str,
    evidence: str,
    report_evidence: str | None = None,
) -> list[str]:
    if not isinstance(artifact, dict):
        return [f"{report_name} execution artifact is not an object"]
    blockers: list[str] = []
    if artifact.get("schema") != 1 or artifact.get("report") != report_name:
        blockers.append(f"{report_name} execution artifact has an invalid identity")
    if artifact.get("verdict") != "pass":
        blockers.append(f"{report_name} execution artifact is not passing")
    if artifact.get("pytest_exit_code") != 0:
        blockers.append(f"{report_name} execution artifact has a nonzero pytest exit")
    if artifact.get("blockers") != []:
        blockers.append(f"{report_name} execution artifact has blockers")
    if report_evidence is not None and artifact.get("evidence") != report_evidence:
        blockers.append(f"{report_name} execution artifact lacks executed evidence")
    entries = artifact.get("entries")
    if not isinstance(entries, list):
        blockers.append(f"{report_name} execution artifact has no entries list")
        return blockers
    actual_entries: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            blockers.append(f"{report_name} execution artifact has an invalid entry")
            continue
        name = entry.get(entry_key)
        if isinstance(name, str):
            actual_entries.append(name)
        if entry.get("fixture_status") != "passed":
            blockers.append(f"{report_name} execution entry is not passed: {name}")
        if entry.get("evidence") != evidence:
            blockers.append(
                f"{report_name} execution entry lacks executed evidence: {name}"
            )
    if (
        len(actual_entries) != len(set(actual_entries))
        or set(actual_entries) != expected_entries
    ):
        blockers.append(f"{report_name} execution artifact coverage is incomplete")
    return blockers


def build_report(repo_root: Path | None = None) -> dict[str, object]:
    root = repo_root or _REPO_ROOT
    status_doc = (root / "docs" / "issue-182-status.md").read_text(encoding="utf-8")
    capability = get_native_capability_matrix()
    recovery = build_recovery_report()
    checker = build_checker_report(root / "tests")
    acceptance_path = root / "checker-acceptance.json"
    acceptance = None
    if acceptance_path.exists():
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    recovery_acceptance_path = root / "recovery-acceptance.json"
    recovery_acceptance = None
    if recovery_acceptance_path.exists():
        recovery_acceptance = json.loads(
            recovery_acceptance_path.read_text(encoding="utf-8")
        )
    blockers: list[str] = []

    if capability["version"] != CAPABILITY_MATRIX_VERSION:
        blockers.append("capability matrix version is internally inconsistent")
    if recovery["capability_matrix_version"] != CAPABILITY_MATRIX_VERSION:
        blockers.append("recovery report uses a stale capability matrix version")
    for entry in checker["entries"]:
        if entry["parity"] != "not_claimed":
            blockers.append(
                f"checker parity claim is not conservative: {entry['profile']}"
            )
        if entry["evidence"] not in {"fixture_required", "fixture_missing"}:
            blockers.append(f"checker evidence is not bounded: {entry['profile']}")
    if recovery["production_ready"] is not False:
        blockers.append("recovery report must remain production_ready=false")
    if acceptance is not None:
        blockers.extend(
            _execution_artifact_blockers(
                acceptance,
                report_name="mountfile-checker-execution-acceptance",
                expected_entries={entry["profile"] for entry in checker["entries"]},
                entry_key="profile",
                evidence="executed_fixture_required",
                report_evidence="executed_fixture_results",
            )
        )
    if recovery_acceptance is not None:
        blockers.extend(
            _execution_artifact_blockers(
                recovery_acceptance,
                report_name="mountfile-recovery-execution-acceptance",
                expected_entries={"fat12-16", "ext2", "fat32", "exfat"},
                entry_key="profile_group",
                evidence="executed_recovery_fixture_results",
            )
        )
    if "power_loss" not in status_doc or "not_claimed" not in status_doc:
        blockers.append("status documentation is missing conservative recovery markers")
    for marker in ("| B1 |", "| D1 |", "| P1 |"):
        if marker not in status_doc:
            blockers.append(f"status documentation is missing {marker} row")

    return {
        "schema": 1,
        "report": "mountfile-product-status-contract",
        "capability_matrix_version": CAPABILITY_MATRIX_VERSION,
        "profile_count": len(capability["profiles"]),
        "recovery_production_ready": recovery["production_ready"],
        "checker_entry_count": len(checker["entries"]),
        "checker_acceptance_verdict": (
            acceptance.get("verdict") if acceptance is not None else "not_run"
        ),
        "recovery_acceptance_verdict": (
            recovery_acceptance.get("verdict")
            if recovery_acceptance is not None
            else "not_run"
        ),
        "verdict": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "claims": {
            "parity": "not_claimed",
            "power_loss": "unsupported",
            "native_ext3_ext4_write": "unsupported",
            "native_ntfs_write": "unsupported",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = build_report()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
