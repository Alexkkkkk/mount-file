#!/usr/bin/env python3
"""Execute recovery fixtures and report evidence without upgrading claims."""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.recovery_matrix import build_report as build_recovery_report

FIXTURE_GROUPS = {
    "ext2": (
        "test_native_ext2_recovers_after_process_dies_mid_commit",
        "test_native_ext2_rolls_back_commit_if_process_dies_before_marker_cleanup",
        "test_ext2_cancellation_leaves_original_and_repeated_recovery_is_idempotent",
    ),
    "fat32": (
        "test_failed_fat_rollback_quarantines_image_and_retains_snapshot",
        "test_fat32_short_write_and_cancellation_restore_selected_extent",
    ),
    "exfat": ("test_exfat_short_write_exception_and_cancellation_never_publish",),
}


def _fixture_nodes(tests_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(tests_root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.setdefault(node.name, f"{path}::{node.name}")
    return result


def _junit_results(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    root = ET.parse(path).getroot()
    result: dict[str, dict[str, object]] = {}
    for case in root.iter("testcase"):
        name = case.attrib.get("name", "").split("[", 1)[0]
        status = "passed"
        detail = ""
        if case.find("failure") is not None:
            status = "failed"
            detail = (case.findtext("failure") or "")[-1000:]
        elif case.find("error") is not None:
            status = "error"
            detail = (case.findtext("error") or "")[-1000:]
        elif case.find("skipped") is not None:
            status = "skipped"
            detail = case.find("skipped").attrib.get("message", "")
        result[name] = {
            "status": status,
            "detail": detail,
            "duration_seconds": float(case.attrib.get("time", "0") or 0),
        }
    return result


def build_execution_report(
    repo_root: Path | None = None, *, junit_path: Path | None = None
) -> tuple[dict[str, object], int]:
    root = repo_root or _REPO_ROOT
    tests_root = root / "tests"
    nodes = _fixture_nodes(tests_root)
    junit = junit_path or root / "recovery-acceptance-junit.xml"
    missing = [
        name for names in FIXTURE_GROUPS.values() for name in names if name not in nodes
    ]
    nodeids = [
        nodes[name]
        for names in FIXTURE_GROUPS.values()
        for name in names
        if name in nodes
    ]
    command = [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}", *nodeids]
    started = time.monotonic()
    completed = subprocess.run(command, cwd=root, check=False)
    results = _junit_results(junit)
    entries = []
    blockers = list(f"missing:{name}" for name in missing)
    for group, names in FIXTURE_GROUPS.items():
        group_results = [results[name] for name in names if name in results]
        if not group_results:
            status = "missing"
        elif any(item["status"] in {"failed", "error"} for item in group_results):
            status = "failed"
        elif any(item["status"] == "skipped" for item in group_results):
            status = "skipped"
        else:
            status = "passed"
        if status != "passed":
            blockers.append(group)
        entries.append(
            {
                "profile_group": group,
                "fixture_tests": list(names),
                "fixture_status": status,
                "results": group_results,
                "evidence": "executed_recovery_fixture_results",
                "production_ready": False,
                "power_loss": "unsupported",
            }
        )
    contract = build_recovery_report()
    report = {
        "schema": 1,
        "report": "mountfile-recovery-execution-acceptance",
        "command": command,
        "elapsed_seconds": time.monotonic() - started,
        "pytest_exit_code": completed.returncode,
        "entries": entries,
        "contract_production_ready": contract["production_ready"],
        "contract_blocked_profiles": [
            item["profile_id"]
            for item in contract["entries"]
            if item["verdict"] == "blocked"
        ],
        "production_ready": False,
        "power_loss": "unsupported",
        "parity": "not_claimed",
        "verdict": "pass" if not blockers and completed.returncode == 0 else "blocked",
        "blockers": blockers,
    }
    return report, (0 if report["verdict"] == "pass" else 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--junitxml", type=Path)
    args = parser.parse_args()
    report, code = build_execution_report(junit_path=args.junitxml)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
