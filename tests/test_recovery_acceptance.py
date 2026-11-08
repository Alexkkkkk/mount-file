"""Tests for executed writer recovery evidence reporting."""

from pathlib import Path

import pytest

from scripts.recovery_acceptance import (
    CELL_FIXTURES,
    FIXTURE_GROUPS,
    _fixture_nodes,
    _junit_results,
)


def test_all_recovery_fixture_declarations_resolve():
    nodes = _fixture_nodes(Path("tests"))
    declared = [name for names in FIXTURE_GROUPS.values() for name in names]
    assert all(name in nodes for name in declared)
    assert all("::" in nodes[name] for name in declared)


def test_typed_recovery_cells_cover_writers_and_keep_power_loss_unsupported():
    assert set(FIXTURE_GROUPS) == {"fat12-16", "fat32", "ext2", "exfat"}
    nodes = _fixture_nodes(Path("tests"))
    for cells in CELL_FIXTURES.values():
        assert all(name in nodes for names in cells.values() for name in names)
    assert "hash_preservation" in CELL_FIXTURES["fat12-16"]
    assert "quarantine" in CELL_FIXTURES["fat32"]
    assert "durable_recovery" in CELL_FIXTURES["ext2"]
    assert all("independent_checker" in cells for cells in CELL_FIXTURES.values())


def test_junit_results_aggregate_parameterized_cases_without_losing_failures(
    tmp_path,
):
    report = tmp_path / "recovery-parameterized.xml"
    report.write_text(
        """<testsuite>
          <testcase classname='a' name='test_fat[12]' time='0.2'>
            <failure message='fat12 failed'>FAT12 corruption</failure>
          </testcase>
          <testcase classname='a' name='test_fat[16]' time='0.4'/>
          <testcase classname='b' name='test_tool[missing]' time='0.3'>
            <skipped message='checker unavailable'/>
          </testcase>
          <testcase classname='b' name='test_tool[available]' time='0.5'/>
        </testsuite>""",
        encoding="utf-8",
    )

    results = _junit_results(report)

    assert results["test_fat"]["status"] == "failed"
    assert results["test_fat"]["detail"] == "FAT12 corruption"
    assert results["test_fat"]["duration_seconds"] == pytest.approx(0.6)
    assert results["test_tool"]["status"] == "skipped"
    assert results["test_tool"]["detail"] == "checker unavailable"
    assert results["test_tool"]["duration_seconds"] == pytest.approx(0.8)


def test_junit_results_preserve_recovery_statuses(tmp_path):
    report = tmp_path / "recovery.xml"
    report.write_text(
        """<testsuite>
          <testcase classname='a' name='test_recovery' time='0.4'/>
          <testcase classname='b' name='test_quarantine' time='0.2'><skipped message='not available'/></testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    results = _junit_results(report)
    assert results["test_recovery"]["status"] == "passed"
    assert results["test_quarantine"]["status"] == "skipped"
    assert results["test_recovery"]["duration_seconds"] == 0.4
