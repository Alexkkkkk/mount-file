#!/usr/bin/env python3
"""Report evidence-bounded writer recovery acceptance status.

This is a contract report, not a test runner. It never upgrades a capability
claim: unsupported or unproven recovery cells remain blockers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.capabilities import CAPABILITY_MATRIX_VERSION, NATIVE_CAPABILITY_MATRIX

_REQUIRED = ("rollback", "process_kill", "power_loss", "quarantine")
_BLOCKING_VALUES = {"not_proven", "unsupported", "not_applicable"}


def build_report() -> dict[str, object]:
    entries = []
    for profile in NATIVE_CAPABILITY_MATRIX:
        if not profile.write_capabilities:
            continue
        recovery = profile.recovery_contract
        blockers = [key for key in _REQUIRED if recovery.get(key) in _BLOCKING_VALUES]
        entries.append(
            {
                "profile_id": profile.profile_id,
                "filesystem": profile.filesystem,
                "strategy": profile.write_strategy,
                "recovery": recovery,
                "required_cells": list(_REQUIRED),
                "blocking_cells": blockers,
                "verdict": "blocked" if blockers else "evidence_bounded",
                "evidence": "capability_contract",
                "parity": "not_claimed",
                "power_loss": recovery.get("power_loss", "unsupported"),
            }
        )
    return {
        "schema": 1,
        "report": "mountfile-writer-recovery-acceptance",
        "capability_matrix_version": CAPABILITY_MATRIX_VERSION,
        "production_ready": False,
        "production_ready_reason": (
            "A profile is not production-ready until every required recovery cell "
            "has reproducible evidence; power-loss remains unsupported."
        ),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(build_report(), indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
