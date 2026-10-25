"""Regression tests for persisted Total Commander UI preferences."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from types import ModuleType


def _load_tc_style():
    """Load settings helpers even on CI hosts without an installed Qt runtime."""
    try:
        return importlib.import_module("mountfile.tc_style")
    except ImportError:
        # tc_style's settings code itself needs no Qt; only its theme helpers do.
        # Supply import-only names in this isolated test process.
        qt_name = "mountfile.qt_compat"
        original = sys.modules.get(qt_name)
        stub = ModuleType(qt_name)
        for name in (
            "P_ALTERNATEBASE",
            "P_BASE",
            "P_BUTTON",
            "P_BUTTONTEXT",
            "P_HIGHLIGHT",
            "P_HIGHLIGHTEDTEXT",
            "P_TEXT",
            "P_TOOLTIPBASE",
            "P_TOOLTIPTEXT",
            "P_WINDOW",
            "P_WINDOWTEXT",
            "CG_Disabled",
            "QColor",
            "QFont",
            "QFontDatabase",
            "QPalette",
        ):
            setattr(stub, name, object())
        sys.modules[qt_name] = stub
        try:
            return importlib.import_module("mountfile.tc_style")
        finally:
            if original is None:
                sys.modules.pop(qt_name, None)
            else:
                sys.modules[qt_name] = original


tc_style = _load_tc_style()


def test_posix_settings_are_private_and_other_preferences_are_saved(tmp_path, monkeypatch):
    if os.name != "posix":
        return
    path = tmp_path / "ui.json"
    path.write_text('{"old": true}', encoding="utf-8")
    path.chmod(0o666)
    monkeypatch.setattr(tc_style, "SETTINGS_PATH", str(path))
    original = dict(tc_style.SETTINGS)
    try:
        tc_style.SETTINGS.update(
            {
                "theme": "light",
                "font_size": 12,
                "show_log": True,
                "ai_api_key": "secret-api",
                "ai_github_token": "secret-github",
                "olma_token": "secret-olma",
            }
        )
        assert tc_style.save_settings()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["ai_api_key"] == "secret-api"
        assert saved["ai_github_token"] == "secret-github"
        assert saved["olma_token"] == "secret-olma"
        assert saved["theme"] == "light"
        assert saved["font_size"] == 12
        assert saved["show_log"] is True
        assert saved["ai_backend"] == original["ai_backend"]
    finally:
        tc_style.SETTINGS.clear()
        tc_style.SETTINGS.update(original)


def test_new_posix_settings_file_uses_private_permissions(tmp_path, monkeypatch):
    if os.name != "posix":
        return
    path = tmp_path / "nested" / "ui.json"
    monkeypatch.setattr(tc_style, "SETTINGS_PATH", str(path))
    assert tc_style.save_settings()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
