# SPDX-License-Identifier: MIT
"""C6/E1 contract tests for the authoritative-checker row.

Three checker states, three distinct outcomes: an absent checker is typed
``unavailable`` (never ``pass``), a checker that ran but cannot establish
parity is a typed ``error`` (never ``pass``, never ``diverged``), and only a
performed comparison can be ``pass`` or ``diverged``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mountfile.acceptance.checker_row import (
    C6_DECLARED_TEST_IDS,
    DIVERGED,
    ERROR,
    EXIT_CODES,
    PASS,
    UNAVAILABLE,
    UNKNOWN,
    build_row,
    classify_parity,
    exit_code,
    inventory,
    select_cases,
)
from scripts.failure_matrix_artifact import build_report

ROW_KEYS = {
    "schema",
    "acceptance_item",
    "checker",
    "authoritative",
    "secondary_checkers",
    "checker_state",
    "outcome",
    "parity",
    "detail",
    "provenance",
    "supported_checks",
    "unsupported_checks",
    "exit_code",
}


def _junit(path: Path, node_ids: list[str], status: str = "passed") -> Path:
    cases = []
    for node_id in node_ids:
        classname, _, name = node_id.partition("::")
        classname = classname.strip("/").replace("/", ".").removesuffix(".py")
        outcome = f"<{status}/>" if status != "passed" else ""
        cases.append(
            f'<testcase classname="{classname}" name="{name}">{outcome}</testcase>'
        )
    path.write_text(
        '<testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "checker_state",
    ["available", "missing", "error"],
    ids=["checker-available", "checker-missing", "checker-error"],
)
def test_c6_checker_row_covers_each_checker_state(checker_state):
    row = build_row(
        checker_state=checker_state,
        differences={"mft": False, "mftmirr": False, "bitmap": False},
        captured_at="2026-11-23T00:00:00Z",
    )

    assert set(row) == ROW_KEYS
    assert row["acceptance_item"] == "C6"
    assert row["authoritative"] is True

    if checker_state == "available":
        # The comparison really happened: only pass/diverged are reachable.
        assert row["outcome"] in {PASS, DIVERGED}
        assert row["parity"] == PASS
        assert exit_code(row) == EXIT_CODES[PASS] == 0
    elif checker_state == "missing":
        # Typed unavailable: exit 0, and never a pass.
        assert row["outcome"] == UNAVAILABLE
        assert row["parity"] == "not_computed"
        assert row["outcome"] != PASS
        assert row["detail"]
        assert exit_code(row) == 0
    else:
        # Typed refusal: not pass, not diverged, non-zero exit.
        assert row["outcome"] == ERROR
        assert row["outcome"] not in {PASS, DIVERGED}
        assert row["parity"] == "not_computed"
        assert exit_code(row) == 1


def test_ntfsfix_stays_secondary_and_never_satisfies_parity():
    row = build_row(
        checker_state="available",
        check_logfile_state="error",
        differences={"mft": False},
    )

    assert row["checker"] == "chkdsk"
    assert row["authoritative"] is True
    secondary = {item["checker"]: item for item in row["secondary_checkers"]}
    assert secondary["ntfsfix"]["authoritative"] is False
    assert secondary["ntfsfix"]["may_satisfy_parity"] is False
    # D5: ntfsfix -n ran and returned ERROR. That refuses parity, not grants it.
    assert row["outcome"] == ERROR
    assert row["outcome"] != PASS
    assert exit_code(row) == 1


def test_row_carries_inline_provenance_and_explicit_unsupported_checks():
    row = build_row(
        checker_state="available",
        differences={"mft": False, "bitmap": False},
        captured_at="2026-11-23T00:00:00Z",
    )

    provenance = row["provenance"]
    assert set(provenance) == {
        "checker_version",
        "checker_version_state",
        "inventory_sha256",
        "captured_at",
    }
    assert provenance["inventory_sha256"] == inventory()["inventory_sha256"]
    assert provenance["inventory_sha256"]
    assert provenance["captured_at"] == "2026-11-23T00:00:00Z"
    assert row["unsupported_checks"]
    assert "mft_allocation_invariants" in row["unsupported_checks"]
    assert not set(row["supported_checks"]) & set(row["unsupported_checks"])


def test_c6_negative_parity_case_reports_diverged():
    row = build_row(checker_state="available", differences={"mft": True})

    assert row["outcome"] == DIVERGED
    assert row["parity"] == DIVERGED
    assert row["outcome"] != PASS
    assert exit_code(row) == 2


def test_c6_unattributed_difference_is_unknown_and_fails_the_job():
    row = build_row(
        checker_state="available",
        differences={"mft": True},
        unattributed=("mft:resident_attribute_flags",),
    )

    assert row["outcome"] == UNKNOWN
    assert row["outcome"] != PASS
    assert exit_code(row) != 0
    assert classify_parity({"mft": False}, unattributed=("x",)) == UNKNOWN


def test_available_checker_without_a_comparison_is_not_a_pass():
    row = build_row(checker_state="available")

    assert row["outcome"] == ERROR
    assert row["parity"] == "not_computed"
    assert exit_code(row) != 0


def test_declared_c6_ids_resolve_in_this_module():
    declared_file = C6_DECLARED_TEST_IDS[0].split("::")[0].split("/")[-1]
    assert declared_file == Path(__file__).name
    for node_id in C6_DECLARED_TEST_IDS:
        name = node_id.split("::", 1)[1].split("[", 1)[0]
        assert name in globals(), f"declared C6 node id points at missing {name}"


def test_reformatted_param_id_is_not_proven_rather_than_green():
    declared = [C6_DECLARED_TEST_IDS[0]]
    reformatted = [
        {
            "node_id": "tests.test_c6_checker_row_contract::"
            "test_c6_checker_row_covers_each_checker_state[available]",
            "status": "passed",
        }
    ]

    result = select_cases(declared, reformatted)

    assert result["status"] == "not_proven"
    assert result["evidence"] == []
    assert result["missing"] == declared


def test_exact_declared_ids_are_matched_as_evidence():
    case = {"node_id": C6_DECLARED_TEST_IDS[1], "status": "passed"}

    result = select_cases([C6_DECLARED_TEST_IDS[1]], [case])

    assert result["status"] == "passed"
    assert result["evidence"] == [C6_DECLARED_TEST_IDS[1]]


def test_matrix_row_is_present_and_never_claims_parity_without_chkdsk(tmp_path):
    junit = _junit(tmp_path / "c6.xml", [C6_DECLARED_TEST_IDS[0]])

    report = build_report(tmp_path, junit)
    row = report["c6_checker_row"]
    evidence = row["test_evidence"]

    assert set(ROW_KEYS) <= set(row)
    assert row["acceptance_item"] == "C6"
    assert report["production_ready"] is False
    assert row["outcome"] != PASS or row["provenance"]["checker_version_state"]
    assert evidence["status"] in {"passed", "partial", "not_proven"}
    assert evidence["status"] != "passed" or len(evidence["evidence"]) == 1


def test_matrix_row_exit_code_matches_declared_contract(tmp_path):
    junit = _junit(tmp_path / "empty-c6.xml", [])
    row = build_report(tmp_path, junit)["c6_checker_row"]

    assert row["exit_code"] == EXIT_CODES[row["outcome"]]
    assert json.loads(json.dumps(row)) == row
    if row["outcome"] == UNAVAILABLE:
        assert row["exit_code"] == 0
