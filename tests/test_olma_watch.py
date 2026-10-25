# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for OLMA Watch: crash hooks, Ollama triage and GitHub reports.

No Qt, no network: ``_http_json`` and ``_ollama_analyze`` are monkeypatched,
so the suite runs anywhere CI does.

Run:  python -m pytest tests/test_olma_watch.py -v
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from mountfile import olma_watch as ow


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Fresh state file and a clean module cache for every test."""
    path = tmp_path / "olma-watch.json"
    monkeypatch.setattr(ow, "state_path", lambda: str(path))
    ow.reset_state_for_tests()
    yield path
    ow.reset_state_for_tests()


TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "main.py", line 10, in <module>\n'
    '  File "mountfile/tc_window.py", line 42, in open_image\n'
    "ValueError: bad image header"
)

TRACEBACK_2 = TRACEBACK.replace("ValueError", "KeyError")


# ------------------------------------------------------------ sanitizing


def test_sanitize_hides_home_and_user_paths():
    home = os.path.expanduser("~")
    text = "open failed: %s/data/dump.img" % home
    out = ow.sanitize(text)
    assert home not in out
    assert "<user>" in ow.sanitize("/home/ivan Ivanov/app.py")
    assert "<user>" in ow.sanitize("C:\\Users\\Vasya\\app.py")


def test_sanitize_hides_tokens_and_emails():
    out = ow.sanitize(
        "token github_pat_FAKE0000FAKE0000FAKE leaked, "
        "authorization: Bearer abcdef123456, mail me@example.com"
    )
    assert "github_pat_FAKE0000FAKE0000FAKE" not in out
    assert "abcdef123456" not in out
    assert "me@example.com" not in out
    assert "<secret>" in out


def test_fingerprint_stable_and_sensitive():
    assert ow.fingerprint(TRACEBACK) == ow.fingerprint(TRACEBACK)
    assert ow.fingerprint(TRACEBACK) != ow.fingerprint(TRACEBACK_2)


# ------------------------------------------------------------ report body


def test_compose_report_marks_fingerprint():
    title, body = ow.compose_report(TRACEBACK, context="тест")
    assert title.startswith("OLMA Watch:")
    assert "olma-fp: %s" % ow.fingerprint(TRACEBACK) in body
    assert "Трассировка" in body


# ------------------------------------------------------------ settings/switch


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ow.ENV_ENABLE, raising=False)
    url, err = ow.report_text(TRACEBACK, context="тест")
    assert url is None
    assert "отключён" in err


def test_env_override_forces_on(monkeypatch):
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    assert ow.enabled() is True
    monkeypatch.setenv(ow.ENV_ENABLE, "0")
    assert ow.enabled() is False


# ------------------------------------------------------------ delivery


def test_new_issue_created(monkeypatch):
    seen = {}

    def fake_http(method, url, payload=None, token=None, timeout=15):
        seen["method"], seen["url"], seen["payload"] = method, url, payload
        if method == "GET":  # the open-issue search before the create
            return 200, [], ""
        assert method == "POST" and url.endswith("/issues")
        assert payload["labels"] == [ow.ISSUE_LABEL]
        return 201, {"html_url": "https://github.com/o/r/issues/99"}, ""

    monkeypatch.setattr(ow, "_http_json", fake_http)
    monkeypatch.setattr(ow, "_ollama_analyze", lambda t: "причина: тест")
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    url, err = ow.report_text(TRACEBACK, context="тест")
    assert err == "" and url.endswith("/issues/99")
    assert seen["payload"]["title"].startswith("OLMA Watch:")
    assert "причина: тест" in seen["payload"]["body"]
    assert "test-token" not in seen["payload"]["body"]


def test_duplicate_comments_instead_of_new_issue(monkeypatch):
    calls = []
    created = {"number": None}

    def fake_http(method, url, payload=None, token=None, timeout=15):
        calls.append((method, url))
        if method == "GET":  # the open-issue search
            if created["number"] is None:
                return 200, [], ""
            fp = ow.fingerprint(TRACEBACK)
            return 200, [{"number": created["number"], "body": "olma-fp: %s" % fp}], ""
        if url.endswith("/issues"):
            created["number"] = 7
            return 201, {"html_url": "https://github.com/o/r/issues/7"}, ""
        assert url.endswith("/issues/7/comments")
        return 201, {"html_url": "https://github.com/o/r/issues/7#x"}, ""

    monkeypatch.setattr(ow, "_http_json", fake_http)
    monkeypatch.setattr(ow, "_ollama_analyze", lambda t: "")
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    url1, _ = ow.report_text(TRACEBACK, context="первый")
    url2, err2 = ow.report_text(TRACEBACK, context="повтор")
    assert url1 and url2 and err2 == ""
    methods = [m for m, _u in calls]
    assert methods.count("POST") == 2  # one create, one comment


def test_offline_report_is_queued_then_flushed(monkeypatch):
    def dead_http(method, url, payload=None, token=None, timeout=15):
        return None, None, "нет соединения"

    monkeypatch.setattr(ow, "_http_json", dead_http)
    monkeypatch.setattr(ow, "_ollama_analyze", lambda t: "")
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    url, err = ow.report_text(TRACEBACK, context="офлайн")
    assert url is None
    state = json.loads(open(ow.state_path(), encoding="utf-8").read())
    assert len(state["pending"]) == 1

    # The network is back on the next start.
    def live_http(method, url, payload=None, token=None, timeout=15):
        return 201, {"html_url": "https://github.com/o/r/issues/5"}, ""

    monkeypatch.setattr(ow, "_http_json", live_http)
    sent = ow.flush_pending()
    assert sent == 1
    state = json.loads(open(ow.state_path(), encoding="utf-8").read())
    assert state["pending"] == []


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(ow, "_http_json", lambda *a, **k: (201, {"html_url": "u"}, ""))
    monkeypatch.setattr(ow, "_ollama_analyze", lambda t: "")
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    sent = 0
    for i in range(ow.MAX_PER_HOUR + 3):
        tb = TRACEBACK.replace("line 42", "line %d" % (100 + i))
        url, err = ow.report_text(tb, context="тест")
        sent += 1 if url else 0
    assert sent == ow.MAX_PER_HOUR


def test_missing_token_is_reported_readably(monkeypatch):
    monkeypatch.delenv(ow.ENV_TOKEN, raising=False)
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    url, err = ow.report_text(TRACEBACK, context="тест")
    assert url is None
    assert ow.ENV_TOKEN in err


def test_analysis_timeout_does_not_block_reporting_thread(monkeypatch):
    started = threading.Event()
    finished = threading.Event()

    def slow_analysis(_text):
        started.set()
        time.sleep(0.4)
        finished.set()
        return "late result"

    monkeypatch.setattr(ow, "_ollama_analyze", slow_analysis)
    monkeypatch.setattr(ow, "ANALYZE_TIMEOUT", 0.02)
    monkeypatch.setattr(
        ow, "_http_json", lambda *a, **k: (201, {"html_url": "u"}, "")
    )
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")

    before = time.monotonic()
    url, err = ow.report_text(TRACEBACK, context="тест")
    elapsed = time.monotonic() - before

    assert url == "u" and err == ""
    assert started.is_set()
    assert elapsed < 0.3
    assert finished.wait(1)


def test_local_backend_timeout_is_capped_at_analyze_timeout(monkeypatch):
    from types import SimpleNamespace

    from mountfile import ai

    class Backend:
        timeout = 60

        def analyze(self, *_args, **_kwargs):
            return SimpleNamespace(ok=True, text="готово")

    backend = Backend()
    monkeypatch.setattr(ow, "_ollama_settings", lambda: {"ai_backend": "ollama"})
    monkeypatch.setattr(ai, "build_backend", lambda _settings: backend)

    assert ow._ollama_analyze(TRACEBACK) == "готово"
    assert backend.timeout == ow.ANALYZE_TIMEOUT


def test_offline_pending_queue_sanitizes_traceback_and_context(monkeypatch):
    monkeypatch.setattr(ow, "_http_json", lambda *a, **k: (None, None, "offline"))
    monkeypatch.setattr(ow, "_ollama_analyze", lambda _text: "")
    monkeypatch.setenv(ow.ENV_TOKEN, "test-token")
    monkeypatch.setenv(ow.ENV_ENABLE, "1")
    private_tb = (
        TRACEBACK
        + "\nfile /home/ivan/private.py token github_pat_FAKE0000FAKE0000FAKE"
    )
    private_context = "opened /home/ivan/disk.img with github_pat_FAKE0000FAKE0000FAKE"

    ow.report_text(private_tb, context=private_context)

    queued = json.loads(open(ow.state_path(), encoding="utf-8").read())["pending"][0]
    assert "ivan" not in queued["traceback"] + queued["context"]
    assert "github_pat_FAKE0000FAKE0000FAKE" not in (
        queued["traceback"] + queued["context"]
    )


# ------------------------------------------------------------ no secrets


def test_no_leaked_token_in_module_source():
    here = os.path.dirname(__file__)
    src = open(
        os.path.join(here, "..", "mountfile", "olma_watch.py"), encoding="utf-8"
    ).read()
    # The real token leaked in chat must never appear in the code; the
    # _SECRET_RE pattern mentions the prefix only as a sanitizer rule.
    assert "github_pat_FAKE0000FAKE0000FAKE" not in src
    assert ow.ENV_TOKEN in src  # the token comes from the env var only
