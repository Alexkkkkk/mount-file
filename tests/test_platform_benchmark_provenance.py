from pathlib import Path

from scripts.platform_benchmark_provenance import (
    OPERATIONS,
    RESOURCE_LIMITS,
    SCHEMA,
    build_report,
    validate_report,
)


def _report(tmp_path: Path) -> dict:
    report = build_report(repo_root=Path(__file__).resolve().parents[1])
    report["source"]["revision"] = "a" * 40
    report["source"]["worktree_dirty"] = False
    return report


def test_provenance_report_has_all_platform_operations_and_conservative_claims(
    tmp_path,
):
    report = _report(tmp_path)
    assert report["schema"] == SCHEMA
    assert set(report["workload"]["operations"]) == set(OPERATIONS)
    assert report["workload"]["resource_limits"] == RESOURCE_LIMITS
    assert report["claims"] == {
        "evidence": "benchmark_observed_only",
        "parity": "not_claimed",
        "power_loss": "unsupported",
        "crash_recovery": "not_claimed",
    }
    result = validate_report(report)
    assert result["verdict"] == "pass"
    assert result["blockers"] == []


def test_provenance_rejects_dirty_source_and_parity_overclaim(tmp_path):
    report = _report(tmp_path)
    report["source"]["worktree_dirty"] = True
    report["claims"]["parity"] = "production_ready"
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "source.worktree_dirty" in result["blockers"]
    assert "claims.parity" in result["blockers"]


def test_provenance_rejects_missing_operation_and_changed_limits(tmp_path):
    report = _report(tmp_path)
    del report["workload"]["operations"]["extract"]
    report["workload"]["resource_limits"]["max_repeats"] += 1
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "workload.operations" in result["blockers"]
    assert "workload.resource_limits" in result["blockers"]


def test_provenance_rejects_crash_and_power_loss_guarantees(tmp_path):
    report = _report(tmp_path)
    report["claims"]["power_loss"] = "guaranteed"
    report["claims"]["crash_recovery"] = "production_ready"
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "claims.power_loss" in result["blockers"]
    assert "claims.crash_recovery" in result["blockers"]
