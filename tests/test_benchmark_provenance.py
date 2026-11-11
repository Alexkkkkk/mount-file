from __future__ import annotations

import json

from scripts.benchmark_provenance import build_manifest


def test_missing_cross_platform_benchmark_is_explicitly_not_collected(tmp_path):
    report = build_manifest(tmp_path)
    assert report["schema"] == "mountfile.benchmark-provenance/v1"
    assert report["benchmark"]["status"] == "not_collected"
    assert report["benchmark"]["evidence"] == "none"
    assert report["benchmark"]["parity"] == "not_claimed"
    assert report["benchmark"]["write_operations"] is False


def test_validated_artifacts_are_hashed_without_claiming_parity(tmp_path):
    report_file = tmp_path / "bench_report.json"
    validation_file = tmp_path / "benchmark-validation.json"
    report_file.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    validation_file.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")

    report = build_manifest(
        tmp_path,
        benchmark_report=report_file,
        benchmark_validation=validation_file,
    )
    assert report["benchmark"]["status"] == "executed"
    assert report["benchmark"]["evidence"] == "benchmark_observed_only"
    assert report["benchmark"]["parity"] == "not_claimed"
    assert len(report["benchmark"]["report_sha256"]) == 64
    assert len(report["benchmark"]["validation_sha256"]) == 64


def test_fixture_hash_is_recorded_only_when_fixture_exists(tmp_path):
    fixture = tmp_path / "fixture.img"
    fixture.write_bytes(b"fixture")
    report = build_manifest(tmp_path, fixture=fixture)
    assert report["fixture"]["status"] == "declared"
    assert report["fixture"]["sha256"]
