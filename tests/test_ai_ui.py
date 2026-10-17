# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Qt-layer tests for the «AI» button: toolbar wiring and the dialog.

Runs offscreen without pytsk3 - the panel is a stand-in exposing the same
``selected_entry`` / ``join`` / ``provider`` surface the dialog uses.

Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_ai_ui.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("mountfile.qt_compat", reason="no Qt bindings installed")

from mountfile import ai  # noqa: E402
from mountfile.ai import AiResult  # noqa: E402
from mountfile.qt_compat import QApplication, QMenu, QToolBar  # noqa: E402
from mountfile.tc_window import AiDialog, MainWindow  # noqa: E402


class FakeProvider:
    """A two-file image: README.TXT plus one binary blob."""

    def __init__(self):
        self.blobs = {"/README.TXT": b"Mount File demo image\n" * 8,
                      "/blob.bin": bytes(range(256)) * 4}

    def read_file_range(self, path, offset=0, length=1 << 20):
        return self.blobs.get(path, b"")[offset:offset + length]


class FakePanel:
    """Minimal TcPanel stand-in for the AI dialog."""

    def __init__(self, mode="img", entry=None):
        self.mode = mode
        self.provider = FakeProvider()
        self._entry = entry

    def selected_entry(self):
        return self._entry

    def join(self, name):
        return "/" + name


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(app):
    w = MainWindow()
    yield w
    w._close_writer()
    w.close()


def test_ai_button_is_last_on_the_toolbar(win):
    bar = win.findChild(QToolBar)
    assert bar is not None
    acts = list(bar.actions())
    assert win.act_ai in acts, "кнопка AI не добавлена в тулбар"
    assert acts.index(win.act_ai) == len(acts) - 1, "AI должна быть последней"
    assert acts[-2].text() == "Справка", acts[-2].text()
    assert win.act_ai.text() == "AI"
    assert win.act_ai.shortcut().toString() == "Ctrl+I"
    assert win.act_ai.toolTip()


def test_ai_entry_duplicated_in_tools_menu(win):
    """Пункт-дубль в меню «Инструменты» — то же самое действие act_ai."""
    titles = [m.title() for m in win.menuBar().findChildren(QMenu)]
    assert "Инструменты" in titles, titles
    tools = next(m for m in win.menuBar().findChildren(QMenu)
                 if m.title() == "Инструменты")
    assert win.act_ai in tools.actions(), "нет пункта AI в меню Инструменты"


def test_dialog_prefills_from_active_panel(app):
    panel = FakePanel(entry={"name": "README.TXT", "is_dir": False, "size": 160})
    dlg = AiDialog(None, panel, log=lambda m: None)
    assert dlg._name() == "README.TXT"
    assert "README.TXT" in dlg.lbl_source.text()
    data, truncated = ai.collect(dlg._reader(), max_bytes=1 << 20)
    assert data.startswith(b"Mount File demo image")
    assert truncated is False
    dlg.deleteLater()


def test_dialog_reports_unconfigured_backend_without_raising(app):
    panel = FakePanel(entry={"name": "blob.bin", "is_dir": False, "size": 1024})
    logged = []
    dlg = AiDialog(None, panel, log=logged.append)
    dlg.run_analysis()
    assert "не настроен" in dlg.txt.toPlainText().lower()
    assert dlg.btn_run.isEnabled()
    assert logged and "AI-анализ" in logged[0]
    dlg.deleteLater()


def test_dialog_reads_local_file_with_shared_collect(app, tmp_path):
    p = tmp_path / "note.txt"
    p.write_bytes(b"hello ai")
    panel = FakePanel(entry=None)
    dlg = AiDialog(None, panel, log=lambda m: None)
    dlg._path = str(p)
    dlg._size = len(b"hello ai")
    data, truncated = ai.collect(dlg._reader(), max_bytes=4)
    assert data == b"hell"
    assert truncated is True
    dlg.deleteLater()


def test_dialog_without_any_file_does_nothing(app):
    panel = FakePanel(entry=None)
    dlg = AiDialog(None, panel, log=lambda m: None)
    assert dlg._entry is None and dlg._path is None
    assert dlg.txt.toPlainText() == ""
    dlg.deleteLater()


def test_result_is_rendered_with_backend_and_truncation(app):
    panel = FakePanel(entry={"name": "blob.bin", "is_dir": False, "size": 2048})
    dlg = AiDialog(None, panel, log=lambda m: None)
    dlg._show("blob.bin", AiResult(ok=True, backend="ollama", model="llama3",
                                   text="это дамп", bytes_read=2048,
                                   truncated=True))
    text = dlg.txt.toPlainText()
    assert "это дамп" in text
    assert "данные усечены" in text
    assert "ollama" in text and "llama3" in text
    dlg.deleteLater()


def test_dialog_shows_error_text_on_failure(app):
    panel = FakePanel(entry={"name": "blob.bin", "is_dir": False, "size": 4})
    dlg = AiDialog(None, panel, log=lambda m: None)
    dlg._show("blob.bin", AiResult(ok=False, backend="openai",
                                   error="Нет соединения: refused"))
    assert "Нет соединения" in dlg.txt.toPlainText()
    dlg.deleteLater()
