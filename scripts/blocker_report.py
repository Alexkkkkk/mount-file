# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Aggregate the canonical status artifacts into one readiness report.

The repository already publishes machine-readable artifacts under
``docs/generated``: capability, checker, recovery and product status.  Each one
answers a different question, and the open production blockers (#384, #530)
exist precisely because a reader cannot see in one place *which evidence is
missing for which profile*.

This script adds no new facts.  It joins what is already published, keeps the
typed vocabulary (``available`` / ``unavailable`` / ``pass`` / ``not_claimed``
/ ``not_proven``) and refuses to promote anything: a profile is reported as
``ready`` only when the checker was available and authoritative, parity was
actually claimed as ``pass``, and the acceptance matrix has no unproven cell.
Everything else is ``blocked`` with an explicit list of reasons - the
fail-closed behaviour the tracker asks for.

    python scripts/blocker_report.py                     # markdown
    python scripts/blocker_report.py --format json
    python scripts/blocker_report.py --check             # exit 1 if artifacts missing
    python scripts/blocker_report.py --fail-on-blocked   # exit 2 if a profile is blocked

The report is advisory: no acceptance claim, no README status token and no
capability matrix entry is derived from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SCHEMA = "mountfile.blocker-report/v1"

GENERATED_DIR = Path("docs") / "generated"

#: Canonical artifacts the report is allowed to cite.
ARTIFACTS = (
    "capability-matrix.json",
    "checker-matrix.json",
    "recovery-matrix.json",
    "recovery-acceptance.json",
    "product-status.json",
)

#: Row outcomes.  ``READY`` is the only one that may be read as "no blocker".
READY = "ready"
BLOCKED = "blocked"

#: Typed blocker codes.  Two states are never enough: an absent checker is
#: missing evidence, which is not the same thing as a failing gate.
BLOCKER_DOCS = {
    "artifact_missing": "regenerate with scripts/sync_repository_status.py",
    "checker_row_missing": "add the profile to scripts/checker_matrix.py MATRIX",
    "checker_unavailable": "install the checker on the runner (scripts/bootstrap-tools.sh)",
    "checker_not_authoritative": "cite an authoritative checker; secondary tools cannot promote a row (#530 P1)",
    "parity_not_claimed": "run the fixture test and record a parity verdict; inventory alone is not parity (#389)",
    "acceptance_row_missing": "join an acceptance entry to this profile in the recovery matrix (#387)",
    "acceptance_cell_not_proven": "close the cell with a reproducible failure-injection artifact (#387, #530 P2)",
    "blocking_cell_declared": "the manifest still lists this state as blocking (#387, #530 P2)",
}


def _blocker(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "requires": BLOCKER_DOCS[code]}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(root: Path) -> list[dict[str, Any]]:
    """Return one row per canonical artifact, present or not."""

    rows = []
    for name in ARTIFACTS:
        path = root / GENERATED_DIR / name
        rows.append(
            {
                "artifact": name,
                "present": path.is_file(),
                "sha256": _sha256(path) if path.is_file() else None,
            }
        )
    return rows


def load_artifacts(
    root: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Load every canonical artifact, reporting unreadable ones by name."""

    loaded: dict[str, Any] = {}
    missing: list[str] = []
    for name in ARTIFACTS:
        path = root / GENERATED_DIR / name
        if not path.is_file():
            missing.append(name)
            loaded[name] = None
            continue
        try:
            loaded[name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            missing.append(name)
            loaded[name] = None
    return loaded, missing


def checker_rows(checker_artifact: Any) -> dict[str, dict[str, Any]]:
    """Index checker-matrix entries by profile name."""

    rows: dict[str, dict[str, Any]] = {}
    entries = (checker_artifact or {}).get("entries") or []
    for entry in entries:
        profile = entry.get("profile")
        if profile:
            rows[str(profile)] = entry
    return rows


def acceptance_rows(acceptance_artifact: Any) -> dict[str, dict[str, Any]]:
    """Index acceptance entries by their declared profile/name/id."""

    rows: dict[str, dict[str, Any]] = {}
    entries = (acceptance_artifact or {}).get("entries") or []
    for entry in entries:
        key = entry.get("profile") or entry.get("name") or entry.get("id")
        if key:
            rows[str(key)] = entry
    return rows


def _cell_is_proven(cell: dict[str, Any]) -> bool:
    return cell.get("status") == "pass"


def profile_row(
    profile: str,
    checker: dict[str, Any] | None,
    acceptance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Join one profile's checker and acceptance evidence into typed blockers."""

    blockers: list[dict[str, str]] = []
    checker_state = None
    parity = None
    if checker is None:
        blockers.append(
            _blocker("checker_row_missing", "profile has no checker-matrix row")
        )
    else:
        checker_state = checker.get("availability")
        parity = checker.get("parity")
        if checker_state != "available":
            detail = str(checker.get("reason") or checker_state or "unavailable")
            blockers.append(_blocker("checker_unavailable", detail))
        if checker.get("checker_authoritative") is not True:
            blockers.append(
                _blocker(
                    "checker_not_authoritative",
                    f"{checker.get('checker')} is secondary evidence for this profile",
                )
            )
        if parity != "pass":
            blockers.append(_blocker("parity_not_claimed", f"parity={parity}"))

    cells: list[dict[str, Any]] = []
    cells_not_proven = 0
    blocking_cells: list[str] = []
    if acceptance is None:
        blockers.append(
            _blocker(
                "acceptance_row_missing",
                "no acceptance entry joins this profile",
            )
        )
    else:
        raw_cells = acceptance.get("cells") or []
        cells = [cell for cell in raw_cells if isinstance(cell, dict)]
        cells_not_proven = len([cell for cell in cells if not _cell_is_proven(cell)])
        if cells_not_proven:
            blockers.append(
                _blocker(
                    "acceptance_cell_not_proven",
                    f"{cells_not_proven} of {len(cells)} cells are not proven",
                )
            )
        blocking_cells = [
            str(cell) for cell in (acceptance.get("blocking_cells") or [])
        ]
        if blocking_cells:
            blockers.append(
                _blocker(
                    "blocking_cell_declared",
                    "blocking: " + ", ".join(blocking_cells),
                )
            )

    return {
        "profile": profile,
        "checker": (checker or {}).get("checker"),
        "checker_state": checker_state,
        "checker_authoritative": (checker or {}).get("checker_authoritative"),
        "parity": parity,
        "fixture_status": (checker or {}).get("fixture_status"),
        "acceptance_cells": len(cells),
        "acceptance_cells_not_proven": cells_not_proven,
        "blocking_cells": blocking_cells,
        "blocked": bool(blockers),
        "status": BLOCKED if blockers else READY,
        "blockers": blockers,
    }


def build_report(root: Path) -> dict[str, Any]:
    """Build the aggregated report for the tree at ``root``."""

    loaded, missing = load_artifacts(root)
    artifact_blockers = [
        _blocker("artifact_missing", f"{name} is absent or unreadable")
        for name in missing
    ]

    checkers = checker_rows(loaded.get("checker-matrix.json"))
    acceptances = acceptance_rows(loaded.get("recovery-acceptance.json"))

    profiles = []
    for profile in sorted(set(checkers) | set(acceptances)):
        profiles.append(
            profile_row(profile, checkers.get(profile), acceptances.get(profile))
        )

    blocker_counts: dict[str, int] = {}
    for row in profiles:
        for blocker in row["blockers"]:
            code = blocker["code"]
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
    for blocker in artifact_blockers:
        code = blocker["code"]
        blocker_counts[code] = blocker_counts.get(code, 0) + 1

    ready = [row["profile"] for row in profiles if row["status"] == READY]
    blocked = [row["profile"] for row in profiles if row["status"] == BLOCKED]

    return {
        "schema": SCHEMA,
        "report": "mountfile-blocker-report",
        "promotes_status": False,
        "advisory": (
            "Derived only from docs/generated artifacts; no capability, parity or "
            "acceptance claim is promoted by this report."
        ),
        "artifacts": provenance(root),
        "missing_artifacts": missing,
        "summary": {
            "profiles": len(profiles),
            "ready": len(ready),
            "blocked": len(blocked),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "ready_profiles": ready,
        "blocked_profiles": blocked,
        "profile_rows": profiles,
        "blockers_by_code": [
            {
                "code": code,
                "count": count,
                "requires": BLOCKER_DOCS.get(code, "no documented closing action"),
            }
            for code, count in sorted(blocker_counts.items())
        ],
        "artifact_blockers": artifact_blockers,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as an issue-ready Markdown summary."""

    summary = report["summary"]
    lines = [
        "# MountFile readiness / blocker report",
        "",
        f"> {report['advisory']}",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| profiles | {summary['profiles']} |",
        f"| ready | {summary['ready']} |",
        f"| blocked | {summary['blocked']} |",
        "",
        "## Artifact provenance",
        "",
        "| artifact | present | sha256 |",
        "|---|---|---|",
    ]
    for row in report["artifacts"]:
        digest = (row["sha256"] or "-")[:16]
        lines.append(
            f"| {row['artifact']} | {'yes' if row['present'] else 'no'} | `{digest}` |"
        )

    lines += [
        "",
        "## Profiles",
        "",
        "| profile | checker | checker state | parity | cells not proven | status |",
        "|---|---|---|---|---:|---|",
    ]
    for row in report["profile_rows"]:
        lines.append(
            "| {profile} | {checker} | {checker_state} | {parity} | "
            "{cells} / {total} | {status} |".format(
                profile=row["profile"],
                checker=row["checker"] or "-",
                checker_state=row["checker_state"] or "-",
                parity=row["parity"] or "-",
                cells=row["acceptance_cells_not_proven"],
                total=row["acceptance_cells"],
                status=row["status"],
            )
        )

    lines += [
        "",
        "## Blockers by code",
        "",
        "| code | profiles | what closes it |",
        "|---|---:|---|",
    ]
    for row in report["blockers_by_code"]:
        lines.append(f"| `{row['code']}` | {row['count']} | {row['requires']} |")

    lines += ["", "## Per-profile blockers", ""]
    for row in report["profile_rows"]:
        if not row["blockers"]:
            lines.append(f"- **{row['profile']}** - ready, no blocker")
            continue
        lines.append(f"- **{row['profile']}** - {row['status']}")
        for blocker in row["blockers"]:
            lines.append(
                f"  - `{blocker['code']}`: {blocker['detail']}"
                f" -> {blocker['requires']}"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate canonical status artifacts into a blocker report.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a canonical artifact is absent or unreadable",
    )
    parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="exit 2 when at least one profile is blocked",
    )
    args = parser.parse_args(argv)

    report = build_report(args.repo_root.resolve())
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))

    if args.check and report["missing_artifacts"]:
        return 1
    if args.fail_on_blocked and report["summary"]["blocked"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
