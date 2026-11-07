"""Regression tests for the conservative product-status contract."""

import json

from scripts.product_status_contract import build_report


def test_product_status_contract_is_conservative_and_consistent():
    report = build_report()
    assert report["schema"] == 1
    assert report["verdict"] == "pass"
    assert report["blockers"] == []
    assert report["capability_matrix_version"] == 5
    assert report["recovery_production_ready"] is False
    assert report["claims"] == {
        "parity": "not_claimed",
        "power_loss": "unsupported",
        "native_ext3_ext4_write": "unsupported",
        "native_ntfs_write": "unsupported",
    }
    assert json.loads(json.dumps(report)) == report


def test_product_status_contract_blocks_stale_or_overclaiming_surfaces(
    tmp_path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "issue-182-status.md").write_text(
        "| B1 |\n| D1 |\n| P1 |\npower_loss not_claimed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.get_native_capability_matrix",
        lambda: {"version": 4, "profiles": []},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_recovery_report",
        lambda: {"capability_matrix_version": 4, "production_ready": True},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_checker_report",
        lambda _tests: {
            "entries": [{"profile": "fake", "parity": "pass", "evidence": "unknown"}]
        },
    )
    report = build_report(tmp_path)
    assert report["verdict"] == "blocked"
    assert report["blockers"]
