# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the built-in HEX editor page and its scoped actions."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("mountfile.qt_compat", exc_type=ImportError)

from mountfile import tc_window
from mountfile.qt_compat import MB_No, MB_Yes, QApplication, WidgetWithChildrenShortcut
from mountfile.tc_window import HexEditorPage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_editor_page_has_scoped_save_find_and_goto_actions(app):
    page = HexEditorPage(
        None, "sample.bin", 3, b"abc", lambda _data: True, lambda: None
    )
    actions = {
        action.shortcut().toString(): action for action in page._shortcut_actions
    }

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


def test_local_hex_save_refuses_external_file_change(monkeypatch, tmp_path):
    target = tmp_path / "sample.bin"
    target.write_bytes(b"opened bytes")

    class Panel:
        mode = "local"

        def selected_entry(self):
            return {"name": target.name, "size": target.stat().st_size}

        def join(self, _name):
            return str(target)

        def refresh(self):
            pass

    class Window:
        def active_panel(self):
            return Panel()

        def _show_hex_page(self, page):
            self.page = page

        def _show_panels(self):
            pass

        def _maybe_advise(self, *_args, **_kwargs):
            pass

        def log_msg(self, _message):
            pass

    class CapturedPage:
        def __init__(self, *args, **kwargs):
            self.save_callback = kwargs["save_callback"]

    monkeypatch.setattr(tc_window, "HexEditorPage", CapturedPage)
    monkeypatch.setattr(tc_window.QMessageBox, "warning", lambda *_args: MB_Yes)
    window = Window()
    tc_window.MainWindow.f3_hex_view(window)
    window.page.save_callback(b"first save")
    window.page.save_callback(b"second save")
    assert target.read_bytes() == b"second save"

    tc_window.MainWindow.f3_hex_view(window)
    target.write_bytes(b"external edit")
    with pytest.raises(RuntimeError, match="changed since it was opened"):
        window.page.save_callback(b"stale editor contents")
    assert target.read_bytes() == b"external edit"
