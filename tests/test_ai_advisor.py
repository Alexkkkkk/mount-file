# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the AI-советник v2 module (no Qt display, no network)."""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# exc_type=ImportError is required: mountfile.qt_compat raises its own
# ImportError when neither PyQt6 nor PyQt5 is importable, and pytest >= 9
# only skips on the requested exception type (same pattern as test_ai_ui.py;
# without the guard the CI runners without libEGL die on collection).
pytest.importorskip(
    "mountfile.qt_compat", exc_type=ImportError, reason="no Qt bindings installed"
)

from mountfile.ai_format_advisor_v2 import Calibration  # noqa: E402
from mountfile.ai_format_advisor_v2 import (
    FormatAdvisorDialog,
    GitHubReporter,
    HypothesisEngine,
    assess_consensus,
    build_fingerprint,
)
from mountfile.qt_compat import QApplication, QTimer  # noqa: E402


def test_fingerprint_of_a_text_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_bytes(b"hello world" * 10)
    fp = build_fingerprint(str(p))
    assert fp["name"] == "note.txt"
    assert fp["size"] == 110
    assert fp["head"].startswith(b"hello")
    assert len(fp["sha1"]) == 40


def test_engine_recognizes_zip(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    fp = build_fingerprint(str(p))
    hyps = HypothesisEngine(None).diagnose(fp)
    assert hyps[0]["name"].startswith("ZIP")
    assert Calibration().is_confident(hyps)


def test_engine_is_not_confident_on_unknown(tmp_path):
    p = tmp_path / "mystery.fw"
    p.write_bytes(bytes(range(1, 200)) * 40)  # no known signature
    fp = build_fingerprint(str(p))
    hyps = HypothesisEngine(None).diagnose(fp)
    assert not Calibration().is_confident(hyps)


def test_github_reporter_validates_arguments():
    with pytest.raises(ValueError):
        GitHubReporter("", "token")
    with pytest.raises(ValueError):
        GitHubReporter("owner/name", "")


def _wait_for(predicate, timeout=2):
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "condition did not become true"


def test_consensus_identical_and_opposite_answers():
    assert "совпадают" in assess_consensus([("a", " ZIP "), ("b", "zip")])
    assert "Расхождение" in assess_consensus([("a", "это ZIP"), ("b", "это gzip")])


def test_dialog_backend_runs_asynchronously_and_updates_on_gui_thread():
    app = QApplication.instance() or QApplication([])
    entered, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()

    class Result:
        ok, text, error = True, "ZIP format", ""

    class Backend:
        name = "slow"

        def ask(self, *_args):
            entered.set()
            assert release.wait(2)
            return Result()

    dlg = FormatAdvisorDialog(
        {"name": "sample.bin", "size": 4, "head": b"data", "entropy": 2},
        [Backend()],
    )
    updates = []
    original_render = dlg._render

    def track_render(*args):
        updates.append(threading.get_ident())
        return original_render(*args)

    dlg._render = track_render
    dlg.ask_models()
    assert not entered.is_set() or dlg.btn_run.isEnabled() is False
    assert not dlg.btn_run.isEnabled()
    assert entered.wait(1)
    assert dlg.txt.toPlainText().find("ZIP format") == -1
    release.set()
    _wait_for(lambda: dlg.btn_run.isEnabled() and "ZIP format" in dlg.txt.toPlainText())
    assert updates[-1] == gui_thread
    dlg.close()


def test_dialog_displays_consensus_and_backend_errors():
    app = QApplication.instance() or QApplication([])

    class Result:
        def __init__(self, text):
            self.ok, self.text, self.error = True, text, ""

    class Backend:
        def __init__(self, name, text):
            self.name, self.text = name, text

        def ask(self, *_args):
            if isinstance(self.text, Exception):
                raise self.text
            return Result(self.text)

    dlg = FormatAdvisorDialog(
        {"name": "x", "size": 1, "head": b"x", "entropy": 0},
        [Backend("one", "same"), Backend("two", "same")],
    )
    dlg.ask_models()
    _wait_for(lambda: dlg.btn_run.isEnabled() and "Консенсус" in dlg.txt.toPlainText())
    assert "совпадают" in dlg.txt.toPlainText()
    dlg.adapters = [Backend("one", "ZIP"), Backend("two", RuntimeError("offline"))]
    dlg.ask_models()
    _wait_for(lambda: dlg.btn_run.isEnabled() and "offline" in dlg.txt.toPlainText())
    assert "Расхождение" not in dlg.txt.toPlainText()
    assert "Ошибка: offline" in dlg.txt.toPlainText()
    dlg.close()
