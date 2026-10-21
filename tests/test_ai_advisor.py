# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the AI-советник v2 module (no Qt display, no network)."""

import os
import sys

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
    GitHubReporter,
    HypothesisEngine,
    build_fingerprint,
)


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
