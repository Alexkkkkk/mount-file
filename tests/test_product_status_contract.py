"""Regression tests for the conservative product-status contract."""

import json

from scripts.product_status_contract import build_report


def _write_surface_fixtures(root):
    (root / "README.md").write_text(
        "| ext3/ext4 | Read-only | No | bounded |\n"
        "| NTFS | Read-only | No | bounded |\n"
        "Native ext3/ext4 and NTFS writes are unsupported; "
        "power-loss safety is unsupported.\n",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "## Unreleased\n"
        "Native ext3/ext4/NTFS writes remain unsupported; "
        "power-loss safety is unsupported.\n",
        encoding="utf-8",
    )
    package = root / "mountfile"
    package.mkdir(exist_ok=True)
    (package / "cli.py").write_text(
        "def cmd_capabilities(args):\n"
        "    matrix = get_native_capability_matrix()\n"
        "    print(json.dumps(matrix, ensure_ascii=False))\n"
        "def cmd_status(args):\n    pass\n"
        '"capabilities"\n',
        encoding="utf-8",
    )
    (package / "mainwindow.py").write_text(
        "def _set_write_enabled(self, on):\n"
        "    status = self.writer.capability_status()\n"
        '    state = operations.get(canonical, {}).get("state")\n'
        '    return state == "available"\n'
        "    def _set_loaded(self, loaded):\n        pass\n",
        encoding="utf-8",
    )


def test_product_status_contract_is_conservative_and_consistent():
    report = build_report()
    assert report["schema"] == 1
    assert report["verdict"] == "pass"
    assert report["blockers"] == []
    assert report["capability_matrix_version"] == 6
    assert report["recovery_production_ready"] is False
    assert report["claims"] == {
        "parity": "not_claimed",
        "power_loss": "unsupported",
        "native_ext3_ext4_write": "unsupported",
        "native_ntfs_write": "unsupported",
    }
    assert all(report["surface_contract"].values())
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


def test_product_status_contract_rejects_execution_artifact_with_false_pass(
    tmp_path, monkeypatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "issue-182-status.md").write_text(
        "| B1 |\n| D1 |\n| P1 |\npower_loss not_claimed\n", encoding="utf-8"
    )
    (tmp_path / "checker-acceptance.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "report": "mountfile-checker-execution-acceptance",
                "verdict": "pass",
                "pytest_exit_code": 1,
                "blockers": [],
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.get_native_capability_matrix",
        lambda: {"version": 5, "profiles": []},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_recovery_report",
        lambda: {"capability_matrix_version": 5, "production_ready": False},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_checker_report",
        lambda _tests: {
            "entries": [
                {
                    "profile": "ext2",
                    "parity": "not_claimed",
                    "evidence": "fixture_required",
                }
            ]
        },
    )

    report = build_report(tmp_path)
    assert report["verdict"] == "blocked"
    assert (
        "mountfile-checker-execution-acceptance execution artifact has a nonzero pytest exit"
        in report["blockers"]
    )
    assert (
        "mountfile-checker-execution-acceptance execution artifact coverage is incomplete"
        in report["blockers"]
    )


def test_product_status_contract_accepts_only_complete_executed_coverage(
    tmp_path, monkeypatch
):
    _write_surface_fixtures(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "issue-182-status.md").write_text(
        "| B1 |\n| D1 |\n| P1 |\npower_loss not_claimed\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.get_native_capability_matrix",
        lambda: {"version": 6, "profiles": []},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_recovery_report",
        lambda: {"capability_matrix_version": 6, "production_ready": False},
    )
    checker_entries = [
        {"profile": "ext2", "parity": "not_claimed", "evidence": "fixture_required"}
    ]
    monkeypatch.setattr(
        "scripts.product_status_contract.build_checker_report",
        lambda _tests: {"entries": checker_entries},
    )
    (tmp_path / "checker-acceptance.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "report": "mountfile-checker-execution-acceptance",
                "verdict": "pass",
                "pytest_exit_code": 0,
                "blockers": [],
                "evidence": "executed_fixture_results",
                "entries": [
                    {
                        "profile": "ext2",
                        "fixture_status": "passed",
                        "evidence": "executed_fixture_required",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    recovery_groups = ["fat12-16", "ext2", "fat32", "exfat"]
    (tmp_path / "recovery-acceptance.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "report": "mountfile-recovery-execution-acceptance",
                "verdict": "pass",
                "pytest_exit_code": 0,
                "blockers": [],
                "entries": [
                    {
                        "profile_group": group,
                        "fixture_status": "passed",
                        "evidence": "executed_recovery_fixture_results",
                    }
                    for group in recovery_groups
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_report(tmp_path)
    assert report["verdict"] == "pass"
    assert report["checker_acceptance_verdict"] == "pass"
    assert report["recovery_acceptance_verdict"] == "pass"


def test_product_status_contract_blocks_readme_writer_overclaim(tmp_path, monkeypatch):
    _write_surface_fixtures(tmp_path)
    (tmp_path / "README.md").write_text(
        "| ext3/ext4 | Read-write | Yes |\n| NTFS | Read-only | No |\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "issue-182-status.md").write_text(
        "| B1 |\n| D1 |\n| P1 |\npower_loss not_claimed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.get_native_capability_matrix",
        lambda: {"version": 5, "profiles": []},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_recovery_report",
        lambda: {"capability_matrix_version": 5, "production_ready": False},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_checker_report",
        lambda _tests: {"entries": []},
    )
    report = build_report(tmp_path)
    assert report["verdict"] == "blocked"
    assert "README advertises an unsupported native write profile" in report["blockers"]


def test_product_status_contract_blocks_gui_write_controls_bypassing_matrix(
    tmp_path, monkeypatch
):
    _write_surface_fixtures(tmp_path)
    gui = tmp_path / "mountfile" / "mainwindow.py"
    gui.write_text(
        "def _set_write_enabled(self, on):\n"
        "    self.btn_import.setEnabled(on)\n"
        "    def _set_loaded(self, loaded):\n        pass\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "issue-182-status.md").write_text(
        "| B1 |\n| D1 |\n| P1 |\npower_loss not_claimed\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.get_native_capability_matrix",
        lambda: {"version": 5, "profiles": []},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_recovery_report",
        lambda: {"capability_matrix_version": 5, "production_ready": False},
    )
    monkeypatch.setattr(
        "scripts.product_status_contract.build_checker_report",
        lambda _tests: {"entries": []},
    )
    report = build_report(tmp_path)
    assert report["verdict"] == "blocked"
    assert (
        "GUI write controls are not driven by capability status" in report["blockers"]
    )
