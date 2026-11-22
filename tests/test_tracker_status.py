# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for scripts/tracker_status.py and scripts/new_acceptance_item.py.

These generators decide what the #510 thread claims, so the properties that
matter are the refusals: a manifest that cites a file nobody created, an item id
declared twice, and a comment that would repeat the previous one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tracker_status = _load("tracker_status")
new_acceptance_item = _load("new_acceptance_item")


def _manifest(tmp_path: Path, items) -> Path:
    manifest = {
        "schema": 1,
        "tracker": 510,
        "slice": {
            "branch": "main",
            "commit": "abc1234",
            "version": "1.0.0",
            "checked": "2026-11-22",
        },
        "items": items,
    }
    path = tmp_path / "docs" / "replay-acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_tracker_status_counts_and_markdown(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    _manifest(
        tmp_path,
        [
            {
                "id": "A1",
                "block": "A",
                "title": "ok",
                "status": "verified",
                "evidence": ["tests/test_a.py"],
            },
            {
                "id": "B1",
                "block": "B",
                "title": "partial",
                "status": "partial",
                "evidence": ["tests/test_a.py"],
            },
            {
                "id": "C1",
                "block": "C",
                "title": "todo",
                "status": "not_started",
                "evidence": [],
            },
        ],
    )

    manifest = tracker_status.load_manifest(
        tmp_path / "docs" / "replay-acceptance.json"
    )
    counts = tracker_status.counts_by_status(manifest)
    assert counts["verified"] == 1
    assert counts["partial"] == 1
    assert counts["not_started"] == 1
    assert counts["total"] == 3
    assert tracker_status.collect_drift(manifest, tmp_path) == []

    block = tracker_status.render_markdown(
        manifest, [], {"commit": "def5678", "subject": "x"}
    )
    assert "✅ 1 · ⚠️ 1 · ⬜ 1 · 🚫 0 · всего 3" in block
    assert "`tests/test_a.py`" in block


def test_tracker_status_detects_missing_evidence_and_duplicate_ids(tmp_path):
    _manifest(
        tmp_path,
        [
            {
                "id": "A1",
                "block": "A",
                "title": "gone",
                "status": "verified",
                "evidence": ["tests/test_missing.py"],
            },
            {
                "id": "A1",
                "block": "A",
                "title": "again",
                "status": "verified",
                "evidence": ["tests/test_missing.py"],
            },
            {
                "id": "A2",
                "block": "A",
                "title": "todo with evidence",
                "status": "not_started",
                "evidence": ["tests/test_missing.py"],
            },
        ],
    )
    manifest = tracker_status.load_manifest(
        tmp_path / "docs" / "replay-acceptance.json"
    )
    drift = "\n".join(tracker_status.collect_drift(manifest, tmp_path))
    assert "evidence file missing: tests/test_missing.py" in drift
    assert "declared 2 times" in drift
    assert "must not cite evidence" in drift


def test_tracker_status_similarity_gate(tmp_path):
    (tmp_path / "tests").mkdir()
    _manifest(
        tmp_path,
        [
            {
                "id": "A1",
                "block": "A",
                "title": "ok",
                "status": "not_started",
                "evidence": [],
            }
        ],
    )
    block = tracker_status.render_markdown(
        tracker_status.load_manifest(tmp_path / "docs" / "replay-acceptance.json"),
        [],
        {"commit": "def5678", "subject": "x"},
    )
    previous = tmp_path / "last.md"
    previous.write_text(block, encoding="utf-8")
    assert tracker_status.similarity(block, previous.read_text(encoding="utf-8")) > 0.99
    assert tracker_status.normalize("A  b\n\nc") == "a b c"


def test_new_acceptance_item_refuses_duplicate_and_unknown_block(tmp_path):
    _manifest(
        tmp_path,
        [
            {
                "id": "C6",
                "block": "C",
                "title": "x",
                "status": "not_started",
                "evidence": [],
            }
        ],
    )

    duplicate = new_acceptance_item.main(
        ["--repo-root", str(tmp_path), "--id", "C6", "--block", "C", "--title", "dup"]
    )
    assert duplicate == 1

    mismatched = new_acceptance_item.main(
        ["--repo-root", str(tmp_path), "--id", "C7", "--block", "B", "--title", "bad"]
    )
    assert mismatched == 1

    missing_evidence = new_acceptance_item.main(
        [
            "--repo-root",
            str(tmp_path),
            "--id",
            "C7",
            "--block",
            "C",
            "--title",
            "needs file",
            "--status",
            "verified",
            "--existing-test",
            "--test-tests",
            "tests/test_absent.py",
        ]
    )
    assert missing_evidence == 1


def test_new_acceptance_item_dry_run_is_side_effect_free(tmp_path):
    manifest_path = _manifest(tmp_path, [])
    before = manifest_path.read_text(encoding="utf-8")

    code = new_acceptance_item.main(
        [
            "--repo-root",
            str(tmp_path),
            "--id",
            "D9",
            "--block",
            "D",
            "--title",
            "new matrix cell",
            "--test-tests",
            "tests/test_d9.py",
            "--dry-run",
        ]
    )
    assert code == 0
    assert manifest_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "tests" / "test_d9.py").exists()


@pytest.mark.parametrize(
    "raw,expected",
    [("C6", "c6"), ("B 10", "b_10"), ("---", "item"), ("a b c", "a_b_c")],
)
def test_slugify(raw, expected):
    assert new_acceptance_item.slugify(raw) == expected
