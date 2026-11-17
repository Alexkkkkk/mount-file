# SPDX-License-Identifier: MIT
"""Tests for the machine-readable failure-matrix execution evidence."""

import json
from pathlib import Path

from scripts.failure_matrix_artifact import SCHEMA, build_report, validate_report


def _cell(report, profile, state):
    return next(
        cell
        for cell in report["cells"]
        if cell["profile"] == profile and cell["state"] == state
    )


def _junit(path: Path, testcases: list[tuple[str, str, str]]) -> Path:
    cases = []
    for classname, name, status in testcases:
        outcome = f"<{status}/>" if status in {"failure", "error", "skipped"} else ""
        cases.append(
            f'<testcase classname="{classname}" name="{name}">{outcome}</testcase>'
        )
    path.write_text(
        '<testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_empty_evidence_files_do_not_create_coverage(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name in (
        "test_fat1216_writer.py",
        "test_ext2_native.py",
        "test_ntfs_writer_fault_coverage.py",
    ):
        (tests_dir / name).write_text("", encoding="utf-8")
    junit = _junit(tmp_path / "empty.xml", [])

    report = build_report(tmp_path, junit)

    assert report["schema"] == SCHEMA
    assert report["verdict"] == "execution_recorded"
    assert report["acceptance"] == "not_claimed"
    assert report["production_ready"] is False
    assert len(report["cells"]) == 28  # 7 profiles x 4 failure states
    assert report["test_status_counts"]["passed"] == 0
    assert all(cell["test_status"] == "not_proven" for cell in report["cells"])
    assert all(cell["acceptance_status"] == "not_accepted" for cell in report["cells"])
    assert validate_report(report, tmp_path, junit)["verdict"] == "pass"


def test_only_matching_passed_node_ids_are_recorded(tmp_path):
    junit = _junit(
        tmp_path / "tests.xml",
        [
            (
                "tests.test_fat1216_writer",
                "test_fat1216_short_write_rolls_back_and_preserves_sentinels",
                "passed",
            ),
            (
                "tests.test_fat1216_writer",
                "test_fat1216_process_kill_before_atomic_replace_never_publishes[12]",
                "passed",
            ),
            (
                "tests.test_fat1216_writer",
                "test_fat1216_process_kill_before_atomic_replace_never_publishes[16]",
                "passed",
            ),
        ],
    )

    report = build_report(tmp_path, junit)

    assert _cell(report, "fat12", "short_write")["test_status"] == "passed"
    assert _cell(report, "fat16", "short_write")["test_status"] == "not_proven"
    assert _cell(report, "fat12", "process_kill")["test_evidence"] == [
        "tests.test_fat1216_writer::test_fat1216_process_kill_before_atomic_replace_never_publishes[12]"
    ]
    assert _cell(report, "fat16", "process_kill")["test_status"] == "passed"
    assert _cell(report, "fat16", "process_kill")["test_evidence"] == [
        "tests.test_fat1216_writer::test_fat1216_process_kill_before_atomic_replace_never_publishes[16]"
    ]


def test_checker_evidence_is_separate_and_ntfsfix_is_not_chkdsk_parity(tmp_path):
    junit = _junit(
        tmp_path / "checkers.xml",
        [
            (
                "tests.test_ntfs_crash_oracle_matrix",
                "test_ntfs_crash_oracle_matrix[committed-resident]",
                "passed",
            ),
        ],
    )
    report = build_report(tmp_path, junit)
    checkers = {item["profile"]: item for item in report["checker_evidence"]}

    assert _cell(report, "ntfs", "process_kill")["test_status"] == "not_proven"
    assert checkers["ntfs"]["status"] == "partial"
    assert checkers["ntfs"]["checker"] == "ntfsfix -n"
    assert checkers["ntfs"]["authority"] == "secondary_only_not_chkdsk_parity"
    assert report["production_ready"] is False


def test_skipped_case_is_unavailable_and_failed_case_is_failed(tmp_path):
    junit = _junit(
        tmp_path / "mixed.xml",
        [
            (
                "tests.test_fat1216_writer",
                "test_fat1216_short_write_rolls_back_and_preserves_sentinels",
                "skipped",
            ),
            (
                "tests.test_fat1216_writer",
                "test_fat1216_process_kill_before_atomic_replace_never_publishes[12]",
                "failure",
            ),
        ],
    )
    report = build_report(tmp_path, junit)

    assert _cell(report, "fat12", "short_write")["test_status"] == "unavailable"
    assert _cell(report, "fat12", "process_kill")["test_status"] == "failed"
    assert report["verdict"] == "tests_failed"


def test_validation_recomputes_report_from_junit_and_rejects_forged_green(tmp_path):
    junit = _junit(
        tmp_path / "empty.xml",
        [],
    )
    report = build_report(tmp_path, junit)
    forged = json.loads(json.dumps(report))
    _cell(forged, "ext4", "process_kill")["test_status"] = "passed"
    result = validate_report(forged, tmp_path, junit)
    assert result["verdict"] == "blocked"
    assert "report_does_not_match_junit_and_declared_selectors" in result["blockers"]


def test_report_round_trips_through_json_with_its_junit_source(tmp_path):
    junit = _junit(
        tmp_path / "tests.xml",
        [
            (
                "tests.test_native_writer_failure_matrix",
                "test_fat32_process_kill_before_atomic_replace_never_publishes",
                "passed",
            ),
        ],
    )
    report = build_report(tmp_path, junit)
    path = tmp_path / "failure-matrix.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert validate_report(reloaded, tmp_path, junit)["verdict"] == "pass"


def test_malformed_junit_is_typed_unavailable_instead_of_green(tmp_path):
    junit = tmp_path / "truncated.xml"
    junit.write_text("<testsuites><testsuite>", encoding="utf-8")

    report = build_report(tmp_path, junit)

    assert report["verdict"] == "junit_unavailable"
    assert report["junit_report"]["status"] == "unavailable"
    assert all(cell["test_status"] == "unavailable" for cell in report["cells"])
    assert all(item["status"] == "unavailable" for item in report["checker_evidence"])
    assert validate_report(report, tmp_path, junit)["verdict"] == "pass"


def test_unmapped_junit_failure_still_marks_overall_verdict_failed(tmp_path):
    junit = _junit(
        tmp_path / "unmapped-failure.xml",
        [("tests.test_unmapped", "test_other_failure", "failure")],
    )

    report = build_report(tmp_path, junit)

    assert report["verdict"] == "tests_failed"
    assert report["junit_report"]["failed"] == 1
    assert report["test_status_counts"]["failed"] == 0


def test_schedule_run_is_explicitly_eligible_for_nightly_acceptance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    report = build_report(tmp_path, _junit(tmp_path / "scheduled.xml", []))
    assert report["run_provenance"] == {
        "event": "schedule",
        "run_id": "12345",
        "sha": "a" * 40,
        "schedule_eligible": True,
    }


def test_manual_run_cannot_be_counted_as_nightly_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_RUN_ID", "67890")
    report = build_report(tmp_path, _junit(tmp_path / "manual.xml", []))
    assert report["run_provenance"]["event"] == "workflow_dispatch"
    assert report["run_provenance"]["schedule_eligible"] is False


def test_missing_trigger_metadata_is_not_scheduled(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    report = build_report(tmp_path, _junit(tmp_path / "local.xml", []))
    assert report["run_provenance"] == {
        "event": None,
        "run_id": None,
        "sha": None,
        "schedule_eligible": False,
    }
