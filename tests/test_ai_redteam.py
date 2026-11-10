# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Offline red-team and property tests for the AI prompt guard.

These tests use only synthetic strings and local code. They never call a model,
read user evidence, or require provider credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mountfile.ai.guard import (
    FENCE_CLOSE,
    FENCE_OPEN,
    LEVELS,
    MAX_SCAN_BYTES,
    fence,
    scan,
)

CORPUS_PATH = Path(__file__).parent / "fixtures" / "ai_redteam_cases.json"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case["id"])
def test_guard_red_team_corpus(case):
    report = scan(case["text"].encode("utf-8"))

    assert report.risk == case["risk"]
    assert report.risk in LEVELS
    if case.get("required_rule"):
        assert case["required_rule"] in report.rules


@settings(max_examples=75, deadline=None)
@given(st.binary(max_size=4096))
def test_scan_is_total_and_counts_original_bytes(data):
    report = scan(data)

    assert report.bytes_scanned == len(data)
    assert report.truncated is False
    assert report.risk in LEVELS
    assert len(report.hits) <= 40


def test_scan_counts_invalid_utf8_as_original_bytes():
    assert scan(b"\xff").bytes_scanned == 1


def test_scan_truncation_uses_raw_byte_count_for_invalid_utf8():
    data = b"\xff" * (MAX_SCAN_BYTES + 8)

    report = scan(data)

    assert report.bytes_scanned == MAX_SCAN_BYTES
    assert report.truncated is True


@settings(max_examples=75, deadline=None)
@given(st.text(max_size=256), st.text(max_size=1024))
def test_fence_markers_cannot_be_forged_by_name_or_payload(name, body):
    wrapped = fence(name, body)

    assert wrapped.startswith(FENCE_OPEN)
    assert wrapped.endswith(FENCE_CLOSE)
    assert wrapped.count(FENCE_OPEN) == 1
    assert wrapped.count(FENCE_CLOSE) == 1
