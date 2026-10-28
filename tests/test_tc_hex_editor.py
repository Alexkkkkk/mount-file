# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the built-in HEX editor page and its scoped actions."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("mountfile.qt_compat", exc_type=ImportError)

from mountfile.qt_compat import MB_No, MB_Yes, QApplication, WidgetWithChildrenShortcut
from mountfile.tc_window import HexEditorPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_editor_page_has_scoped_save_find_and_goto_actions(app):
    page = HexEditorPage(
        None, "sample.bin", 3, b"abc", lambda _data: True, lambda: None
    )
    actions = {action.shortcut().toString(): action for action in page._shortcut_actions}

    assert set(actions) == {"Ctrl+S", "Ctrl+F", "Ctrl+G"}
    assert all(
        action.shortcutContext() == WidgetWithChildrenShortcut
        for action in actions.values()
    )
    assert page.goto_button.text() == "Перейти…"


def test_back_to_panels_requires_explicit_discard_confirmation(app, monkeypatch):
    closed = []
    page = HexEditorPage(
        None, "sample.bin", 3, b"abc", lambda _data: True, lambda: closed.append(True)
    )
    page.hex_view.insertBytes(b"\x00")

    monkeypatch.setattr(
        "mountfile.tc_window.QMessageBox.warning",
        lambda *_args, **_kwargs: MB_No,
    )
    page.request_close()
    assert closed == []

    monkeypatch.setattr(
        "mountfile.tc_window.QMessageBox.warning",
        lambda *_args, **_kwargs: MB_Yes,
    )
    page.request_close()
    assert closed == [True]


def test_save_callback_updates_document_state(app):
    saved = []
    page = HexEditorPage(
        None,
        "sample.bin",
        3,
        b"abc",
        lambda data: saved.append(data) or True,
        lambda: None,
    )
    page.hex_view.insertBytes(b"\x00")

    page.save()

    assert saved == [b"\x00abc"]
    assert not page.hex_view.isModified()


def test_read_only_page_keeps_save_disabled(app):
    page = HexEditorPage(None, "sample.bin", 3, b"abc", None, lambda: None)

    page.hex_view.insertBytes(b"\x00")

    assert not page.save_button.isEnabled()
    assert "только для чтения" in page.status.text()
