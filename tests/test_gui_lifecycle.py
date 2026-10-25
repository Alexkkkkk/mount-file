"""Focused GUI regressions for directory retry and editor-copy cleanup."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - depends on the runner's Qt runtime
    import mountfile.qt_compat  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.skip("Qt bindings are not usable: %s" % exc, allow_module_level=True)

from mountfile import tc_style, text_editor
from mountfile.qt_compat import QApplication, MB_Yes, QMessageBox
from mountfile.tc_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_panel_list_error_has_visible_retry_button(app, monkeypatch):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    win = MainWindow()

    class Provider:
        def __init__(self):
            self.calls = 0

        def list_dir(self, _path):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary listing failure")
            return []

    try:
        panel = win.panelL
        provider = Provider()
        panel.mode = "img"
        panel.provider = provider
        panel.path = "/"
        panel.refresh()

        assert panel._listing_hint and "temporary listing failure" in panel._listing_hint
        assert not panel.empty_retry.isHidden()
        panel.empty_retry.click()
        assert provider.calls == 2
        assert panel._listing_hint is None
        assert panel.empty_retry.isHidden()
    finally:
        win.close()


def test_saved_image_editor_copy_is_removed_when_window_closes(
    app, monkeypatch, tmp_path
):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: MB_Yes)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args, **_kwargs: None)
    win = MainWindow()
    saved = {}

    class Provider:
        data = b"value=old\n"

        def read_file(self, _path):
            return self.data

    class Writer:
        def write_file(self, directory, name, data):
            saved["path"] = (directory, name)
            saved["data"] = data
            provider.data = data

    class Panel:
        mode = "img"
        path = "/"
        provider = Provider()
        writer = Writer()

        def join(self, name):
            return "/" + name

        def refresh(self):
            pass

    provider = Panel.provider
    panel = Panel()
    monkeypatch.setattr(win, "_backup_before_write", lambda _panel: True)

    def edit_copy(path):
        with open(path, "wb") as stream:
            stream.write(b"value=saved\n")
        return ["fake-editor", str(path)]

    monkeypatch.setattr(text_editor, "open_in_editor", edit_copy)
    try:
        win._open_text_entry(
            panel,
            {"name": "settings.ini", "is_dir": False, "size": len(provider.data)},
        )
        temp_dir = win._text_tmp
        assert temp_dir and os.path.isdir(temp_dir)
        assert saved == {"path": ("/", "settings.ini"), "data": b"value=saved\n"}
        assert provider.data == b"value=saved\n"
        assert os.path.exists(os.path.join(temp_dir, "settings.ini"))

        win.close()
        assert not os.path.exists(temp_dir)
        assert win._text_tmp is None
    finally:
        if win.isVisible():
            win.close()


def test_window_keeps_editor_copy_until_external_editor_closes(
    app, monkeypatch, tmp_path
):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    win = MainWindow()
    temp_dir = tmp_path / "mountfile-text-session"
    temp_dir.mkdir()
    copy = temp_dir / "notes.txt"
    copy.write_text("saved text", encoding="utf-8")
    win._text_tmp = str(temp_dir)
    active = {"value": True}
    monkeypatch.setattr(
        text_editor, "has_active_editors", lambda _directory: active["value"]
    )

    try:
        assert not win.close()
        assert copy.read_text(encoding="utf-8") == "saved text"
        assert temp_dir.is_dir()

        active["value"] = False
        assert win.close()
        assert not temp_dir.exists()
    finally:
        if win.isVisible():
            active["value"] = False
            win.close()
