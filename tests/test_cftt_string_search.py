# SPDX-License-Identifier: MIT
"""Optional fixture-backed string-search check for a user-supplied CFTT image.

The fixture is never downloaded by the test. Set both CFTT_STRING_IMAGE and
CFTT_EXPECTED_JSON to opt in. The expected JSON maps each literal string to one
byte offset or a list of expected byte offsets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mountfile.search_content import search_image

CFTT_IMAGE = os.environ.get("CFTT_STRING_IMAGE")
CFTT_EXPECTED = os.environ.get("CFTT_EXPECTED_JSON")


def _normalize_offsets(value: object) -> tuple[int, ...]:
    if type(value) is int:
        offsets = (value,)
    elif isinstance(value, list) and value and all(type(item) is int for item in value):
        offsets = tuple(value)
    else:
        raise ValueError("expected offset must be an integer or non-empty integer list")
    if any(offset < 0 for offset in offsets):
        raise ValueError("expected offsets must be non-negative")
    return tuple(sorted(set(offsets)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(12, (12,)), ([12, 48, 12], (12, 48))],
)
def test_normalize_expected_offsets(value, expected):
    assert _normalize_offsets(value) == expected


@pytest.mark.parametrize("value", [True, -1, [], [1, False], "12"])
def test_normalize_expected_offsets_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        _normalize_offsets(value)


@pytest.mark.skipif(
    not CFTT_IMAGE and not CFTT_EXPECTED,
    reason="set CFTT_STRING_IMAGE and CFTT_EXPECTED_JSON to run the external fixture",
)
def test_cftt_string_search_recall():
    if not CFTT_IMAGE or not CFTT_EXPECTED:
        pytest.fail("both CFTT_STRING_IMAGE and CFTT_EXPECTED_JSON must be set")

    image_path = Path(CFTT_IMAGE)
    expected_path = Path(CFTT_EXPECTED)
    assert image_path.is_file(), f"CFTT image not found: {image_path}"
    assert expected_path.is_file(), f"CFTT expected JSON not found: {expected_path}"

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert (
        isinstance(expected, dict) and expected
    ), "expected JSON must be a non-empty object"

    missing: dict[str, list[int]] = {}
    for pattern, raw_offsets in expected.items():
        assert (
            isinstance(pattern, str) and pattern
        ), "expected strings must be non-empty"
        offsets = _normalize_offsets(raw_offsets)
        found_offsets = {
            hit.offset for hit in search_image(image_path, pattern, context=0)
        }
        absent = sorted(set(offsets) - found_offsets)
        if absent:
            missing[pattern] = absent

    assert not missing, f"search_image missed expected CFTT matches: {missing}"
