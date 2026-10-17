# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the AI backend layer - no Qt, no network.

Run:  python -m pytest tests/test_ai_backend.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.ai import (BACKENDS, AiResult, NullBackend, OllamaBackend,
                          OpenAiCompatBackend, build_backend, build_prompt,
                          collect, local_reader, looks_textual, max_bytes,
                          summarize_bytes)


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_collect_stops_at_limit_and_flags_truncation(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"A" * 5000)
    data, truncated = collect(local_reader(p), max_bytes=1000, chunk=256)
    assert data == b"A" * 1000
    assert truncated is True


def test_collect_full_file_is_not_truncated(tmp_path):
    p = tmp_path / "small.bin"
    p.write_bytes(b"hello")
    data, truncated = collect(local_reader(p), max_bytes=1 << 20)
    assert data == b"hello"
    assert truncated is False


def test_collect_on_empty_file(tmp_path):
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    data, truncated = collect(local_reader(p))
    assert data == b""
    assert truncated is False


def test_looks_textual_separates_text_and_binary():
    assert looks_textual(b"key=value\nsecond line\n")
    assert not looks_textual(b"\x00\x01\x02\x03")
    assert not looks_textual(b"")


def test_summarize_bytes_hex_dump_for_binary():
    text = summarize_bytes("dump.bin", bytes(range(32)), truncated=True, size=4096)
    assert "dump.bin" in text
    assert "данные усечены" in text
    assert "00000000" in text


def test_build_prompt_appends_user_question():
    prompt = build_prompt("a.txt", b"hello", user_prompt="что это?")
    assert "hello" in prompt
    assert "Задача от пользователя" in prompt
    assert prompt.index("hello") < prompt.index("Задача от пользователя")


def test_null_backend_returns_readable_error_without_raising():
    res = NullBackend().analyze("a.bin", b"data")
    assert res.ok is False
    assert "не настроен" in res.error
    assert res.bytes_read == 4


def test_ollama_backend_parses_response(monkeypatch):
    backend = OllamaBackend(base_url="http://127.0.0.1:11434/", model="llama3")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse({"response": "  это FAT32  "})

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", fake_urlopen)
    res = backend.analyze("img.bin", b"EB 3C 90", prompt="что это?")
    assert res.ok and res.text == "это FAT32"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "llama3"


def test_ollama_backend_empty_answer_is_an_error(monkeypatch):
    backend = OllamaBackend(model="missing")
    monkeypatch.setattr(
        "mountfile.ai.backend.urllib.request.urlopen",
        lambda req, timeout=None: FakeResponse({"response": ""}),
    )
    res = backend.analyze("a.bin", b"x")
    assert res.ok is False
    assert "пустой ответ" in res.error


def test_openai_backend_sends_bearer_and_parses_choice(monkeypatch):
    backend = OpenAiCompatBackend(
        base_url="https://example.test/", model="m1", api_key="secret-key"
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", fake_urlopen)
    res = backend.analyze("a.bin", b"x")
    assert res.ok and res.text == "ok"
    assert captured["auth"] == "Bearer secret-key"
    assert captured["url"] == "https://example.test/v1/chat/completions"


def test_network_failure_is_reported_not_raised(monkeypatch):
    backend = OllamaBackend()
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", boom)
    res = backend.analyze("a.bin", b"x")
    assert res.ok is False
    assert "Нет соединения" in res.error


def test_available_is_false_when_nothing_configured():
    assert NullBackend().available() is False
    assert OpenAiCompatBackend(api_key="").available() is False
    assert OpenAiCompatBackend(api_key="k").available() is True


def test_build_backend_selects_by_settings_and_never_returns_none():
    assert isinstance(build_backend({}), NullBackend)
    assert isinstance(build_backend({"ai_backend": "unknown"}), NullBackend)
    assert isinstance(
        build_backend({"ai_backend": "ollama", "ai_model": "m"}), OllamaBackend
    )
    assert isinstance(
        build_backend({"ai_backend": "openai", "ai_api_key": "k"}),
        OpenAiCompatBackend,
    )
    for key, (_label, factory) in BACKENDS.items():
        assert isinstance(factory({"ai_backend": key}), object)


def test_max_bytes_is_clamped():
    assert max_bytes({}) > 0
    assert max_bytes({"ai_max_bytes": "nonsense"}) > 0
    assert max_bytes({"ai_max_bytes": 1}) >= 4096
    assert max_bytes({"ai_max_bytes": 10**12}) <= 64 * 1024 * 1024


def test_result_to_dict_has_no_api_key_field():
    d = AiResult(ok=True, backend="ollama", text="t").to_dict()
    assert "api_key" not in d
    assert d["ok"] is True
