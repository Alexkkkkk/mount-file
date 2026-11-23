# SPDX-License-Identifier: MIT
"""Issue #510: the open-item gap register must agree with the manifest.

``docs/replay-acceptance.json`` owns the statuses; ``docs/replay-acceptance-gaps.json``
owns the explanation of every status that is not ``verified``.  Neither file can
be checked alone: a ``partial`` with no stated gap is a silent hole, and a gap
entry left behind for a closed item would silently cover the same regression if
it came back.  These tests pin both directions, the file-existence requirement
for evidence, and the machine-readable output the tracker block is built from.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "replay_acceptance_gaps",
    _REPO_ROOT / "scripts" / "replay_acceptance_gaps.py",
)
assert _spec is not None and _spec.loader is not None
gaps_gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gaps_gate
_spec.loader.exec_module(gaps_gate)

DEFAULT_GAPS = gaps_gate.DEFAULT_GAPS
DEFAULT_MANIFEST = gaps_gate.DEFAULT_MANIFEST
evaluate = gaps_gate.evaluate
load_gaps = gaps_gate.load_gaps
load_manifest = gaps_gate.load_manifest
main = gaps_gate.main
render_markdown = gaps_gate.render_markdown
summarise = gaps_gate.summarise


def _item(ident: str, status: str, evidence: list[str] | None = None) -> dict:
    return {
        "id": ident,
        "block": ident[0],
        "title": f"item {ident}",
        "status": status,
        "evidence": evidence or ["README.md"],
    }


def _gap(
    ident: str,
    status: str = "partial",
    *,
    blocker: str = "пробел описан достаточно подробно для ревьюера",
    closes_when: str = "появится независимое доказательство свойства",
    owner_issue: int | None = 387,
    evidence: list[str] | None = None,
) -> dict:
    entry: dict = {
        "id": ident,
        "status": status,
        "blocker": blocker,
        "closes_when": closes_when,
        "evidence": evidence or ["README.md"],
    }
    if owner_issue is not None:
        entry["owner_issue"] = owner_issue
    return entry


def _kinds(problems) -> list[str]:
    return [problem.kind for problem in problems]


def test_repository_gap_register_matches_the_manifest() -> None:
    """The shipped files must agree; this is the gate the tracker relies on."""

    items = load_manifest(DEFAULT_MANIFEST)
    gaps = load_gaps(DEFAULT_GAPS)

    assert evaluate(items, gaps, _REPO_ROOT) == []


def test_every_open_item_in_the_manifest_has_a_registered_gap() -> None:
    items = load_manifest(DEFAULT_MANIFEST)
    registered = {gap["id"] for gap in load_gaps(DEFAULT_GAPS)}
    open_items = {item["id"] for item in items if item["status"] != "verified"}

    assert open_items, "у #510 должен оставаться хотя бы один открытый пункт"
    assert open_items == registered


def test_gap_register_never_claims_a_closed_item() -> None:
    """A verified item must not carry a gap entry: that would mask a regression."""

    items = load_manifest(DEFAULT_MANIFEST)
    verified = {item["id"] for item in items if item["status"] == "verified"}
    registered = {gap["id"] for gap in load_gaps(DEFAULT_GAPS)}

    assert verified & registered == set()


@pytest.mark.parametrize(
    "item_status",
    ["partial", "not_started", "blocked"],
)
def test_missing_gap_for_an_open_item_is_reported(item_status: str) -> None:
    problems = evaluate([_item("X1", item_status)], [], _REPO_ROOT)

    assert _kinds(problems) == ["missing_gap"]
    assert problems[0].ident == "X1"


def test_gap_for_a_verified_item_is_reported_as_stale() -> None:
    problems = evaluate([_item("X1", "verified")], [_gap("X1")], _REPO_ROOT)

    assert "stale_gap" in _kinds(problems)


def test_status_disagreement_between_files_is_reported() -> None:
    problems = evaluate(
        [_item("X1", "partial")], [_gap("X1", status="blocked")], _REPO_ROOT
    )

    assert _kinds(problems) == ["status_mismatch"]


def test_unknown_item_reference_is_reported() -> None:
    problems = evaluate([_item("X1", "partial")], [_gap("X1"), _gap("Z9")], _REPO_ROOT)

    assert "unknown_item" in _kinds(problems)


def test_duplicate_gap_entry_is_reported() -> None:
    problems = evaluate(
        [_item("X1", "partial")], [_gap("X1"), _gap("X1")], _REPO_ROOT
    )

    assert "duplicate" in _kinds(problems)


@pytest.mark.parametrize("field", ["blocker", "closes_when"])
def test_one_word_reason_is_rejected(field: str) -> None:
    entry = _gap("X1")
    entry[field] = "нет"
    problems = evaluate([_item("X1", "partial")], [entry], _REPO_ROOT)

    assert _kinds(problems) == ["vague_reason"]
    assert field in problems[0].message


def test_owner_issue_must_be_an_issue_number() -> None:
    problems = evaluate(
        [_item("X1", "partial")], [_gap("X1", owner_issue=None)], _REPO_ROOT
    )

    assert _kinds(problems) == ["vague_reason"]
    assert "owner_issue" in problems[0].message


def test_gap_without_evidence_files_is_rejected() -> None:
    entry = _gap("X1")
    entry["evidence"] = []
    problems = evaluate([_item("X1", "partial")], [entry], _REPO_ROOT)

    assert _kinds(problems) == ["no_evidence"]


def test_evidence_path_must_exist_in_the_tree(tmp_path: Path) -> None:
    problems = evaluate(
        [_item("X1", "partial")],
        [_gap("X1", evidence=["docs/does-not-exist.md"])],
        tmp_path,
    )

    assert _kinds(problems) == ["missing_evidence"]


def test_markdown_lists_open_items_in_manifest_order() -> None:
    items = [_item("C3", "partial"), _item("A1", "verified"), _item("E1", "partial")]
    gaps = [_gap("E1", owner_issue=389), _gap("C3", owner_issue=386)]

    block = render_markdown(items, gaps)

    assert "| C3 |" in block and "| E1 |" in block
    assert "A1" not in block
    assert block.index("C3") < block.index("E1")
    assert "#386" in block and "#389" in block


def test_summary_counts_cover_every_status() -> None:
    items = [_item("A1", "verified"), _item("C3", "partial"), _item("C4", "blocked")]

    counts = summarise(items)

    assert counts == {"verified": 1, "partial": 1, "blocked": 1, "total": 3}


def test_cli_check_passes_on_the_repository(tmp_path: Path, capsys) -> None:
    exit_code = main(["--check"])

    assert exit_code == 0
    assert "расхождений нет" in capsys.readouterr().out


def test_cli_check_fails_when_a_gap_is_missing(tmp_path: Path, capsys) -> None:
    manifest = tmp_path / "manifest.json"
    gaps = tmp_path / "gaps.json"
    manifest.write_text(
        json.dumps({"items": [_item("X1", "partial"), _item("X2", "partial")]}),
        encoding="utf-8",
    )
    gaps.write_text(json.dumps({"gaps": [_gap("X1")]}), encoding="utf-8")

    exit_code = main(
        [
            "--check",
            "--manifest",
            str(manifest),
            "--gaps",
            str(gaps),
            "--root",
            str(_REPO_ROOT),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "missing_gap" in output
    assert "X2" in output


def test_cli_json_reports_pass(capsys) -> None:
    exit_code = main(["--json", "--check"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["verdict"] == "pass"
    assert payload["problems"] == []
    assert payload["open_items"] == len(load_gaps(DEFAULT_GAPS))


def test_gate_module_is_importable_as_a_script_name() -> None:
    assert gaps_gate.DEFAULT_GAPS.name == "replay-acceptance-gaps.json"
