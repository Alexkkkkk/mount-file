"""Tests for the aggregated readiness/blocker report."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.blocker_report import (
    ARTIFACTS,
    READY,
    SCHEMA,
    build_report,
    main,
    render_markdown,
)


def _write(root: Path, name: str, payload: object) -> None:
    target = root / "docs" / "generated" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _root(tmp_path: Path, *, checker_row: dict, acceptance_entry: dict) -> Path:
    _write(
        tmp_path,
        "capability-matrix.json",
        {"operations": ["read-file"], "profiles": []},
    )
    _write(tmp_path, "checker-matrix.json", {"entries": [checker_row]})
    _write(tmp_path, "recovery-matrix.json", {"entries": []})
    _write(tmp_path, "recovery-acceptance.json", {"entries": [acceptance_entry]})
    _write(tmp_path, "product-status.json", {"surfaces": {}})
    return tmp_path


def _checker(**overrides: object) -> dict:
    row = {
        "profile": "ext2-native-rw",
        "checker": "e2fsck",
        "availability": "available",
        "checker_authoritative": True,
        "parity": "pass",
        "fixture_status": "fixture_declared",
    }
    row.update(overrides)
    return row


def _acceptance(**overrides: object) -> dict:
    row = {
        "profile": "ext2-native-rw",
        "cells": [
            {"operation": "write-file", "state": "clean", "status": "pass"},
        ],
        "blocking_cells": [],
    }
    row.update(overrides)
    return row


def test_report_joins_checker_and_acceptance_into_one_row(tmp_path):
    root = _root(tmp_path, checker_row=_checker(), acceptance_entry=_acceptance())
    report = build_report(root)
    assert report["schema"] == SCHEMA
    assert report["promotes_status"] is False
    assert report["missing_artifacts"] == []
    row = report["profile_rows"][0]
    assert row["profile"] == "ext2-native-rw"
    assert row["status"] == READY
    assert row["blockers"] == []
    assert report["summary"]["ready"] == 1
    assert report["summary"]["blocked"] == 0


def test_available_checker_without_parity_never_becomes_ready(tmp_path):
    root = _root(
        tmp_path,
        checker_row=_checker(parity="not_claimed"),
        acceptance_entry=_acceptance(),
    )
    report = build_report(root)
    row = report["profile_rows"][0]
    assert row["status"] != READY
    assert "parity_not_claimed" in {b["code"] for b in row["blockers"]}
    assert report["summary"]["blocked"] == 1


def test_unavailable_checker_is_missing_evidence_not_a_pass(tmp_path):
    root = _root(
        tmp_path,
        checker_row=_checker(
            availability="unavailable",
            parity="not_claimed",
            reason="required checker/runtime is unavailable on this runner",
        ),
        acceptance_entry=_acceptance(),
    )
    row = build_report(root)["profile_rows"][0]
    codes = {b["code"] for b in row["blockers"]}
    assert "checker_unavailable" in codes
    assert row["status"] != READY


def test_secondary_checker_can_never_promote_a_row(tmp_path):
    root = _root(
        tmp_path,
        checker_row=_checker(checker="ntfsfix", checker_authoritative=False),
        acceptance_entry=_acceptance(),
    )
    row = build_report(root)["profile_rows"][0]
    assert "checker_not_authoritative" in {b["code"] for b in row["blockers"]}
    assert row["status"] != READY


def test_unproven_acceptance_cell_blocks_the_profile(tmp_path):
    root = _root(
        tmp_path,
        checker_row=_checker(),
        acceptance_entry=_acceptance(
            cells=[
                {"operation": "write-file", "state": "clean", "status": "pass"},
                {
                    "operation": "delete",
                    "state": "process-killed",
                    "status": "not_proven",
                },
            ],
            blocking_cells=["process_kill"],
        ),
    )
    row = build_report(root)["profile_rows"][0]
    codes = {b["code"] for b in row["blockers"]}
    assert row["acceptance_cells_not_proven"] == 1
    assert {"acceptance_cell_not_proven", "blocking_cell_declared"} <= codes
    assert row["status"] != READY


def test_missing_artifact_is_typed_and_check_exits_one(tmp_path):
    root = _root(tmp_path, checker_row=_checker(), acceptance_entry=_acceptance())
    (root / "docs" / "generated" / "product-status.json").unlink()
    report = build_report(root)
    assert report["missing_artifacts"] == ["product-status.json"]
    assert "artifact_missing" in report["summary"]["blocker_counts"]
    assert main(["--repo-root", str(root), "--format", "json", "--check"]) == 1


def test_fail_on_blocked_returns_two_and_clean_tree_returns_zero(tmp_path):
    blocked = _root(
        tmp_path / "blocked",
        checker_row=_checker(parity="not_claimed"),
        acceptance_entry=_acceptance(),
    )
    clean = _root(
        tmp_path / "clean",
        checker_row=_checker(),
        acceptance_entry=_acceptance(),
    )
    assert main(["--repo-root", str(blocked), "--fail-on-blocked"]) == 2
    assert main(["--repo-root", str(clean), "--fail-on-blocked"]) == 0


def test_report_is_json_serialisable_and_markdown_lists_every_profile(tmp_path):
    root = _root(
        tmp_path,
        checker_row=_checker(parity="not_claimed"),
        acceptance_entry=_acceptance(),
    )
    report = build_report(root)
    assert json.loads(json.dumps(report)) == report
    markdown = render_markdown(report)
    assert "ext2-native-rw" in markdown
    assert "parity_not_claimed" in markdown
    assert len(ARTIFACTS) == len(report["artifacts"])
