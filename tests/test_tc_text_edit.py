# SPDX-License-Identifier: MIT
"""F4 text-edit save and encoding preservation tests."""
import importlib.util
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

if not (importlib.util.find_spec("PyQt6") or importlib.util.find_spec("PyQt5")):
    pytest.skip("no Qt bindings installed", allow_module_level=True)

from mountfile import tc_window
from mountfile.ext_debugfs_writer import Ext234ImageWriter, Ext234Provider
from mountfile.qt_compat import (
    DialogAccepted,
    MB_No,
    MB_Yes,
    QApplication,
    QMessageBox,
)


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["MOUNTFILE_NO_UPDATE"] = "1"
    return QApplication.instance() or QApplication([])


def test_editable_text_preserves_cp1251_bom_and_crlf():
    original = b"\xef\xbb\xbf[main]\r\nname=\xcf\xf0\xe8\xe2\xe5\xf2\r\n"
    decoded = tc_window._decode_editable_text(original)
    assert decoded is not None
    text, encoding, bom, newline = decoded
    assert "name=Привет" in text
    assert tc_window._encode_editable_text(text + "\nkey=value", encoding, bom, newline) == (
        b"\xef\xbb\xbf[main]\r\nname=\xcf\xf0\xe8\xe2\xe5\xf2\r\n\r\nkey=value"
    )


def test_local_edit_uses_atomic_replace(tmp_path):
    target = tmp_path / "settings.ini"
    target.write_bytes(b"before\n")
    tc_window._atomic_write_local(str(target), b"after\n")
    assert target.read_bytes() == b"after\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.ini"]


def test_image_write_requires_backup_and_never_overwrites_existing_backup(
    monkeypatch, tmp_path
):
    image = tmp_path / "disk.img"
    image.write_bytes(b"evidence")
    original_backup = tmp_path / "disk.img.bak"
    original_backup.write_bytes(b"previous backup")

    class Window:
        image_path = str(image)

        def log_msg(self, _message):
            pass

    class Panel:
        mode = "img"

    window = Window()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: MB_No)
    assert not tc_window.MainWindow._backup_before_write(window, Panel())
    assert original_backup.read_bytes() == b"previous backup"
    assert not (tmp_path / "disk.img.bak.1").exists()

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: MB_Yes)
    assert tc_window.MainWindow._backup_before_write(window, Panel())
    assert original_backup.read_bytes() == b"previous backup"
    assert (tmp_path / "disk.img.bak.1").read_bytes() == b"evidence"


def test_f4_persists_edited_image_text(monkeypatch, qapp):
    class Provider:
        def read_file(self, path):
            assert path == "/config/factorySrv.ini"
            return b"value=old\r\n"

    class Writer:
        def __init__(self):
            self.calls = []

        def write_file(self, directory, name, data):
            self.calls.append((directory, name, data))

    class Panel:
        mode = "img"
        path = "/config"
        provider = Provider()
        writer = Writer()

        def selected_entry(self):
            return {"name": "factorySrv.ini", "is_dir": False}

        def join(self, name):
            return self.path.rstrip("/") + "/" + name

        def refresh(self):
            self.refreshed = True

    panel = Panel()

    class Window:
        image_path = "disk.img"

        def active_panel(self):
            return panel

        def log_msg(self, _message):
            pass

        def _backup_before_write(self, _panel):
            return True

    class Dialog:
        def __init__(self, _parent, _name, text):
            self.updated = text
            self.ends_with_newline = text.endswith("\n")

        def text(self):
            if self.ends_with_newline and not self.updated.endswith("\n"):
                return self.updated + "\n"
            return self.updated

    window = Window()

    def edit_dialog(dialog):
        dialog.updated = "value=new"
        return DialogAccepted

    monkeypatch.setattr(tc_window, "TextEditorDialog", Dialog)
    monkeypatch.setattr(tc_window, "exec_dialog", edit_dialog)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: MB_Yes)

    tc_window.MainWindow.f4_edit(window)

    assert panel.writer.calls == [("/config", "factorySrv.ini", b"value=new\r\n")]
    assert panel.refreshed is True


