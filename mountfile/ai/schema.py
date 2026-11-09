# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""JSON-Schema contracts for structured model output.

A dependency-free validator is used instead of ``jsonschema`` so a forensic
workstation can stay offline and the PyInstaller build keeps its size.  Only the
keywords the shipped schemas need are implemented: ``type``, ``const``,
``enum``, ``required``, ``properties``, ``additionalProperties``, ``items``,
``minItems``, ``maxItems``, ``minimum``, ``maximum``.

Typical use::

    from mountfile.ai import SCHEMAS, parse_structured
    result = backend.analyze("dump.bin", data, schema=SCHEMAS["file_triage"])
    obj, errors = parse_structured(result.text, SCHEMAS["file_triage"])

``parse_structured`` never raises: a model that answers with prose or with a
fenced code block is normal, so the text is mined for the first JSON value and
the remaining violations are returned for the caller to display.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

#: Longest model answer inspected for JSON.
MAX_TEXT = 256 * 1024

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _balanced_at(text: str, start: int) -> Optional[str]:
    """Return the balanced ``{...}``/``[...]`` block starting at ``start``."""

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : pos + 1]
    return None


def _first_balanced(text: str) -> Optional[str]:
    """Return the balanced block at the *first* bracket, or ``None``.

    Anchoring on the first bracket keeps a truncated answer from being mined
    for a nested object that no longer represents the model's reply.
    """

    for index, char in enumerate(text):
        if char in "{[":
            return _balanced_at(text, index)
    return None


def extract_json(text: Any) -> Any:
    """Return the first JSON object/array found in ``text``, or ``None``."""

    if not isinstance(text, str) or not text.strip():
        return None
    window = text[:MAX_TEXT]
    candidates: List[str] = [match.group(1) for match in _FENCE.finditer(window)]
    candidates.append(window)
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        block = _first_balanced(candidate)
        if block is None:
            continue
        try:
            return json.loads(block)
        except ValueError:
            continue
    return None


def validate(instance: Any, schema: Any, path: str = "$") -> List[str]:
    """Return human-readable violations; an empty list means valid."""

    errors: List[str] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if isinstance(expected, str):
        check = _TYPE_CHECKS.get(expected)
        if check is not None and not check(instance):
            return [
                "%s: ожидался тип %s, получен %s"
                % (path, expected, type(instance).__name__)
            ]

    if "const" in schema and instance != schema["const"]:
        errors.append("%s: ожидалось значение %r" % (path, schema["const"]))

    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        errors.append("%s: значение вне перечисления %r" % (path, enum))

    if isinstance(instance, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in instance:
                errors.append("%s: отсутствует обязательное поле %r" % (path, key))
        for key, subschema in props.items():
            if key in instance:
                errors.extend(validate(instance[key], subschema, "%s.%s" % (path, key)))
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(props)):
                errors.append("%s: лишнее поле %r" % (path, key))

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, "%s[%d]" % (path, index)))
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(instance) < minimum:
            errors.append("%s: элементов меньше %d" % (path, minimum))
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(instance) > maximum:
            errors.append("%s: элементов больше %d" % (path, maximum))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        low = schema.get("minimum")
        if isinstance(low, (int, float)) and instance < low:
            errors.append("%s: значение меньше %s" % (path, low))
        high = schema.get("maximum")
        if isinstance(high, (int, float)) and instance > high:
            errors.append("%s: значение больше %s" % (path, high))

    return errors


def parse_structured(text: Any, schema: Any) -> Tuple[Any, List[str]]:
    """Extract JSON from a model answer and validate it against ``schema``."""

    obj = extract_json(text)
    if obj is None:
        return None, ["ответ модели не содержит JSON-объекта"]
    return obj, validate(obj, schema)


_KINDS = [
    "document",
    "image",
    "audio",
    "video",
    "archive",
    "executable",
    "database",
    "keymaterial",
    "config",
    "other",
]

_TOOLS = [
    "list_partitions",
    "list_directory",
    "search_files",
    "extract_file",
    "recover_deleted_file",
    "detect_signatures",
    "auto_decrypt",
    "hash_file",
]

#: The schemas shipped with the application.  Adding one here is enough for
#: ``mountfile ai triage --schema`` and for any MCP client to use it.
SCHEMAS: Dict[str, dict] = {
    "file_triage": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "summary", "risk"],
        "properties": {
            "kind": {"type": "string", "enum": _KINDS},
            "summary": {"type": "string"},
            "risk": {"type": "string", "enum": ["none", "low", "high"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "indicators": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string"},
            },
            "next_steps": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
        },
    },
    "format_hypotheses": {
        "type": "object",
        "additionalProperties": False,
        "required": ["hypotheses"],
        "properties": {
            "hypotheses": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["format", "confidence", "reason"],
                    "properties": {
                        "format": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
    "investigation_plan": {
        "type": "object",
        "additionalProperties": False,
        "required": ["steps"],
        "properties": {
            "steps": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tool", "purpose"],
                    "properties": {
                        "tool": {"type": "string", "enum": _TOOLS},
                        "args": {"type": "object"},
                        "purpose": {"type": "string"},
                    },
                },
            }
        },
    },
    "timeline_query": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sql"],
        "properties": {
            "sql": {"type": "string"},
            "explanation": {"type": "string"},
        },
    },
}

#: Read-only SQL the timeline assistant is allowed to emit.
READ_ONLY_SQL_PREFIXES = ("select", "with", "explain")


def guard_sql(sql: Any) -> List[str]:
    """Reject anything that is not a single read-only statement."""

    errors: List[str] = []
    if not isinstance(sql, str) or not sql.strip():
        return ["пустой SQL"]
    body = sql.strip().rstrip(";").strip()
    lowered = body.lower()
    if not lowered.startswith(READ_ONLY_SQL_PREFIXES):
        errors.append("разрешены только SELECT/WITH/EXPLAIN")
    if ";" in body:
        errors.append("несколько операторов в одном запросе запрещены")
    for banned in ("insert ", "update ", "delete ", "drop ", "alter ", "attach ",
                   "create ", "pragma ", "vacuum ", "load_extension"):
        if re.search(r"\b%s" % banned.strip(), lowered):
            errors.append("запрещённая команда: %s" % banned.strip().upper())
    return errors
