"""Regression tests for saved benchmark artifact validation."""

from scripts.benchmark_artifact import validate_report


def _report():
    backend = {
        "capability_status": {"parity": "not_claimed"},
        "operations": {
            "list_dir": {},
            "stat": {},
            "read_range": {},
            "extract": {},
        },
        "iterations": [{"elapsed_seconds": 0.1}, {"elapsed_seconds": 0.2}],
    }
    return {
        "schema_version": 2,
        "environment": {"runtime_versions": {"source_worktree_dirty": False}},
        "image": {"size_bytes": 4096},
        "extent": {"offset_bytes": 0, "volume_size_bytes": 4096},
        "workload": {
            "repeats": 2,
            "warmups": 1,
            "range_read_limit_bytes_per_file": 1 << 20,
        },
        "capability_contract": {
            "evidence": "benchmark_observed_only",
            "parity": "not_claimed",
        },
        "comparison": {"content_equivalent": True, "content_mismatches": []},
        "backends": {"native": backend, "pytsk3": dict(backend)},
    }


def test_valid_benchmark_artifact_passes():
    result = validate_report(_report())
    assert result["verdict"] == "pass"
    assert result["blockers"] == []
    assert result["write_operations"] is False


def test_benchmark_artifact_rejects_extent_outside_image():
    report = _report()
    report["extent"]["offset_bytes"] = 1
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "extent.outside_image" in result["blockers"]


def test_benchmark_artifact_rejects_content_mismatch():
    report = _report()
    report["comparison"] = {
        "content_equivalent": False,
        "content_mismatches": ["/bench/file.bin"],
    }
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "comparison.content_equivalent" in result["blockers"]
    assert "comparison.content_mismatches" in result["blockers"]


def test_benchmark_artifact_requires_reference_backend():
    report = _report()
    del report["backends"]["pytsk3"]
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "backends.required" in result["blockers"]


def test_benchmark_artifact_rejects_dirty_or_overclaiming_report():
    report = _report()
    report["environment"]["runtime_versions"]["source_worktree_dirty"] = True
    report["capability_contract"]["parity"] = "pass"
    report["backends"]["native"]["capability_status"]["parity"] = "pass"
    result = validate_report(report)
    assert result["verdict"] == "blocked"
    assert "environment.runtime_versions.source_worktree_dirty" in result["blockers"]
    assert "capability_contract.parity" in result["blockers"]
