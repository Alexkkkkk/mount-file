# SPDX-License-Identifier: MIT
"""Emit the failure-coverage matrix as a machine-readable CI artifact.

Cells are ``profile x operation x state x checker``.  A cell is
``covered_by_test_suite`` only when the test file that exercises it exists in
the tree; anything else is emitted as a typed refusal
(``refused_no_evidence``) - never a silent skip - so the artifact cannot
overclaim.  ``--validate`` re-checks an emitted report and blocks when a
covered cell lost its evidence.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SCHEMA = "mountfile.failure-matrix/v1"
SEAM_RE = re.compile(r'_fault\(\s*"([^"]+)"')

#: Writer profiles with their operation entry points and backing evidence.
PROFILES: dict[str, dict[str, object]] = {
    "fat32": {
        "operation": "write_file",
        "evidence": "tests/test_native_writer_failure_matrix.py",
        "checker": "not_claimed",
    },
    "ext2": {
        "operation": "write_file",
        "evidence": "tests/test_native_writer_failure_matrix.py",
        "checker": "not_claimed",
    },
    "exfat": {
        "operation": "write_file",
        "evidence": "tests/test_native_writer_failure_matrix.py",
        "checker": "not_claimed",
    },
    "ext4": {
        "operation": "write_file",
        "evidence": "tests/test_jbd2_interrupted_commit.py",
        "checker": "e2fsck",
    },
    "ntfs": {
        "operation": "write_file",
        "evidence": "tests/test_ntfs_writer_fault_coverage.py",
        "checker": "ntfsfix (check-only, -n)",
    },
}

#: Fault states exercised by the fault-seam tests.
STATES = ("short_write", "injected_exception", "process_kill", "cancellation")


def _collect_seams(native_write_path: Path) -> list[str]:
    try:
        text = native_write_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(set(SEAM_RE.findall(text)))


def build_report(root: Path) -> dict[str, object]:
    seams = _collect_seams(root / "mountfile" / "native_write.py")
    cells: list[dict[str, object]] = []
    for profile, spec in sorted(PROFILES.items()):
        evidence = str(spec["evidence"])
        covered = (root / evidence).is_file()
        for state in STATES:
            cells.append(
                {
                    "profile": profile,
                    "operation": spec["operation"],
                    "state": state,
                    "checker": spec["checker"],
                    "status": (
                        "covered_by_test_suite" if covered else "refused_no_evidence"
                    ),
                    "evidence": evidence if covered else None,
                }
            )
    return {
        "schema": SCHEMA,
        "fault_seams": seams,
        "cells": cells,
        "covered_cells": sum(
            1 for c in cells if c["status"] == "covered_by_test_suite"
        ),
        "refused_cells": sum(1 for c in cells if c["status"] == "refused_no_evidence"),
        "skipped_cells": 0,
        "checker_claims": "ext4 checker per B2/B10; ntfs ntfsfix check-only per C6; fat32/exfat/ext2 not_claimed",
    }


def validate_report(report: dict[str, object], root: Path) -> dict[str, object]:
    blockers: list[str] = []
    if report.get("schema") != SCHEMA:
        blockers.append("schema")
    if report.get("skipped_cells") != 0:
        blockers.append("skipped_cells")
    cells = report.get("cells")
    if not isinstance(cells, list) or not cells:
        blockers.append("cells")
    else:
        for cell in cells:
            if not isinstance(cell, dict):
                blockers.append("cells")
                continue
            if cell.get("status") == "covered_by_test_suite":
                evidence = cell.get("evidence")
                if not evidence or not (root / str(evidence)).is_file():
                    blockers.append(f"missing_evidence:{cell.get('profile')}")
    return {
        "schema": "mountfile.failure-matrix-validation/v1",
        "verdict": "pass" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.validate:
        report = json.loads(args.validate.read_text(encoding="utf-8"))
        result = validate_report(report, args.repo_root)
    else:
        result = build_report(args.repo_root)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if result.get("verdict") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
