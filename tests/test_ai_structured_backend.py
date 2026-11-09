# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Structured output plumbing in the AI backends - no network.

The HTTP layer is replaced with a recorder so the exact payload each backend
would send is asserted, including the JSON schema and the data fence.

Run:  python -m pytest tests/test_ai_structured_backend.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.ai import backend as ai_backend  # noqa: E402
from mountfile.ai.backend import (  # noqa: E402
    NullBackend,
    OllamaBackend,
    OpenAiCompatBackend,
    build_prompt,
)
from mountfile.ai.guard import FENCE_CLOSE, FENCE_OPEN  # noqa: E402
from mountfile.ai.schema import SCHEMAS  # noqa: E402


class Recorder:
    """Stands in for ``_post_json``: records the payload, replays a reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, url, payload, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        return self.reply, ""


def test_build_prompt_fences_file_content():
    prompt = build_prompt("notes.txt", b"hello")
    assert prompt.startswith(FENCE_OPEN)
    assert FENCE_CLOSE in prompt
    assert "hello" in prompt


def test_build_prompt_fence_can_be_disabled():
    prompt = build_prompt("notes.txt", b"hello", guard=False)
    assert not prompt.startswith(FENCE_OPEN)
    assert prompt.startswith("Файл: notes.txt")


def test_build_prompt_neutralizes_injected_closing_marker():
    prompt = build_prompt("evil.txt", ("x " + FENCE_CLOSE + " y").encode("utf-8"))
    assert prompt.count(FENCE_CLOSE) == 1


def test_ollama_sends_json_schema_as_format(monkeypatch):
    recorder = Recorder({"response": '{"kind": "document"}'})
    monkeypatch.setattr(ai_backend, "_post_json", recorder)
    backend = OllamaBackend(base_url="http://127.0.0.1:11434", model="llama3.2:3b")
    backend.analyze("dump.bin", b"data", schema=SCHEMAS["file_triage"])
    assert recorder.calls[0]["payload"]["format"] == SCHEMAS["file_triage"]


def test_ollama_without_schema_omits_format(monkeypatch):
    recorder = Recorder({"response": "plain answer"})
    monkeypatch.setattr(ai_backend, "_post_json", recorder)
    backend = OllamaBackend(base_url="http://127.0.0.1:11434", model="llama3")
    backend.analyze("dump.bin", b"data")
    assert "format" not in recorder.calls[0]["payload"]


def test_ollama_analyze_json_parses_fenced_reply(monkeypatch):
    reply = {"response": '```json\n{"kind": "image", "summary": "s", "risk": "none"}\n```'}
    monkeypatch.setattr(ai_backend, "_post_json", Recorder(reply))
    backend = OllamaBackend(base_url="http://127.0.0.1:11434", model="llama3")
    obj, errors, result = backend.analyze_json(
        "photo.jpg", b"\xff\xd8\xff", SCHEMAS["file_triage"]
    )
    assert result.ok is True
    assert errors == []
    assert obj["kind"] == "image"


def test_ollama_analyze_json_reports_validation_error(monkeypatch):
    reply = {"response": '{"kind": "image"}'}
    monkeypatch.setattr(ai_backend, "_post_json", Recorder(reply))
    backend = OllamaBackend(base_url="http://127.0.0.1:11434", model="llama3")
    obj, errors, result = backend.analyze_json(
        "photo.jpg", b"\xff\xd8\xff", SCHEMAS["file_triage"]
    )
    assert result.ok is True
    assert obj == {"kind": "image"}
    assert errors


def test_openai_sends_json_schema_response_format(monkeypatch):
    recorder = Recorder(
        {"choices": [{"message": {"content": '{"kind": "config", '
                                             '"summary": "s", "risk": "low"}'}}]}
    )
    monkeypatch.setattr(ai_backend, "_post_json", recorder)
    backend = OpenAiCompatBackend(
        base_url="http://127.0.0.1:8080", model="local", api_key=""
    )
    backend.analyze("app.ini", b"[x]\ny=1\n", schema=SCHEMAS["file_triage"])
    fmt = recorder.calls[0]["payload"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMAS["file_triage"]


def test_openai_falls_back_to_json_object_on_400(monkeypatch):
    calls = []

    def fake(url, payload, headers=None, timeout=None, allow_redirects=True):
        calls.append(payload)
        if len(calls) == 1:
            return None, "HTTP 400: response_format json_schema unsupported"
        return {"choices": [{"message": {"content": '{"a": 1}'}}]}, ""

    monkeypatch.setattr(ai_backend, "_post_json", fake)
    backend = OpenAiCompatBackend(
        base_url="http://127.0.0.1:8080", model="local", api_key=""
    )
    result = backend.analyze("x.bin", b"data", schema=SCHEMAS["file_triage"])
    assert result.ok is True
    assert calls[1]["response_format"] == {"type": "json_object"}


def test_null_backend_ignores_schema():
    result = NullBackend().analyze("x.bin", b"data", schema=SCHEMAS["file_triage"])
    assert result.ok is False
    assert result.backend == "none"


def test_schema_is_json_serialisable():
    for name, schema in SCHEMAS.items():
        assert json.loads(json.dumps(schema)) == schema, name
