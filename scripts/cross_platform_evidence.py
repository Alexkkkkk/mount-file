#!/usr/bin/env python3
"""Aggregate per-runner native evidence without turning absence into parity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EXPECTED_PLATFORMS = {"linux", "windows", "macos"}
REQUIRED_SCHEMA = "mountfile.native-read-evidence/v1"
PLATFORM_ALIASES = {"darwin": "macos"}


def _load_reports(root: Path) -> list[tuple[Path, dict]]:
    reports = []
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == REQUIRED_SCHEMA:
            reports.append((path, value))
    return reports


def aggregate(root: Path) -> dict[str, object]:
    reports = _load_reports(root)
    blockers: list[str] = []
    by_platform: dict[str, dict[str, object]] = {}
    seen_legs: set[str] = set()
    for path, report in reports:
        runner = report.get("runner", {})
        platform_name = runner.get("platform")
        if platform_name in PLATFORM_ALIASES:
            # platform.system() reports "Darwin" on macOS runners; evidence
            # files lowercase it to "darwin". Normalize to the canonical name.
            platform_name = PLATFORM_ALIASES[platform_name]
        if platform_name not in EXPECTED_PLATFORMS:
            blockers.append(f"unexpected_platform:{platform_name}")
            continue
        python_version = str(runner.get("python", "unknown"))
        leg = f"{platform_name}:{python_version}"
        if leg in seen_legs:
            blockers.append(f"duplicate_platform:{platform_name}")
            continue
        seen_legs.add(leg)
        gate = report.get("gate", {})
        summary = report.get("summary", {})
        status = gate.get("verdict")
        if status != "pass":
            blockers.append(f"blocked_platform:{platform_name}")
        if summary.get("verdict") != "pass":
            blockers.append(f"native_read_not_pass:{platform_name}")
        if report.get("scope", {}).get("legacy_fallback") != "disabled":
            blockers.append(f"fallback_not_disabled:{platform_name}")
        if (
            report.get("scope", {}).get("independent_parity")
            != "not_claimed_by_this_report"
        ):
            blockers.append(f"parity_claim:{platform_name}")
        by_platform[platform_name] = {
            "artifact": str(path),
            "gate": status,
            "summary": summary.get("verdict"),
            "runner": runner,
            "profiles": summary.get("profiles_with_passed_read_contract", []),
        }
    missing = sorted(EXPECTED_PLATFORMS - set(by_platform))
    blockers.extend(f"missing_platform:{name}" for name in missing)
    return {
        "schema": "mountfile.cross-platform-evidence/v1",
        "verdict": "pass" if not blockers else "blocked",
        "platforms_expected": sorted(EXPECTED_PLATFORMS),
        "platforms_observed": sorted(by_platform),
        "platforms": by_platform,
        "blockers": blockers,
        "parity": "not_claimed",
        "unavailable_checker_is_not_pass": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = aggregate(args.root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
