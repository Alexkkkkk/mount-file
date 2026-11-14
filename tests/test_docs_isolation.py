"""CI gate for the docs/proposals replay-draft isolation invariant (issue #510).

The static audit of 2026-11-14 (issue #510) proved that the replay drafts in
docs/proposals/ are markdown-only review material and that no runtime module
imports from docs/ or implements the draft's unsafe replay_if_needed().
These tests pin that property so it cannot silently degrade.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROPOSALS = ROOT / "docs" / "proposals"
RUNTIME_DIRS = [ROOT / "mountfile"]
GATE_FILE = Path(__file__).resolve()


def _repo_py_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if ".git" not in path.parts and "docs" not in path.parts and path != GATE_FILE
    ]


def test_proposals_contain_markdown_only() -> None:
    """No executable draft code may live under docs/proposals/."""
    assert PROPOSALS.is_dir(), "docs/proposals/ disappeared"
    stray = [
        path for path in PROPOSALS.rglob("*") if path.is_file() and path.suffix != ".md"
    ]
    assert not stray, f"non-markdown files in docs/proposals/: {stray}"


def test_replay_if_needed_is_not_implemented_anywhere() -> None:
    """The draft's unsafe replay_if_needed() must never reach any .py file."""
    offenders = [
        str(path)
        for path in _repo_py_files()
        if "replay_if_needed" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"replay_if_needed found in: {offenders}"


@pytest.mark.parametrize("runtime_dir", RUNTIME_DIRS, ids=lambda p: p.name)
def test_runtime_does_not_import_from_docs(runtime_dir: Path) -> None:
    """Runtime modules must not import anything from docs/."""
    pattern = re.compile(r"^\s*(?:from|import)\s+docs\b", re.MULTILINE)
    offenders = [
        str(path)
        for path in runtime_dir.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"docs imports found in runtime: {offenders}"
