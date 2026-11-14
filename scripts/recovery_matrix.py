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
_OPERATIONS = ("write-file", "mkdir", "delete", "rename", "journal-recovery")
_STATES = ("clean", "dirty", "corrupt", "concurrent-change", "process-killed")
_CHECKERS = {
    "ext2": "e2fsck -fn",
    "FAT12": "fsck.fat -n",
    "FAT32": "fsck.fat -n",
    "exFAT": "fsck.exfat -n",
}


def _typed_evidence(value: str) -> dict[str, str]:
    """Project legacy contract labels into an explicit evidence vocabulary.

    ``pass`` is reserved for a reproducible test label. In particular,
    ``unsupported`` becomes ``unavailable`` rather than a successful result.
    """
    if value.startswith("tested_"):
        return {"status": "pass", "source": value}
    if value in {"unsupported", "not_applicable"}:
        return {"status": "unavailable", "source": value}
    return {"status": "not_proven", "source": value}


def _acceptance_cells(profile, recovery: dict[str, str]) -> list[dict[str, str]]:
    """Expand the compact recovery contract into auditable matrix cells.

    Missing evidence is never inferred to be successful.  The expansion copies
    profile-level evidence only to the matching state dimension and leaves all
    other combinations explicitly unavailable or not proven.
    """
    checker = _CHECKERS.get(profile.filesystem, "unavailable")
    cells: list[dict[str, str]] = []
    for operation in _OPERATIONS:
        for state in _STATES:
            if state == "clean":
                source = (
                    "not_applicable"
                    if operation == "journal-recovery"
                    else "not_proven"
                )
            elif state == "dirty":
                source = (
                    recovery.get("quarantine", "not_proven")
                    if operation == "journal-recovery"
                    else "not_proven"
                )
            elif state == "concurrent-change":
                source = (
                    recovery.get("rollback", "not_proven")
                    if operation == "write-file"
                    else "not_proven"
                )
            elif state == "process-killed":
                source = (
                    recovery.get("process_kill", "not_proven")
                    if operation == "write-file"
                    else "not_proven"
                )
            else:
                source = "not_proven"
            evidence = _typed_evidence(source)
            cells.append(
                {
                    "operation": operation,
                    "state": state,
                    "checker": checker,
                    "status": evidence["status"],
                    "source": evidence["source"],
                }
            )
    return cells


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
                "acceptance_dimensions": {
                    "operations": list(_OPERATIONS),
                    "states": list(_STATES),
                    "checker": _CHECKERS.get(profile.filesystem, "unavailable"),
                },
                "acceptance_cells": _acceptance_cells(profile, recovery),
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
        "acceptance_dimensions": {
            "operations": list(_OPERATIONS),
            "states": list(_STATES),
            "checkers": dict(_CHECKERS),
        },
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
