"""Tests for executed writer recovery evidence reporting."""

from pathlib import Path

from scripts.recovery_acceptance import FIXTURE_GROUPS, _fixture_nodes, _junit_results


def test_all_recovery_fixture_declarations_resolve():
    nodes = _fixture_nodes(Path("tests"))
    declared = [name for names in FIXTURE_GROUPS.values() for name in names]
    assert all(name in nodes for name in declared)
    assert all("::" in nodes[name] for name in declared)


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
