#!/usr/bin/env python3
"""macOS read-only adapter contract evidence (issue #389 / #510, E1).

``scripts/native_read_evidence.py`` currently reports
``macos_read_only_adapter_contract`` as ``not_collected``/``blocked`` because
the macOS CI runner only builds the binary and never runs the adapter
contract suite.  This module closes that gap additively: it validates a
JUnit report produced by a macOS runner job and turns it into the same
typed group report shape the platform evidence gate already consumes
(``pass`` / ``blocked`` / ``not_collected``), so ``ci.yml`` can wire a
macOS ``pytest`` step without touching the trusted generator's vocabulary.

Wiring (ci.yml, macOS job, after the adapter-contract pytest step)::

    - name: macOS read-only adapter contract evidence
      run: |
        python scripts/macos_adapter_evidence.py \
          --junitxml pytest-macos-adapter.xml \
          --json-out artifacts/macos-adapter-evidence.json

A ``pass`` verdict requires every declared macOS adapter-contract test to
run and succeed on the runner (parametrized cases count individually,
matched by base name); anything less is ``blocked`` with per-test
blockers, never an inferred pass.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_MACOS_ADAPTER_TESTS: dict[str, int] = {
    "test_detect_macos_filesystem_signatures": 4,
    "test_detection_honors_filesystem_offset": 1,
    "test_vfs_does_not_silently_fallback_for_macos_filesystems": 2,
    "test_hfs_requires_optional_pytsk3": 1,
    "test_apfs_missing_tool_is_explicit_and_does_not_mount": 1,
    "test_apfs_constructor_never_mounts": 1,
    "test_apfs_status_is_read_only_and_unmounted_without_side_effects": 1,
    "test_apfs_status_tracks_owned_mount_process_and_close": 1,
}


def _junit_cases(junit_path: Path) -> dict[str, list[dict[str, str]]]:
    """Map base testcase name -> list of {status, message} entries.

    Parametrized pytest ids (``name[param]``) are grouped under ``name``.
    """

    root = ET.parse(junit_path).getroot()
    cases: dict[str, list[dict[str, str]]] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        base = name.split("[", 1)[0]
        if not base:
            continue
        outcome = "passed"
        message = ""
        for child in case:
            if child.tag in {"failure", "error"}:
                outcome = "failed"
                message = child.get("message") or ""
                break
            if child.tag == "skipped":
                outcome = "skipped"
                message = child.get("message") or ""
                break
        cases.setdefault(base, []).append(
            {"status": outcome, "message": message}
        )
    return cases


def build_macos_adapter_evidence(junit_path: Path | None) -> dict[str, object]:
    if junit_path is None or not junit_path.is_file():
        return {
            "status": "not_collected",
            "blockers": ["macOS adapter-contract JUnit report was not provided"],
        }
    try:
        cases = _junit_cases(junit_path)
    except (OSError, ET.ParseError) as exc:
        return {
            "status": "blocked",
            "blockers": [f"invalid JUnit report: {type(exc).__name__}"],
        }

    probes: dict[str, object] = {}
    blockers: list[str] = []
    for test_name, expected_runs in REQUIRED_MACOS_ADAPTER_TESTS.items():
        entries = cases.get(test_name, [])
        if not entries:
            probes[test_name] = {"status": "not_collected", "observed_cases": 0}
            blockers.append(f"{test_name}: not collected on the macOS runner")
            continue
        statuses = [entry["status"] for entry in entries]
        if len(entries) < expected_runs:
            probes[test_name] = {
                "status": "blocked",
                "observed_cases": len(entries),
                "case_statuses": statuses,
            }
            blockers.append(
                f"{test_name}: expected {expected_runs} cases, observed {len(entries)}"
            )
            continue
        if any(status != "passed" for status in statuses):
            probes[test_name] = {
                "status": "blocked",
                "observed_cases": len(entries),
                "case_statuses": statuses,
            }
            blockers.append(f"{test_name}: failed on the macOS runner")
            continue
        probes[test_name] = {
            "status": "pass",
            "observed_cases": len(entries),
            "case_statuses": statuses,
        }
    return {
        "status": "pass" if not blockers else "blocked",
        "probes": probes,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junitxml", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = build_macos_adapter_evidence(args.junitxml)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
