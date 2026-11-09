#!/usr/bin/env python3
"""Execute declared checker fixtures and emit bounded acceptance evidence."""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.checker_matrix import MATRIX, _tool_version, build_report


def _fixture_nodes(tests_root: Path) -> dict[str, str]:
    nodes: dict[str, str] = {}
    for path in sorted(tests_root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                nodes.setdefault(node.name, f"{path}::{node.name}")
    return nodes


def _junit_results(path: Path) -> dict[str, list[dict[str, object]]]:
    if not path.exists():
        return {}
    root = ET.parse(path).getroot()
    results: dict[str, list[dict[str, object]]] = {}
    for case in root.iter("testcase"):
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        key = name.split("[", 1)[0]
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
        properties = {
            prop.attrib.get("name", ""): prop.attrib.get("value", "")
            for prop in case.findall("./properties/property")
        }
        results.setdefault(key, []).append(
            {
                "case_name": name,
                "status": status,
                "detail": detail,
                "classname": classname,
                "duration_seconds": float(case.attrib.get("time", "0") or 0),
                "properties": properties,
            }
        )
    return results


def _fixture_evidence(
    profile: str,
    checker: str,
    results: list[dict[str, object]],
) -> tuple[list[dict[str, object]], str]:
    """Validate per-case image/copy hashes carried by the pytest JUnit report."""
    required_profiles = (
        {"fat12-native-rw", "fat16-native-rw"}
        if profile == "fat12-16-native-rw"
        else {profile}
    )
    if profile == "ntfs-native-ro":
        expected_checker_verdict = "inconclusive"
    elif checker == "pytsk3":
        expected_checker_verdict = "pass"
    else:
        expected_checker_verdict = "clean"
    images = []
    valid = True
    observed_profiles = set()
    for result in results:
        props = result.get("properties", {})
        if not isinstance(props, dict):
            valid = False
            continue
        item_profile = props.get("mountfile.fixture.profile", "")
        observed_profiles.add(item_profile)
        source_before = props.get("mountfile.fixture.sha256_before_checker", "")
        source_after = props.get("mountfile.fixture.sha256_after_checker", "")
        size_raw = props.get("mountfile.fixture.image_size_bytes", "")
        tool = props.get("mountfile.checker.tool", "")
        verdict = props.get("mountfile.checker.verdict", "")
        copy_before = props.get("mountfile.checker.copy_sha256_before", "")
        copy_after = props.get("mountfile.checker.copy_sha256_after", "")
        source_unchanged = props.get("mountfile.checker.source_unchanged", "")
        copy_unchanged = props.get("mountfile.checker.copy_unchanged", "")
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            size_bytes = 0

        def valid_hash(value: object) -> bool:
            return (
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            )

        is_direct_reader = checker == "pytsk3"
        case_valid = (
            result.get("status") == "passed"
            and item_profile in required_profiles
            and valid_hash(source_before)
            and source_before == source_after
            and size_bytes > 0
            and tool == checker
            and verdict == expected_checker_verdict
            and source_unchanged == "true"
        )
        if is_direct_reader:
            case_valid = (
                case_valid
                and not copy_before
                and not copy_after
                and copy_unchanged == "not_applicable"
            )
        else:
            case_valid = (
                case_valid
                and valid_hash(copy_before)
                and copy_before == source_before
                and copy_after == copy_before
                and copy_unchanged == "true"
            )
        valid = valid and case_valid
        images.append(
            {
                "test_case": result.get("case_name", ""),
                "profile": item_profile,
                "image_size_bytes": size_bytes,
                "source_sha256_before_checker": source_before,
                "source_sha256_after_checker": source_after,
                "checker": tool,
                "checker_verdict": verdict,
                "checker_copy_sha256_before": copy_before or None,
                "checker_copy_sha256_after": copy_after or None,
                "source_unchanged": source_unchanged == "true",
                "checker_copy_unchanged": copy_unchanged,
                "evidence_status": "pass" if case_valid else "blocked",
            }
        )
    expected_count = len(required_profiles)
    if len(images) != expected_count or observed_profiles != required_profiles:
        valid = False
    return images, "complete" if valid else "blocked"


def build_execution_report(
    repo_root: Path | None = None,
    *,
    junit_path: Path | None = None,
    allow_unavailable: bool = False,
) -> tuple[dict[str, object], int]:
    root = repo_root or _REPO_ROOT
    tests_root = root / "tests"
    matrix = build_report(tests_root)
    nodes = _fixture_nodes(tests_root)
    junit = junit_path or root / "checker-acceptance-junit.xml"
    entries = []
    nodeids: list[str] = []
    missing: list[str] = []
    unavailable: list[str] = []
    for item in MATRIX:
        checker = item["checker"]
        if item.get("checker_kind") == "python_module":
            available = bool(__import__("importlib.util").util.find_spec(checker))
            executable = checker if available else None
        else:
            executable = shutil.which(checker)
            available = bool(executable)
        if not available:
            unavailable.append(item["profile"])
        tests = []
        for test_name in item["fixture_tests"]:
            nodeid = nodes.get(test_name)
            if nodeid is None:
                missing.append(test_name)
            else:
                tests.append(test_name)
                if available:
                    nodeids.append(nodeid)
        version = _tool_version(item, executable)
        entries.append(
            {
                "profile": item["profile"],
                "checker": checker,
                "checker_path": executable,
                "checker_version": version,
                "checker_availability": "available" if available else "unavailable",
                "fixture_tests": tests,
                "fixture_status": "pending",
                "evidence": "executed_fixture_required",
                "parity": "not_claimed",
            }
        )

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-o",
        "junit_family=legacy",
        f"--junitxml={junit}",
        *nodeids,
    ]
    started = time.monotonic()
    if nodeids:
        result = subprocess.run(command, cwd=root, check=False)
        pytest_exit = result.returncode
    else:
        pytest_exit = 0
    elapsed = time.monotonic() - started
    parsed = _junit_results(junit)
    for entry in entries:
        statuses = []
        for test_name in entry["fixture_tests"]:
            statuses.extend(parsed.get(test_name, []))
        if not statuses:
            entry["fixture_status"] = (
                "unavailable"
                if entry["checker_availability"] == "unavailable"
                else "missing"
            )
        elif any(item["status"] in {"failed", "error"} for item in statuses):
            entry["fixture_status"] = "failed"
        elif any(item["status"] == "skipped" for item in statuses):
            entry["fixture_status"] = "skipped"
        else:
            entry["fixture_status"] = "passed"
        entry["results"] = statuses
        fixture_images, fixture_evidence_status = _fixture_evidence(
            entry["profile"], entry["checker"], statuses
        )
        entry["fixture_images"] = fixture_images
        entry["fixture_evidence_status"] = fixture_evidence_status

    blockers = [
        entry["profile"]
        for entry in entries
        if entry["fixture_status"] not in {"passed"}
    ]
    blockers.extend(
        f"fixture-evidence:{entry['profile']}"
        for entry in entries
        if entry["fixture_evidence_status"] != "complete"
    )
    blockers.extend(f"missing:{name}" for name in missing)
    if unavailable and not allow_unavailable:
        blockers.extend(f"unavailable:{name}" for name in unavailable)
    report = {
        "schema": 1,
        "report": "mountfile-checker-execution-acceptance",
        "matrix_schema": matrix["schema"],
        "command": command,
        "elapsed_seconds": elapsed,
        "pytest_exit_code": pytest_exit,
        "entries": entries,
        "verdict": "pass" if not blockers and pytest_exit == 0 else "blocked",
        "blockers": blockers,
        "evidence": "executed_fixture_results",
        "parity": "not_claimed",
    }
    return report, (0 if report["verdict"] == "pass" else 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("--allow-unavailable", action="store_true")
    args = parser.parse_args()
    report, code = build_execution_report(
        junit_path=args.junitxml, allow_unavailable=args.allow_unavailable
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
