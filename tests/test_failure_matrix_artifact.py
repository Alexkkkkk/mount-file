# SPDX-License-Identifier: MIT
"""Tests for the machine-readable failure-coverage matrix artifact."""

import json

import pytest

from scripts.failure_matrix_artifact import SCHEMA, build_report, validate_report


def test_report_types_every_cell_without_skips(tmp_path):
    report = build_report(tmp_path)
    assert report["schema"] == SCHEMA
    assert report["skipped_cells"] == 0
    assert len(report["cells"]) == 20  # 5 profiles x 4 fault states
    assert report["covered_cells"] + report["refused_cells"] == 20
    for cell in report["cells"]:
        assert cell["status"] in ("covered_by_test_suite", "refused_no_evidence")


def test_covered_cells_cite_files_that_exist(tmp_path):
    evidence = tmp_path / "tests"
    evidence.mkdir()
    for name in (
        "test_native_writer_failure_matrix.py",
        "test_jbd2_interrupted_commit.py",
        "test_ntfs_writer_fault_coverage.py",
    ):
        (evidence / name).write_text("", encoding="utf-8")
    report = build_report(tmp_path)
    assert report["covered_cells"] == 20
    result = validate_report(report, tmp_path)
    assert result["verdict"] == "pass"
    assert result["blockers"] == []


def test_ext4_cells_claim_the_e2fsck_checker(tmp_path):
    report = build_report(tmp_path)
    ext4 = [c for c in report["cells"] if c["profile"] == "ext4"]
    assert ext4 and all(c["checker"] == "e2fsck" for c in ext4)


def test_validation_blocks_cells_that_lost_their_evidence(tmp_path):
    evidence = tmp_path / "tests"
    evidence.mkdir()
    names = (
        "test_native_writer_failure_matrix.py",
        "test_jbd2_interrupted_commit.py",
        "test_ntfs_writer_fault_coverage.py",
    )
    for name in names:
        (evidence / name).write_text("", encoding="utf-8")
    report = build_report(tmp_path)  # cells are covered at build time
    for name in names:
        (evidence / name).unlink()  # ...and now the evidence is gone
    result = validate_report(report, tmp_path)
    assert result["verdict"] == "blocked"
    assert any(b.startswith("missing_evidence:") for b in result["blockers"])


def test_report_round_trips_through_json(tmp_path):
    report = build_report(tmp_path)
    path = tmp_path / "failure-matrix.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert validate_report(reloaded, tmp_path)["verdict"] == "pass"
