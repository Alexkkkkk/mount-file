# SPDX-License-Identifier: MIT
"""Issue #510, D6: typed acceptance cells without hidden skips or inference.

Every acceptance cell published in ``docs/generated/recovery-acceptance.json``
must carry a typed verdict from the closed vocabulary.  A ``pass`` cell must
cite a reproducible evidence label (``tested_*``); anything else stays
explicitly ``not_proven``/``unavailable``.  Unproven cells surface as
*skips with an explicit typed reason* -- visible in the JUnit report -- and
never as silent passes or silent omissions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.recovery_matrix import build_acceptance_report

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARTIFACT = _REPO_ROOT / "docs" / "generated" / "recovery-acceptance.json"

_TYPED_VERDICTS = {"pass", "unavailable", "not_proven", "blocked", "not_collected", "missing"}
_REPRODUCIBLE_PREFIX = "tested_"


def _cells(obj: object):
    """Yield every dict that looks like an acceptance cell, recursively."""

    if isinstance(obj, dict):
        keys = set(obj)
        if keys & {"status", "verdict"} and keys & {
            "cell",
            "operation",
            "state",
            "profile",
            "profile_id",
            "source",
        }:
            yield obj
        for value in obj.values():
            yield from _cells(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _cells(value)


def _verdict(cell: dict) -> str:
    for key in ("verdict", "status"):
        if key in cell and isinstance(cell[key], str):
            return cell[key]
    return "missing"


def test_artifact_exists_and_matches_the_trusted_generator():
    committed = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    regenerated = build_acceptance_report()
    assert committed == json.loads(json.dumps(regenerated, sort_keys=True))


def test_every_published_cell_carries_a_typed_verdict():
    cells = list(_cells(json.loads(_ARTIFACT.read_text(encoding="utf-8"))))
    assert cells, "acceptance artifact must publish cells"
    for cell in cells:
        verdict = _verdict(cell)
        assert verdict in _TYPED_VERDICTS, (
            f"untyped cell verdict {verdict!r} in {_ARTIFACT.name}"
        )


def test_pass_cells_cite_reproducible_evidence_only():
    for cell in _cells(json.loads(_ARTIFACT.read_text(encoding="utf-8"))):
        if _verdict(cell) != "pass":
            continue
        source = cell.get("source")
        assert isinstance(source, str) and source.startswith(_REPRODUCIBLE_PREFIX), (
            f"pass cell without a reproducible 'tested_*' source: {source!r}"
        )


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(cell, id=f"{_verdict(cell)}:{cell.get('cell') or cell.get('source')}")
        for cell in _cells(json.loads(_ARTIFACT.read_text(encoding="utf-8")))
        if _verdict(cell) != "pass"
    ],
)
def test_unproven_cells_surface_with_explicit_typed_reasons(cell):
    verdict = _verdict(cell)
    label = cell.get("cell") or cell.get("source") or "<unnamed>"
    pytest.skip(f"not proven ({verdict}): {label}")
