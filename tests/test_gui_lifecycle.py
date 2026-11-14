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
from mountfile.qt_compat import MB_No, MB_Yes, QApplication, QEvent, QMessageBox
from mountfile.tc_window import HexEditorPage, MainWindow


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

        assert (
            panel._listing_hint and "temporary listing failure" in panel._listing_hint
        )
        assert not panel.empty_retry.isHidden()
        panel.empty_retry.click()
        assert provider.calls == 2
        assert panel._listing_hint is None
        assert panel.empty_retry.isHidden()
    finally:
        win.close()
        # Closing a QWidget leaves its C++ object alive until DeferredDelete is
        # delivered. Remove it before the next MainWindow reapplies the global
        # application stylesheet (which can access-violate on Windows/PyQt).
        win.deleteLater()
        deferred_delete = getattr(QEvent, "Type", QEvent).DeferredDelete
        app.sendPostedEvents(None, deferred_delete)
        app.processEvents()


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
        from pathlib import Path

        assert len(list(Path(temp_dir).rglob("settings.ini"))) == 1

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


def test_closed_window_ignores_late_update_notification(app, monkeypatch):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    win = MainWindow()

    def unexpected_dialog(*_args, **_kwargs):
        raise AssertionError("closed window must not show an update dialog")

    monkeypatch.setattr(QMessageBox, "information", unexpected_dialog)
    monkeypatch.setattr(QMessageBox, "question", unexpected_dialog)
    assert win.close()
    assert win._closing

    win._on_update_available(
        {"version": "1.55.0", "url": "https://example.invalid/release"}
    )


def test_image_directory_copy_uses_provider_and_exports_files(
    app, monkeypatch, tmp_path
):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    win = MainWindow()
    calls = []

    class Provider:
        def list_dir(self, path):
            calls.append(path)
            if path == "/":
                return [{"name": "folder", "is_dir": True}]
            return [{"name": "note.txt", "is_dir": False}]

        def read_file(self, path):
            assert path == "/folder/note.txt"
            return b"exported"

    class Panel:
        provider = Provider()

        @staticmethod
        def join(name):
            return "/" + name

    class LocalPanel:
        path = str(tmp_path)

        @staticmethod
        def refresh():
            pass

    try:
        win._copy_img_to_local(
            Panel(), LocalPanel(), [{"name": "folder", "is_dir": True}]
        )
        assert calls == ["/folder", "/", "/folder"]
        assert (tmp_path / "folder" / "note.txt").read_bytes() == b"exported"
    finally:
        win.close()


def test_switching_image_closes_hex_and_resets_old_image_panel(
    app, monkeypatch, tmp_path
):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    win = MainWindow()
    image = tmp_path / "new.img"
    image.write_bytes(b"new image")
    reset_calls = []

    class Provider:
        closed = False

        def close(self):
            self.closed = True

    old_provider = Provider()
    right = win.panelR
    right.mode = "img"
    right.provider = old_provider
    right.path = "/old-image/path"
    right.tabs = [right.path]
    right.tab_index = 0
    right.entries = [{"name": "old-image-only-entry"}]
    right._rebuild_tabs()
    monkeypatch.setattr(win, "scan_volumes", lambda _path: [])
    monkeypatch.setattr(win, "mount_volume", lambda _panel, _offset: True)
    monkeypatch.setattr(win, "_set_loaded", lambda _loaded: None)
    monkeypatch.setattr(win, "_update_cmd_prompt", lambda: None)
    original_refresh = right.refresh

    def refresh():
        reset_calls.append(right.path)
        original_refresh()

    monkeypatch.setattr(right, "refresh", refresh)
    page = HexEditorPage(
        win, "sample.bin", 3, b"abc", lambda _data: True, win._show_panels
    )
    page.hex_view.insertBytes(b"changed")
    win._show_hex_page(page)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: MB_No)

    try:
        assert not win.load_image(str(image))
        assert win.image_path is None
        assert win.workspace_stack.currentWidget() is page
        assert not old_provider.closed

        monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: MB_Yes)
        assert win.load_image(str(image))
        assert win.workspace_stack.currentWidget() is win.splitter
        assert old_provider.closed
        assert right.mode == "local"
        assert right.path == os.path.expanduser("~")
        assert right.tabs == [right.path]
        assert reset_calls == [right.path]
        assert all(e.get("name") != "old-image-only-entry" for e in right.entries)
    finally:
        if win.isVisible():
            monkeypatch.setattr(
                QMessageBox, "warning", lambda *_args, **_kwargs: MB_Yes
            )
            win.close()


def test_close_event_releases_provider_and_reader_handle(app, monkeypatch):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "save_settings", lambda: True)
    win = MainWindow()
    closed = {"provider": False, "reader": False}

    class Provider:
        def close(self):
            closed["provider"] = True

    class ReaderHandle:
        def close(self):
            closed["reader"] = True

    win.panelL.provider = Provider()
    win.panelL.writer_fh = ReaderHandle()
    try:
        assert win.close()
        assert closed == {"provider": True, "reader": True}
        assert win.panelL.provider is None
        assert win.panelL.writer_fh is None
    finally:
        if win.isVisible():
            win.close()
