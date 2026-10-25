# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Текстовые файлы открываются в системном редакторе (Блокнот).

Реальный редактор не запускается: команда собирается отдельно и
подменяется, а роль «редактора» играет скрипт, который пишет файл-маркер.
Проверяется и путь GUI: выбранный в панели ``.txt`` из образа должен
открыться копией во временном каталоге, а сам образ — не измениться.
"""

import os
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from mountfile import text_editor  # noqa: E402

# pytest.importorskip здесь не работает: mountfile.qt_compat бросает
# ImportError сам, но pytest 8.2+ считает «нет Qt» ошибкой (reason=... только
# для отсутствующего модуля), поэтому GUI-тест падал вместо skip на джобах
# без PyQt. Проверяем доступность Qt вручную.
try:  # pragma: no cover - зависит от окружения
    import mountfile.qt_compat as _qt_compat  # noqa: F401

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

needs_qt = pytest.mark.skipif(not HAVE_QT, reason="нет установленных Qt-биндингов")


class FakeProvider:
    """Минимальный провайдер образа: только ``read_file``."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def read_file(self, path):
        self.calls.append(path)
        return self.files[path]


# --------------------------------------------------------------------- kinds
def test_is_text_file():
    assert text_editor.is_text_file("README.TXT")
    assert text_editor.is_text_file("notes.txt")
    assert text_editor.is_text_file("config.ini")
    assert text_editor.is_text_file("dump.log")
    assert not text_editor.is_text_file("kernel.img")
    assert not text_editor.is_text_file("photo.png")
    assert not text_editor.is_text_file("noext")


# ------------------------------------------------------------------ команда
def test_editor_command_uses_env_override(tmp_path):
    fake = tmp_path / "myedit"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    old = {k: os.environ.get(k) for k in ("MOUNTFILE_EDITOR", "VISUAL", "EDITOR")}
    try:
        os.environ["MOUNTFILE_EDITOR"] = "%s --wait" % fake
        argv = text_editor.editor_command("/tmp/x.txt")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if os.name == "nt":  # pragma: no cover - на Windows ветка Блокнота
        assert argv[0].lower().endswith("notepad.exe")
    else:
        assert argv[:2] == [str(fake), "--wait"], argv
        assert argv[-1] == "/tmp/x.txt"


def test_open_in_editor_starts_the_command(tmp_path):
    if os.name == "nt":  # pragma: no cover - shell-скрипт только для POSIX
        pytest.skip("проверка запуска командой строки — только POSIX")

    marker = tmp_path / "opened.txt"
    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\nprintf "%%s" "$1" > "%s"\n' % marker)
    editor.chmod(0o755)

    target = tmp_path / "note.txt"
    target.write_text("привет")

    old = os.environ.get("MOUNTFILE_EDITOR")
    os.environ["MOUNTFILE_EDITOR"] = str(editor)
    try:
        argv = text_editor.open_in_editor(target)
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
    finally:
        if old is None:
            os.environ.pop("MOUNTFILE_EDITOR", None)
        else:
            os.environ["MOUNTFILE_EDITOR"] = old

    assert argv == [str(editor), str(target)], argv
    assert marker.read_text() == str(target)


