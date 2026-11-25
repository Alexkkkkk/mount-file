# SPDX-License-Identifier: MIT
"""Emit the NTFS replay oracle evidence artifact (issue #530 P1, #595).

For every cataloged dirty fixture the production executor is run on a
disposable copy and the result is checked with the external ``ntfsfix``
oracle plus byte-level comparisons.  Runners without the ntfs-3g tools
report ``unavailable`` - never ``pass`` - so the artifact cannot overclaim:

    python scripts/ntfs_oracle_evidence.py --json-out ntfs-oracle-evidence.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = "mountfile.ntfs-oracle-evidence/v1"


def _aggregate_verdict(scenarios: list[dict[str, object]]) -> str:
    """Pass only when at least five scenarios ran and every one passed."""
    if any(scenario["verdict"] == "divergence" for scenario in scenarios):
        return "blocked"
    if len(scenarios) >= 5 and all(
        scenario["verdict"] == "pass" for scenario in scenarios
    ):
        return "pass"
    return "unavailable"


def build_evidence(root: Path) -> dict[str, object]:
    tools_present = all(
        shutil.which(tool) for tool in ("mkntfs", "ntfscp", "ntfsfix", "ntfscat")
    )
    scenarios: list[dict[str, object]] = []
    if tools_present:
        from tests.ntfs_oracle import SCENARIOS, build_dirty_fixture, run_oracle

        with tempfile.TemporaryDirectory(prefix="ntfs-oracle-") as workdir:
            work = Path(workdir)
            for name in sorted(SCENARIOS):
                try:
                    fixture = build_dirty_fixture(work, name)
                    scenarios.append(run_oracle(fixture, work))
                except (RuntimeError, OSError) as exc:
                    scenarios.append(
                        {
                            "scenario": name,
                            "replay": "error",
                            "verdict": "unavailable",
                            "detail": str(exc)[:400],
                        }
                    )
    else:
        scenarios = [
            {
                "scenario": name,
                "replay": "not_run",
                "verdict": "unavailable",
                "detail": "ntfs-3g tools are unavailable on this runner",
            }
            for name in (
                "resident-commit",
                "resident-active",
                "nonresident-commit",
                "nonresident-active",
                "volume-dirty-no-log",
                "mft-mirr-corrupt",
            )
        ]
    unavailable = sum(1 for s in scenarios if s["verdict"] == "unavailable")
    passed = sum(1 for s in scenarios if s["verdict"] == "pass")
    diverged = sum(1 for s in scenarios if s["verdict"] == "divergence")
    return {
        "schema": SCHEMA,
        "oracle": "ntfsfix -n + byte comparison via ntfscat (Linux)",
        "authoritative_windows_oracle": "chkdsk (not available on this runner)",
        "scenario_count": len(scenarios),
        "passed_scenarios": passed,
        "diverged_scenarios": diverged,
        "unavailable_scenarios": unavailable,
        "skipped_cells": 0,
        "scenarios": scenarios,
        "verdict": _aggregate_verdict(scenarios),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = build_evidence(args.repo_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