@pytest.mark.skipif(
    not (shutil.which("debugfs") and shutil.which("mke2fs")),
    reason="e2fsprogs debugfs and mke2fs are required",
)
def test_f4_saves_existing_ext4_ini_through_real_backend(monkeypatch, tmp_path, qapp):
    image = tmp_path / "factorydata.ext4"
    with image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run(
        [shutil.which("mke2fs"), "-q", "-F", "-t", "ext4", str(image)],
        check=True,
    )
    backend = Ext234ImageWriter(str(image))
    backend.mkdir("/config")
    backend.write_file("/config/factorySrv.ini", b"UARTOnOff=0\r\n")

    class Panel:
        mode = "img"
        path = "/config"

        def __init__(self):
            self.provider = Ext234Provider(str(image))
            self.writer = tc_window._ExtPanelWriter(backend)
            self.refreshed = False

        def selected_entry(self):
            return {"name": "factorySrv.ini", "is_dir": False, "size": 13}

        def join(self, name):
            return self.path.rstrip("/") + "/" + name

        def refresh(self):
            self.refreshed = True

    panel = Panel()

    class Window:
        image_path = str(image)

        def active_panel(self):
            return panel

        def log_msg(self, _message):
            pass

        def _backup_before_write(self, _panel):
            return True

    class Dialog:
        def __init__(self, _parent, _name, text):
            self.updated = text
            self.ends_with_newline = text.endswith("\n")

        def text(self):
            return self.updated + ("\n" if self.ends_with_newline and not self.updated.endswith("\n") else "")

    def edit_dialog(dialog):
        dialog.updated = "UARTOnOff=1"
        return DialogAccepted

    monkeypatch.setattr(tc_window, "TextEditorDialog", Dialog)
    monkeypatch.setattr(tc_window, "exec_dialog", edit_dialog)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: MB_Yes)

    tc_window.MainWindow.f4_edit(Window())

    assert backend.read_file("/config/factorySrv.ini") == b"UARTOnOff=1\r\n"
    assert panel.refreshed is True


@pytest.mark.skipif(
    not (shutil.which("debugfs") and shutil.which("mke2fs")),
    reason="e2fsprogs debugfs and mke2fs are required",
)
def test_mount_ext_uses_debugfs_fallback_without_pytsk3(monkeypatch, tmp_path):
    partition = tmp_path / "data.ext4"
    with partition.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024)
    subprocess.run(
        [shutil.which("mke2fs"), "-q", "-F", "-t", "ext4", str(partition)],
        check=True,
    )
    initial = Ext234ImageWriter(str(partition))
    initial.mkdir("/config")
    initial.write_file("/config/factorySrv.ini", b"UARTOnOff=0\r\n")

    image = tmp_path / "disk.img"
    offset = 1024 * 1024
    prefix = b"\x00" * offset
    with image.open("wb") as target, partition.open("rb") as source:
        target.write(prefix)
        shutil.copyfileobj(source, target)

    import mountfile.vfs as vfs

    def no_tsk(*_args, **_kwargs):
        raise vfs.TskError("simulated pytsk3 absence")

    monkeypatch.setattr(vfs, "TskImage", no_tsk)

    class Panel:
        def __init__(self):
            self.provider = None
            self.writer = None
            self.writer_fh = None
            self.marked = set()

        def _rebuild_tabs(self):
            pass

        def refresh(self):
            pass

        def populate_drives(self):
            pass

    panel = Panel()

    class Window:
        image_path = str(image)
        volumes = [SimpleNamespace(offset=offset, size=8 * 1024 * 1024)]

        def release_volume(self, _panel):
            pass

        def log_msg(self, _message):
            pass

        def _update_cmd_prompt(self):
            pass

    assert tc_window.MainWindow.mount_volume(Window(), panel, offset)
    assert panel.provider.read_file("/config/factorySrv.ini") == b"UARTOnOff=0\r\n"
    assert panel.writer is not None
    panel.writer.write_file("/config", "factorySrv.ini", b"UARTOnOff=1\r\n")
    assert panel.provider.read_file("/config/factorySrv.ini") == b"UARTOnOff=1\r\n"


def test_text_dialog_preserves_existing_terminal_newline(qapp):
    dialog = tc_window.TextEditorDialog(None, "factorySrv.ini", "old=value\n")
    dialog.editor.setPlainText("new=value")
    assert dialog.text() == "new=value\n"
    dialog.deleteLater()
    qapp.processEvents()
