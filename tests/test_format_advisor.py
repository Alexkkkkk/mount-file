# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Focused tests for the metadata-only AI format advisor."""

import json

from mountfile.ai_format_advisor import (KNOWLEDGE_BASE, FileFingerprint,
                                         HypothesisEngine, Remember)


def test_fingerprint_uses_metadata_without_opening_file(monkeypatch):
    def fail_open(*args, **kwargs):
        raise AssertionError("the advisor must not open the target file")

    monkeypatch.setattr("builtins.open", fail_open)
    fingerprint = FileFingerprint(
        "/not/a/real/photo.PNG", 123, magic_summary="89 50 4e 47"
    )

    assert fingerprint.filename == "photo.PNG"
    assert fingerprint.extension == ".png"
    assert fingerprint.size == 123
    assert fingerprint.magic_summary == "89 50 4e 47"
    assert set(fingerprint.to_dict()) == {
        "filename",
        "size",
        "extension",
        "magic_summary",
    }


def test_known_format_returns_ranked_actionable_shortlist():
    fingerprint = FileFingerprint("archive.zip", 4096, magic_summary="50 4b 03 04")
    recipes = HypothesisEngine().suggest(fingerprint)

    assert len(recipes) == 5
    assert recipes[0]["id"] == "zip"
    assert recipes[0]["confidence"] > recipes[1]["confidence"]
    assert [recipe["rank"] for recipe in recipes] == [1, 2, 3, 4, 5]
    assert all(recipe["action"] and recipe["steps"] for recipe in recipes)
    assert len(KNOWLEDGE_BASE) >= 12


def test_unknown_format_still_has_safe_next_actions():
    fingerprint = FileFingerprint(
        "mystery.zzz", 17, magic_summary="not-a-known-signature"
    )
    recipes = HypothesisEngine().suggest(fingerprint)

    assert 3 <= len(recipes) <= 5
    assert recipes[0]["id"] == "metadata check"
    assert all(recipe["evidence"] for recipe in recipes)
    safety_text = " ".join(" ".join(recipe["steps"]).lower() for recipe in recipes)
    assert any(
        term in safety_text for term in ("read-only", "copy", "isolated", "disposable")
    )


def test_remember_persists_only_metadata_and_recipe(tmp_path):
    path = tmp_path / "ai_kb.json"
    fingerprint = FileFingerprint(
        "dump.bin", 2048, magic_summary="user supplied summary"
    )
    selected = {"format": "Custom dump", "action": "Inspect the header in a sandbox."}

    remember = Remember(path)
    remember.remember(fingerprint, selected)
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["entries"][0]["fingerprint"] == fingerprint.to_dict()
    assert "contents" not in json.dumps(payload)

    restored = Remember(path)
    assert restored.recall(fingerprint) == selected
    assert (
        HypothesisEngine(remember=restored).suggest(fingerprint)[0]["id"]
        == "remembered"
    )
