import json
from pathlib import Path

from scripts.cross_platform_evidence import (
    EXPECTED_PLATFORMS,
    aggregate,
    expected_platforms,
)


def _report(
    platform_name: str,
    gate: str = "pass",
    summary: str = "pass",
    python_version: str = "3.12.0",
) -> dict:
    return {
        "schema": "mountfile.native-read-evidence/v1",
        "runner": {"platform": platform_name, "python": python_version},
        "gate": {"verdict": gate},
        "summary": {
            "verdict": summary,
            "profiles_with_passed_read_contract": ["ext2-native-ro"],
        },
        "scope": {
            "legacy_fallback": "disabled",
            "independent_parity": "not_claimed_by_this_report",
        },
    }


def _write(root: Path, name: str, report: dict) -> None:
    path = root / name
    path.write_text(json.dumps(report), encoding="utf-8")


def _expect_full_matrix(monkeypatch):
    monkeypatch.setenv("MOUNTFILE_EXPECTED_PLATFORMS", "linux,windows,macos")


def test_expected_platforms_defaults_to_full_matrix_and_accepts_fast_tier(monkeypatch):
    monkeypatch.delenv("MOUNTFILE_EXPECTED_PLATFORMS", raising=False)
    assert expected_platforms() == EXPECTED_PLATFORMS

    monkeypatch.setenv("MOUNTFILE_EXPECTED_PLATFORMS", "linux,windows")
    assert expected_platforms() == {"linux", "windows"}

    monkeypatch.setenv("MOUNTFILE_EXPECTED_PLATFORMS", "linux,windows,macos")
    assert expected_platforms() == EXPECTED_PLATFORMS


def test_aggregate_normalizes_darwin_to_macos(tmp_path, monkeypatch):
    _expect_full_matrix(monkeypatch)
    for platform_name in ("linux", "windows"):
        _write(tmp_path, f"{platform_name}.json", _report(platform_name))
    macos = _report("darwin")  # platform.system() lowercased
    _write(tmp_path, "macos.json", macos)
    result = aggregate(tmp_path)
    assert result["verdict"] == "pass"
    assert result["platforms_observed"] == ["linux", "macos", "windows"]


def test_aggregate_full_matrix_requires_macos(tmp_path, monkeypatch):
    _expect_full_matrix(monkeypatch)
    _write(tmp_path, "linux.json", _report("linux"))
    _write(tmp_path, "windows.json", _report("windows"))
    result = aggregate(tmp_path)
    assert result["verdict"] == "blocked"
    assert "missing_platform:macos" in result["blockers"]


def test_aggregate_passes_complete_conservative_matrix(tmp_path, monkeypatch):
    _expect_full_matrix(monkeypatch)
    for platform_name in ("linux", "windows", "macos"):
        _write(tmp_path, f"{platform_name}.json", _report(platform_name))
    result = aggregate(tmp_path)
    assert result["verdict"] == "pass"
    assert result["platforms_observed"] == ["linux", "macos", "windows"]
    assert result["parity"] == "not_claimed"
    assert result["unavailable_checker_is_not_pass"] is True


def test_aggregate_passes_pr_fast_tier_without_claiming_macos(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUNTFILE_EXPECTED_PLATFORMS", "linux,windows")
    for platform_name in ("linux", "windows"):
        _write(tmp_path, f"{platform_name}.json", _report(platform_name))
    result = aggregate(tmp_path)
    assert result["verdict"] == "pass"
    assert result["platforms_expected"] == ["linux", "windows"]
    assert result["platforms_observed"] == ["linux", "windows"]


def test_aggregate_rejects_blocked_and_duplicate_platforms(tmp_path, monkeypatch):
    _expect_full_matrix(monkeypatch)
    _write(
        tmp_path,
        "linux-a.json",
        _report("linux", gate="blocked", python_version="3.11.0"),
    )
    _write(tmp_path, "linux-b.json", _report("linux", python_version="3.12.0"))
    for platform_name in ("windows", "macos"):
        _write(tmp_path, f"{platform_name}.json", _report(platform_name))
    result = aggregate(tmp_path)
    assert result["verdict"] == "blocked"
    assert "blocked_platform:linux" in result["blockers"]
    assert "duplicate_platform:linux" in result["blockers"]
    assert result["platforms"]["linux"]["runner"]["python"] == "3.11.0"


def test_aggregate_rejects_fallback_or_parity_claim(tmp_path, monkeypatch):
    _expect_full_matrix(monkeypatch)
    for platform_name in ("linux", "windows", "macos"):
        report = _report(platform_name)
        report["scope"]["legacy_fallback"] = "enabled"
        _write(tmp_path, f"{platform_name}.json", report)
    result = aggregate(tmp_path)
    assert result["verdict"] == "blocked"
    assert (
        len([item for item in result["blockers"] if "fallback_not_disabled" in item])
        == 3
    )
