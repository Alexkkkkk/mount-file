"""Tests for executed checker-fixture evidence reporting."""

from pathlib import Path

from scripts.checker_acceptance import _fixture_nodes, _junit_results


def test_fixture_nodes_resolve_declared_test_functions():
    nodes = _fixture_nodes(Path("tests"))
    assert (
        nodes["test_native_ext2_mutation_passes_e2fsck_on_disposable_copy"]
        .replace("\\", "/")
        .endswith(
            "tests/test_independent_checkers.py::test_native_ext2_mutation_passes_e2fsck_on_disposable_copy"
        )
    )
    assert (
        nodes["test_real_mkfs_ntfs_is_never_declared_clean_by_ntfsfix"]
        .replace("\\", "/")
        .endswith(
            "tests/test_independent_checkers.py::test_real_mkfs_ntfs_is_never_declared_clean_by_ntfsfix"
        )
    )


def test_junit_results_preserve_pass_fail_skip_and_duration(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        """<testsuite>
          <testcase classname='a' name='test_pass' time='0.25'/>
          <testcase classname='b' name='test_skip' time='0.01'><skipped message='tool unavailable'/></testcase>
          <testcase classname='c' name='test_fail' time='0.5'><failure>bad fixture</failure></testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    results = _junit_results(report)
    assert results["test_pass"]["status"] == "passed"
    assert results["test_pass"]["duration_seconds"] == 0.25
    assert results["test_skip"]["status"] == "skipped"
    assert results["test_fail"]["status"] == "failed"
    assert results["test_fail"]["detail"] == "bad fixture"