@pytest.mark.skipif(os.name == "nt", reason="POSIX editor command behavior")
@pytest.mark.parametrize(
    ("candidate", "wait_flag"),
    [("gedit", "--wait"), ("kate", "--block")],
)
def test_default_linux_editors_are_started_in_wait_mode(
    monkeypatch, tmp_path, candidate, wait_flag
):
    monkeypatch.setattr(text_editor.sys, "platform", "linux")
    monkeypatch.setattr(text_editor, "_editor_from_env", lambda: None)
    monkeypatch.setattr(
        text_editor.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name == candidate else None,
    )

    argv = text_editor.editor_command(tmp_path / "copy.txt")

    assert argv == ["/usr/bin/" + candidate, wait_flag, str(tmp_path / "copy.txt")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX editor command behavior")
def test_editor_command_does_not_fall_back_to_detached_xdg_open(monkeypatch, tmp_path):
    monkeypatch.setattr(text_editor.sys, "platform", "linux")
    monkeypatch.setattr(text_editor, "_editor_from_env", lambda: None)
    monkeypatch.setattr(
        text_editor.shutil,
        "which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )

    assert text_editor.editor_command(tmp_path / "copy.txt") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX editor command behavior")
def test_detached_env_launcher_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(text_editor.sys, "platform", "linux")
    monkeypatch.setattr(text_editor, "_editor_from_env", lambda: ["xdg-open"])

    assert text_editor.editor_command(tmp_path / "copy.txt") is None


# --------------------------------------------------------------- кодировки
def test_decode_for_editor_keeps_utf8_and_fixes_cp1251():
    assert text_editor.decode_for_editor("Привет".encode("utf-8")) == (
        "Привет".encode("utf-8"),
        "utf-8",
    )

    payload, encoding = text_editor.decode_for_editor("Привет".encode("cp1251"))
    assert encoding == "cp1251"
    assert payload.startswith(b"\xef\xbb\xbf"), payload[:8]
    assert payload.decode("utf-8-sig") == "Привет"

    assert text_editor.decode_for_editor(b"\x00\x01\x02") == (b"\x00\x01\x02", "binary")


def test_prepare_image_text_writes_a_host_copy(tmp_path):
    provider = FakeProvider({"/README.TXT": "строка".encode("cp1251")})
    dest, encoding = text_editor.prepare_image_text(provider, "/README.TXT", tmp_path)

    assert provider.calls == ["/README.TXT"]
    assert dest.name == "README.TXT"
    assert dest.parent == tmp_path
    assert dest.read_bytes().decode("utf-8-sig") == "строка"
    assert encoding == "cp1251"


def test_safe_name_never_escapes_the_directory():
    assert text_editor.safe_name("..") == "file.txt"
    assert text_editor.safe_name("a/b:c.txt") == "a_b_c.txt"


# --------------------------------------------------------------------- GUI
@needs_qt
def test_gui_opens_txt_from_image_in_editor(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")

    from mountfile.qt_compat import QApplication, QMessageBox
    from mountfile.tc_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    panel = win.panelL
    panel.mode = "img"
    panel.path = "/"
    panel.provider = FakeProvider({"/notes.txt": "текст".encode("cp1251")})
    panel.selected_entry = lambda: {"name": "notes.txt", "is_dir": False, "size": 9}

    seen = {}

    def fake_open(path):
        seen["path"] = str(path)
        return ["fake-editor", str(path)]

    monkeypatch.setattr(text_editor, "open_in_editor", fake_open)
    monkeypatch.setattr(win, "_text_temp_dir", lambda: str(tmp_path))
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: messages.append(args)
    )

    win.open_text_file(panel)

    assert seen["path"].endswith("notes.txt"), seen
    with open(seen["path"], "rb") as stream:
        assert stream.read().decode("utf-8-sig") == "текст"
    assert panel.provider.calls == ["/notes.txt"]
    assert messages and messages[-1][1] == "Образ только для чтения"
    assert seen["path"] in messages[-1][2]
    log = win.log.toPlainText()
    assert "Блокнот" in log and "notes.txt" in log, log[-300:]

    # Enter / двойной щелчок по .txt идёт тем же путём, а не в HEX-просмотр
    win.f3_view()
    assert seen["path"].endswith("notes.txt"), seen

    win.close()
    assert app is not None


@needs_qt
@pytest.mark.parametrize("encoding", ["cp1251", "cp866"])
def test_external_editor_copy_is_imported_only_after_confirmation(
    tmp_path, monkeypatch, encoding
):
    from pathlib import Path

    from mountfile import tc_window
    from mountfile.qt_compat import MB_Yes, QMessageBox

    original = "[main]\r\nname=старый Ш\r\n".encode(encoding)
    updated = "[main]\r\nname=новый Ш\r\n".encode(encoding)

    class Provider:
        def __init__(self):
            self.calls = 0

        def read_file(self, path):
            assert path == "/notes.ini"
            self.calls += 1
            return original

    class Writer:
        def __init__(self):
            self.calls = []

        def write_file(self, directory, name, data):
            self.calls.append((directory, name, data))

    class Panel:
        mode = "img"
        path = "/"

        def __init__(self):
            self.provider = Provider()
            self.writer = Writer()
            self.refreshed = False

        def join(self, name):
            return "/" + name

        def refresh(self):
            self.refreshed = True

    class Window:
        _import_image_text_copy = tc_window.MainWindow._import_image_text_copy

        def __init__(self):
            self.logs = []
            self.backup_calls = 0

        def _text_temp_dir(self):
            return str(tmp_path)

        def log_msg(self, message):
            self.logs.append(message)

        def _backup_before_write(self, panel):
            self.backup_calls += 1
            return True

    panel = Panel()
    window = Window()
    entry = {"name": "notes.ini", "is_dir": False, "size": len(original)}

    def fake_open(path):
        Path(path).write_bytes(
            b"\xef\xbb\xbf" + "[main]\r\nname=новый Ш\r\n".encode("utf-8")
        )
        return ["fake-editor", str(path)]

    monkeypatch.setattr(text_editor, "open_in_editor", fake_open)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: MB_Yes)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: None)

    tc_window.MainWindow._open_text_entry(window, panel, entry)

    assert panel.writer.calls == [("/", "notes.ini", updated)]
    assert panel.refreshed
    assert window.backup_calls == 1
    assert any("записаны в образ" in message for message in window.logs)


@needs_qt
def test_external_editor_refuses_to_overwrite_concurrent_image_change(
    tmp_path, monkeypatch
):
    from mountfile import tc_window
    from mountfile.qt_compat import QMessageBox

    original = b"key=before\r\n"

    class Provider:
        def read_file(self, path):
            assert path == "/notes.ini"
            return b"key=changed-elsewhere\r\n"

    class Writer:
        def __init__(self):
            self.calls = []

        def write_file(self, *args):
            self.calls.append(args)

    class Panel:
        mode = "img"
        path = "/"
        provider = Provider()
        writer = Writer()

        def join(self, name):
            return "/" + name

    class Window:
        def __init__(self):
            self.logs = []
            self.backup_calls = 0

        def log_msg(self, message):
            self.logs.append(message)

        def _backup_before_write(self, panel):
            self.backup_calls += 1
            return True

    target = tmp_path / "notes.ini"
    target.write_bytes(b"key=edited\r\n")
    errors = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: errors.append(args[-1]),
    )

    window = Window()
    panel = Panel()
    entry = {"name": "notes.ini"}
    tc_window.MainWindow._import_image_text_copy(
        window, panel, entry, str(target), original, "utf-8"
    )

    assert not panel.writer.calls
    assert not window.backup_calls
    assert errors and "изменился после открытия" in errors[0]
