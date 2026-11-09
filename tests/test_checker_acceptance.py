"""Tests for executed checker-fixture evidence reporting."""

from pathlib import Path

from scripts.checker_acceptance import _fixture_evidence, _fixture_nodes, _junit_results


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
          <testcase classname='a' name='test_pass[one]' time='0.25'>
            <properties><property name='fixture_sha256' value='abc123'/></properties>
          </testcase>
          <testcase classname='a' name='test_pass[two]' time='0.3'/>
          <testcase classname='b' name='test_skip' time='0.01'><skipped message='tool unavailable'/></testcase>
          <testcase classname='c' name='test_fail' time='0.5'><failure>bad fixture</failure></testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    results = _junit_results(report)
    assert [item["status"] for item in results["test_pass"]] == ["passed", "passed"]
    assert results["test_pass"][0]["duration_seconds"] == 0.25
    assert results["test_pass"][0]["properties"]["fixture_sha256"] == "abc123"
    assert results["test_skip"][0]["status"] == "skipped"
    assert results["test_fail"][0]["status"] == "failed"
    assert results["test_fail"][0]["detail"] == "bad fixture"


def test_fixture_evidence_requires_matching_source_and_checker_copy_hashes():
    image_hash = "a" * 64
    properties = {
        "mountfile.fixture.profile": "ext2-native-rw",
        "mountfile.fixture.image_size_bytes": "4096",
        "mountfile.fixture.sha256_before_checker": image_hash,
        "mountfile.fixture.sha256_after_checker": image_hash,
        "mountfile.checker.tool": "e2fsck",
        "mountfile.checker.verdict": "clean",
        "mountfile.checker.copy_sha256_before": image_hash,
        "mountfile.checker.copy_sha256_after": image_hash,
        "mountfile.checker.source_unchanged": "true",
        "mountfile.checker.copy_unchanged": "true",
    }
    results = [
        {"status": "passed", "case_name": "checker-test", "properties": properties}
    ]

    images, status = _fixture_evidence("ext2-native-rw", "e2fsck", results)

    assert status == "complete"
    assert images[0]["source_sha256_before_checker"] == image_hash
    assert images[0]["checker_copy_sha256_after"] == image_hash

    properties["mountfile.checker.copy_sha256_after"] = "b" * 64
    _images, changed_copy_status = _fixture_evidence(
        "ext2-native-rw", "e2fsck", results
    )
    assert changed_copy_status == "blocked"
