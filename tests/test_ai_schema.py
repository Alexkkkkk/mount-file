# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for structured AI output - no Qt, no network.

Run:  python -m pytest tests/test_ai_schema.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.ai.schema import (  # noqa: E402
    SCHEMAS,
    extract_json,
    guard_sql,
    parse_structured,
    validate,
)


def test_extract_json_from_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced_block():
    text = 'Вот результат:\n```json\n{"kind": "document"}\n```\nГотово.'
    assert extract_json(text) == {"kind": "document"}


def test_extract_json_ignores_braces_inside_strings():
    text = 'prefix {"summary": "brace } inside", "n": 2} suffix'
    assert extract_json(text) == {"summary": "brace } inside", "n": 2}


def test_extract_json_returns_none_for_prose():
    assert extract_json("Модель не смогла ответить.") is None


def test_extract_json_handles_unbalanced_input():
    assert extract_json('{"a": {"b": 1}') is None


def test_validate_reports_missing_required():
    errors = validate({"kind": "document"}, SCHEMAS["file_triage"])
    assert any("summary" in item for item in errors)
    assert any("risk" in item for item in errors)


def test_validate_reports_extra_field():
    obj = {"kind": "document", "summary": "x", "risk": "none", "bogus": 1}
    errors = validate(obj, SCHEMAS["file_triage"])
    assert any("bogus" in item for item in errors)


def test_validate_rejects_out_of_enum():
    obj = {"kind": "spaceship", "summary": "x", "risk": "none"}
    errors = validate(obj, SCHEMAS["file_triage"])
    assert any("перечислен" in item for item in errors)


def test_validate_accepts_good_object():
    obj = {
        "kind": "image",
        "summary": "JPEG photo",
        "risk": "none",
        "confidence": 0.9,
        "indicators": ["exif"],
    }
    assert validate(obj, SCHEMAS["file_triage"]) == []


def test_validate_checks_numeric_bounds():
    obj = {"kind": "document", "summary": "x", "risk": "none", "confidence": 1.5}
    errors = validate(obj, SCHEMAS["file_triage"])
    assert any("confidence" in item for item in errors)


def test_validate_checks_array_bounds():
    errors = validate(
        {"steps": [{"tool": "hash_file", "purpose": "x"}] * 9},
        SCHEMAS["investigation_plan"],
    )
    assert any("элементов больше" in item for item in errors)


def test_parse_structured_success():
    payload = json.dumps({"kind": "audio", "summary": "voice memo", "risk": "low"})
    obj, errors = parse_structured("```json\n%s\n```" % payload, SCHEMAS["file_triage"])
    assert errors == []
    assert obj["kind"] == "audio"


def test_parse_structured_reports_missing_json():
    obj, errors = parse_structured("no json here", SCHEMAS["file_triage"])
    assert obj is None
    assert errors == ["ответ модели не содержит JSON-объекта"]


def test_every_shipped_schema_is_an_object_with_required_fields():
    for name, schema in SCHEMAS.items():
        assert schema["type"] == "object", name
        assert schema.get("required"), name
        assert isinstance(schema.get("properties"), dict), name


def test_guard_sql_allows_select():
    assert guard_sql("SELECT path, mtime FROM events LIMIT 10") == []


def test_guard_sql_rejects_write():
    errors = guard_sql("DELETE FROM events")
    assert errors


def test_guard_sql_rejects_multiple_statements():
    errors = guard_sql("SELECT 1; DROP TABLE events")
    assert any("несколько" in item for item in errors)
