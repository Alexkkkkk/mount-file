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


def _surface_contract(root: Path) -> tuple[dict[str, bool], list[str]]:
    """Fail closed when docs or UI stop reflecting the conservative matrix."""
    blockers: list[str] = []
    readme_path = root / "README.md"
    changelog_path = root / "CHANGELOG.md"
    cli_path = root / "mountfile" / "cli.py"
    gui_path = root / "mountfile" / "mainwindow.py"

    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    rows: dict[str, list[str]] = {}
    for line in readme.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0] in {"ext3/ext4", "NTFS"}:
            rows[cells[0]] = cells
    readme_safe = all(
        family in rows
        and rows[family][1].lower() == "read-only"
        and rows[family][2].lower() == "no"
        for family in ("ext3/ext4", "NTFS")
    )
    readme_lower = " ".join(readme.lower().split())
    readme_safe = readme_safe and (
        "native ext3/ext4 and ntfs writes are unsupported" in readme_lower
        and "power-loss safety is unsupported" in readme_lower
    )
    if not readme_safe:
        blockers.append("README advertises an unsupported native write profile")

    changelog = (
        changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    )
    unreleased = (
        changelog.split("## Unreleased", 1)[1].split("\n## ", 1)[0]
        if "## Unreleased" in changelog
        else ""
    )
    unreleased_lower = " ".join(unreleased.lower().split())
    changelog_safe = (
        "native ext3/ext4/ntfs writes remain unsupported" in unreleased_lower
        and "power-loss safety is unsupported" in unreleased_lower
    )
    if not changelog_safe:
        blockers.append(
            "Unreleased changelog lacks conservative writer/recovery status"
        )

    cli = cli_path.read_text(encoding="utf-8") if cli_path.exists() else ""
    cli_start = cli.find("def cmd_capabilities")
    cli_end = cli.find("\ndef ", cli_start + 1) if cli_start >= 0 else -1
    cli_command = cli[cli_start : cli_end if cli_end >= 0 else None]
    cli_safe = (
        "get_native_capability_matrix()" in cli_command
        and "json.dumps(matrix" in cli_command
        and '"capabilities"' in cli
    )
    if not cli_safe:
        blockers.append("CLI capability output is not wired to the canonical matrix")

    gui = gui_path.read_text(encoding="utf-8") if gui_path.exists() else ""
    gui_start = gui.find("def _set_write_enabled")
    gui_end = gui.find("\n    def ", gui_start + 1) if gui_start >= 0 else -1
    gui_method = gui[gui_start : gui_end if gui_end >= 0 else None]
    gui_safe = (
        "self.writer.capability_status()" in gui_method
        and "operations.get(canonical" in gui_method
        and 'state == "available"' in gui_method
    )
    if not gui_safe:
        blockers.append("GUI write controls are not driven by capability status")

    return (
        {
            "readme_native_write_table": readme_safe,
            "unreleased_conservative_status": changelog_safe,
            "cli_canonical_capabilities": cli_safe,
            "gui_capability_driven_write_controls": gui_safe,
        },
        blockers,
    )


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
    unsafe_writable_profiles = [
        profile["profile_id"]
        for profile in capability["profiles"]
        if profile["filesystem"] in {"ext3", "ext4", "NTFS"}
        and profile["mode"] == "read-write"
    ]
    if unsafe_writable_profiles:
        blockers.append(
            "unsupported native writer profiles are enabled: "
            + ", ".join(unsafe_writable_profiles)
        )
    surface_contract, surface_blockers = _surface_contract(root)
    blockers.extend(surface_blockers)
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
        "surface_contract": surface_contract,
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
