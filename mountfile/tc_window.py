# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Total Commander-style dual-pane main window (Qt5 / Qt6 via qt_compat).

The layout reproduces the classic Total Commander window: menu bar, icon
toolbar, two panels - each with its own drive bar (drive combo + free space
readout + root/up buttons), folder tabs, the ``*.*`` path line, the file
list with the columns Имя / Тип / Размер / Дата / Атрибуты and its own
status line - then the command line and the function key bar.

Function keys (real shortcuts, also clickable buttons at the bottom):

    F3  view the selected file - a text file (``.txt`` and the other plain
        text kinds) opens in the system editor (Блокнот on Windows), anything
        else in the hex viewer
    F4  edit a text file in the built-in bounded editor: a local file is saved
        atomically, a file inside the image is written through the volume
        writer (FAT32 or ext2/3/4 via ``debugfs``) after an explicit
        confirmation and a fresh, non-overwriting image backup
    F5  copy between panels (image -> local = extract,
        local -> image = import via the selected filesystem writer)
    F6  move (source removed when the engine can remove it)
    F7  make a directory in the active panel
    F8  delete (local filesystem or a writable image backend)
    F9  toggle the menu bar

Keys inside a panel: Enter / double click opens, Backspace goes up, Tab
switches the active panel, Space marks the cursor row (Insert marks and
moves down), Ctrl+A marks everything, Ctrl+T opens a tab, Ctrl+W closes it,
Ctrl+U swaps the panels. Entries marked with Space are what F5/F6/F8 act
on. The command line understands ``cd <path>``.

All colours, fonts and column formatting live in :mod:`mountfile.tc_style`.
"""

from __future__ import annotations

import atexit
import fnmatch
import os
import shutil
import stat
import tempfile
import threading
import webbrowser
from pathlib import PurePosixPath

from . import ai, autopick, components, tc_style, text_editor
from .dfir import detect_timestomp, grep_image, read_timeline_csv, sha256_file
from .hexview import HexView
from .partition import list_partitions
from .qt_compat import (
    AlignRight,
    AlignVCenter,
    AscendingOrder,
    BottomDock,
    ComboAdjustToMinimumContentsLengthWithIcon,
    CtrlModifier,
    CustomContextMenu,
    DescendingOrder,
    DialogAccepted,
    DlgActionRole,
    DlgCancel,
    DlgClose,
    DlgOk,
    EchoPassword,
    ElideMiddle,
    HVC_Interactive,
    ItemIsEnabled,
    ItemIsSelectable,
    Key_A,
    Key_Backspace,
    Key_Delete,
    Key_Enter,
    Key_Insert,
    Key_Return,
    Key_Space,
    Key_Tab,
    MB_No,
    MB_Yes,
    NoEditTriggers,
    QAction,
    QApplication,
    QBrush,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QEvent,
    QEvent_KeyPress,
    QEventLoop,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QKeySequence,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSize,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    Qt,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTimer,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    SelectRows,
    SizePolicyExpanding,
    SizePolicyFixed,
    Stretch,
    TableExtendedSelection,
    TB_InstantPopup,
    TB_TextBesideIcon,
    WidgetWithChildrenShortcut,
    exec_dialog,
    standard_icon,
)
from .updater import StartupUpdater, app_version, auto_install
from .updater import disabled as updates_disabled
from .updater import is_frozen

#: The five columns of the reference interface, in order.
COLUMNS = ("Имя", "Тип", "Размер", "Дата", "Атрибуты")

#: What Total Commander prints in the size column for a directory.
DIR_SIZE_TEXT = "<Папка>"


class _UniversalImageWriter:
    """Adapt :class:`UniversalFSRW` to the GUI's legacy writer contract.

    The panel UI historically called the native FAT32 writer with
    ``(directory, name, data)`` arguments.  The package RW facade uses one
    POSIX path instead.  Keeping this adapter in the GUI avoids changing the
    tested FAT32 engine while enabling the same commands for ext2/3/4, NTFS,
    and an available loop-mount backend.
    """

    def __init__(self, filesystem, provider=None):
        self.filesystem = filesystem
        self.provider = provider
        info = filesystem.info()
        self.total_bytes = int(info.total_size or 0)
        self.fs_type = info.fs_type
        self.write_strategy = info.write_strategy
        self.writable = bool(info.writable)
        self.write_capabilities = tuple(info.write_capabilities)
        self.write_rejection_reason = info.write_rejection_reason

    @staticmethod
    def _join(directory, name):
        directory = str(directory or "/").replace("\\", "/")
        name = str(name).replace("\\", "/")
        if name.startswith("/"):
            return str(PurePosixPath(name))
        return str(PurePosixPath(directory) / name)

    def _mutate(self, operation):
        close = getattr(self.provider, "close", None)
        reopen = getattr(self.provider, "reopen", None)
        release_reader = self.write_strategy == "imdisk-rw" and callable(close)
        if release_reader:
            close()
        try:
            return operation()
        finally:
            if release_reader and callable(reopen):
                reopen()

    def mkdir(self, directory, name):
        self._mutate(lambda: self.filesystem.mkdir(self._join(directory, name)))

    def write_file(self, directory, name, data):
        self._mutate(
            lambda: self.filesystem.write_file(self._join(directory, name), data)
        )

    def rename(self, source, destination):
        source = str(source).replace("\\", "/")
        destination = str(destination).replace("\\", "/")
        if not destination.startswith("/"):
            destination = self._join(str(PurePosixPath(source).parent), destination)
        self._mutate(lambda: self.filesystem.rename(source, destination))

    def delete(self, path, recursive=False):
        self._mutate(lambda: self.filesystem.delete(path, recursive=recursive))

    def close(self):
        self.filesystem.close()

    def free_space(self):
        # The package facade deliberately does not guess free space for every
        # filesystem.  Returning None keeps the GUI status truthful.
        return None


#: What the Имя / Тип / Размер / Дата / Атрибуты header row reads.
#: The flexible Имя column is `COLUMN_NAME`; the other four keep a width
#: derived from their own header and reference content (see
#: :meth:`PanelTable._column_widths`) so no header is ever cut off.
COLUMN_NAME = 0
#: Smallest usable column (Qt cuts a section to `minimumSectionSize`, and the
#: default squeezed the Имя column down to 's..').
MIN_SECTION_WIDTH = 40
#: The Имя column never goes below this, the other columns give up width first.
NAME_MIN_WIDTH = 150

#: How many bytes the F3 viewer shows, and the chunk one AI read pulls.
PREVIEW_LIMIT = 1 << 20

#: Extensions the built-in F4 editor accepts (bounded text files only).
EDITABLE_TEXT_EXTENSIONS = (
    ".ini",
    ".cfg",
    ".conf",
    ".txt",
    ".log",
    ".json",
    ".xml",
    ".csv",
    ".yaml",
    ".yml",
    ".md",
)

#: Hard size limit of one F4 edit; the reader also refuses larger files.
MAX_EDIT_BYTES = 2 * 1024 * 1024

#: One-line reminder of the panel keys, shown under an empty panel.
PANEL_LEGEND = (
    "Enter — открыть, Backspace — вверх, Space — отметить, "
    "Ctrl+T — вкладка, Tab — другая панель"
)

#: F3..F8 as shown by the function key bar (the F9 menu toggle stays a
#: shortcut only, exactly like the reference window).
FKEYS = (
    ("F3", "Просмотр/HEX"),
    ("F4", "Правка"),
    ("F5", "Копирование"),
    ("F6", "Перемещение"),
    ("F7", "Каталог"),
    ("F8", "Удаление"),
)

IMAGE_FILTER = "Образы дисков (*.img *.bin *.raw *.dump *.dd);;" "Все файлы (*)"

#: Filter of the «AI → файл с диска» picker: any file first, images after.
AI_FILE_FILTER = (
    "Все файлы (*);;"
    "Образы дисков (*.img *.bin *.raw *.dump *.dd);;"
    "Текстовые файлы (*.txt *.log *.json *.csv *.xml)"
)


def local_drives():
    """(label, path) pairs of the places a panel may browse locally."""
    out = []
    if os.name == "nt":  # pragma: no cover - Windows only
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = "%s:\\" % letter
            if os.path.exists(root):
                out.append(("%s:" % letter, root))
    else:
        out.append(("/", "/"))
        out.append(("~", os.path.expanduser("~")))
    return out


#: Фоновые потоки, отсоединённые от закрытого окна.
#:
#: Работающий QThread нельзя уничтожать: Qt печатает «QThread: Destroyed
#: while thread is still running» и завершает процесс с ``Fatal Python
#: error: Aborted``. Именно это убивало и CI, и само приложение при выходе
#: во время проверки обновлений. Ссылка на такой поток переезжает сюда.
_ORPHAN_THREADS = []


def drain_orphan_threads(timeout_ms=250):
    """Дать отсоединённым потокам завершиться, не блокируя GUI надолго."""
    for thread in list(_ORPHAN_THREADS):
        try:
            if thread.isRunning():
                thread.wait(timeout_ms)
            if not thread.isRunning():
                _ORPHAN_THREADS.remove(thread)
        except Exception:  # pragma: no cover - поток уже уничтожен
            if thread in _ORPHAN_THREADS:
                _ORPHAN_THREADS.remove(thread)


atexit.register(drain_orphan_threads)


class ViewerDialog(QDialog):
    """F3 full-file HEX editor with an optional persistence callback."""

    def __init__(self, parent, name, size, data, save_callback=None):
        super().__init__(parent)
        self.setWindowTitle("Просмотр — %s" % name)
        self._fit_to_screen()
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "%s   ·   %s байт   ·   загружено %d байт"
                % (name, tc_style.fmt_size(size), len(data))
            )
        )
        self.hex_view = HexView()
        self.hex_view.setData(data)
        lay.addWidget(self.hex_view, 1)
        buttons = QHBoxLayout()
        save = QPushButton("Сохранить")
        save.setEnabled(save_callback is not None)
        save.clicked.connect(self._save)
        self.hex_view.dataChanged.connect(
            lambda: save.setEnabled(
                save_callback is not None and self.hex_view.isModified()
            )
        )
        buttons.addWidget(save)
        buttons.addStretch(1)
        close = QPushButton("Закрыть (Esc)")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        self._save_callback = save_callback

    def _save(self):
        if self._save_callback is None or not self.hex_view.isModified():
            return
        try:
            if self._save_callback(self.hex_view.data()):
                self.hex_view.setData(self.hex_view.data())
        except Exception as exc:
            QMessageBox.critical(self, "Сохранение HEX не удалось", str(exc))

    def _fit_to_screen(self):
        """Viewer takes ~90% of the monitor work area, not fixed 820x560."""
        geo = None
        try:
            geo = self.screen().availableGeometry()
        except Exception:
            geo = None
        if geo is None:
            screen = QApplication.primaryScreen()
            geo = screen.availableGeometry() if screen else None
        if geo is not None:
            self.resize(
                max(400, geo.width() * 9 // 10), max(300, geo.height() * 9 // 10)
            )


class HexEditorPage(QWidget):
    """Full-window HEX editor page; intentionally not a modal dialog."""

    def __init__(self, parent, name, size, data, save_callback, close_callback):
        super().__init__(parent)
        self._save_callback = save_callback
        self._close_callback = close_callback
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        title = QLabel(
            "HEX-редактор  ·  %s  ·  %s байт" % (name, tc_style.fmt_size(size))
        )
        title.setObjectName("panelTitle")
        top.addWidget(title, 1)
        self.status = QLabel("Готово")
        self.status.setObjectName("panelSpace")
        top.addWidget(self.status)
        layout.addLayout(top)
        self.hex_view = HexView()
        self.hex_view.setData(data)
        self.hex_view.dataChanged.connect(self._changed)
        layout.addWidget(self.hex_view, 1)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        find = QPushButton("Найти")
        find.clicked.connect(self.find)
        buttons.addWidget(find)
        self.goto_button = QPushButton("Перейти…")
        self.goto_button.clicked.connect(self.goto)
        buttons.addWidget(self.goto_button)
        buttons.addStretch(1)
        back = QPushButton("Назад к панелям")
        back.clicked.connect(self.request_close)
        buttons.addWidget(back)
        layout.addLayout(buttons)
        self._shortcut_actions = []
        for shortcut, callback in (
            ("Ctrl+S", self.save),
            ("Ctrl+F", self.find),
            ("Ctrl+G", self.goto),
        ):
            action = QAction(shortcut, self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(WidgetWithChildrenShortcut)
            action.triggered.connect(lambda *_args, slot=callback: slot())
            self.addAction(action)
            self._shortcut_actions.append(action)
        self._changed()
        self.hex_view.setFocus()

    def _changed(self):
        modified = self.hex_view.isModified()
        can_save = self._save_callback is not None
        self.save_button.setEnabled(modified and can_save)
        if not can_save:
            self.status.setText(
                "Изменения не сохраняются: том только для чтения"
                if modified
                else "Только чтение"
            )
        else:
            self.status.setText(
                "Есть несохранённые изменения" if modified else "Сохранено"
            )

    def save(self):
        if not self.hex_view.isModified() or self._save_callback is None:
            return
        try:
            if self._save_callback(self.hex_view.data()):
                self.hex_view.setData(self.hex_view.data())
        except Exception as exc:
            QMessageBox.critical(self, "Сохранение HEX не удалось", str(exc))

    def find(self):
        value, ok = QInputDialog.getText(self, "Поиск байтов", "HEX или текст:")
        if not ok or not value:
            return
        try:
            needle = bytes.fromhex("".join(value.split()))
        except ValueError:
            needle = value.encode("utf-8")
        pos = self.hex_view.find(needle)
        self.status.setText("Найдено: 0x%X" % pos if pos >= 0 else "Не найдено")

    def goto(self):
        value, ok = QInputDialog.getText(
            self, "Перейти к смещению", "Смещение (десятичное или 0x…):"
        )
        if not ok or not value:
            return
        try:
            offset = int(value.strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "Перейти к смещению", "Некорректное смещение.")
            return
        if not self.hex_view.goto(offset):
            QMessageBox.information(
                self, "Перейти к смещению", "Нет загруженных данных."
            )
        else:
            actual = min(max(offset, 0), len(self.hex_view.data()) - 1)
            self.status.setText("Смещение: 0x%X" % actual)

    def request_close(self):
        if self.hex_view.isModified():
            answer = QMessageBox.warning(
                self,
                "Несохранённые изменения",
                "Вернуться к панелям и отбросить изменения HEX?",
                MB_Yes | MB_No,
                MB_No,
            )
            if answer != MB_Yes:
                return
        self._close_callback()


class TextEditorDialog(QDialog):
    """Small text-only F4 editor; binary formats remain view-only."""

    def __init__(self, parent, name, text):
        super().__init__(parent)
        self.setWindowTitle("Правка — %s" % name)
        self.resize(760, 560)
        self._ends_with_newline = text.endswith("\n")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Изменения попадут в файл только после нажатия «Сохранить».")
        )
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        layout.addWidget(self.editor, 1)
        buttons = QHBoxLayout()
        save = QPushButton("Сохранить")
        cancel = QPushButton("Отмена")
        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def text(self):
        text = self.editor.toPlainText()
        if self._ends_with_newline and not text.endswith("\n"):
            text += "\n"
        return text


class _ExtPanelWriter:
    """Adapt the ext writer to the FAT32-style two-argument GUI contract."""

    def __init__(self, backend, provider=None):
        self.backend = backend
        self.provider = provider

    def _mutate(self, operation):
        close = getattr(self.provider, "close", None)
        reopen = getattr(self.provider, "reopen", None)
        release_reader = os.name == "nt" and callable(close)
        if release_reader:
            close()
        try:
            return operation()
        finally:
            if release_reader and callable(reopen):
                reopen()

    @staticmethod
    def _join(directory, name):
        directory = (directory or "/").replace("\\", "/").rstrip("/")
        return (directory + "/" + name) if directory else ("/" + name)

    def write_file(self, directory, name, data):
        self._mutate(lambda: self.backend.write_file(self._join(directory, name), data))

    def mkdir(self, directory, name):
        self._mutate(lambda: self.backend.mkdir(self._join(directory, name)))

    def rename(self, source, new_name):
        parent = source.replace("\\", "/").rsplit("/", 1)[0] or "/"
        self._mutate(lambda: self.backend.rename(source, self._join(parent, new_name)))

    def delete(self, path, recursive=False):
        self._mutate(lambda: self.backend.delete(path, recursive=recursive))

    def close(self):
        self.backend.close()

    def free_space(self):
        return self.backend.free_space()

    @property
    def total_bytes(self):
        return self.backend.filesystem_size


def _decode_editable_text(data, encoding=None):
    """Return normalized text and its encoding/newline metadata, or None."""
    if len(data) > 2 * 1024 * 1024 or b"\x00" in data:
        return None
    bom = data.startswith(b"\xef\xbb\xbf")
    content = data[3:] if bom else data
    if encoding is None:
        encoding = "utf-8"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "cp1251"
            try:
                text = content.decode(encoding)
            except UnicodeDecodeError:
                return None
    else:
        if encoding not in ("utf-8", "cp1251", "cp866"):
            return None
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            return None
    newline = "\r\n" if b"\r\n" in data else "\r" if b"\r" in data else "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, bom, newline


def _encode_editable_text(text, encoding, bom, newline):
    content = text.replace("\r\n", "\n").replace("\r", "\n")
    data = content.replace("\n", newline).encode(encoding)
    return (b"\xef\xbb\xbf" + data) if bom else data


def _local_file_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _file_contents_match(stream, expected):
    offset = 0
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            return offset == len(expected)
        end = offset + len(chunk)
        if end > len(expected) or expected[offset:end] != chunk:
            return False
        offset = end


def _atomic_write_local(path, data, expected_data=None, expected_identity=None):
    """Atomically replace a regular local file without clobbering later edits."""
    parent = os.path.dirname(os.path.abspath(path))
    original = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(original.st_mode):
        raise RuntimeError("refusing to replace a non-regular local file")
    baseline = expected_identity or _local_file_identity(original)

    def verify_unchanged():
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or _local_file_identity(current) != baseline
        ):
            raise RuntimeError(
                "local file changed since it was opened; refusing to overwrite"
            )
        with open(path, "rb") as source:
            opened = os.fstat(source.fileno())
            # On Windows, fstat metadata can differ from path stat after
            # os.replace; path identity is checked on both sides of this read.
            if not stat.S_ISREG(opened.st_mode) or (
                expected_data is not None
                and not _file_contents_match(source, expected_data)
            ):
                raise RuntimeError(
                    "local file changed since it was opened; refusing to overwrite"
                )
        current = os.stat(path, follow_symlinks=False)
        if _local_file_identity(current) != baseline:
            raise RuntimeError(
                "local file changed since it was opened; refusing to overwrite"
            )

    verify_unchanged()
    fd, temp_path = tempfile.mkstemp(prefix=".mountfile-edit-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IMODE(original.st_mode))
        verify_unchanged()
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


class ForensicTriageDialog(QDialog):
    """Bounded, read-only forensic helpers for the currently opened image."""

    def __init__(self, parent, image_path):
        super().__init__(parent)
        self.image_path = image_path
        self.setWindowTitle("DFIR triage — только чтение")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Образ: %s" % (image_path or "не открыт")))

        search_row = QHBoxLayout()
        self.ed_pattern = QLineEdit()
        self.ed_pattern.setPlaceholderText(
            "Строка или regex для поиска в байтах образа"
        )
        self.cmb_encoding = QComboBox()
        for label, value in (
            ("UTF-8", "utf-8"),
            ("UTF-16LE", "utf-16le"),
            ("Latin-1", "latin-1"),
            ("Raw", "raw"),
        ):
            self.cmb_encoding.addItem(label, value)
        self.chk_regex = QCheckBox("regex")
        btn_search = QPushButton("Искать")
        btn_search.clicked.connect(self.search)
        search_row.addWidget(self.ed_pattern, 1)
        search_row.addWidget(self.cmb_encoding)
        search_row.addWidget(self.chk_regex)
        search_row.addWidget(btn_search)
        layout.addLayout(search_row)

        action_row = QHBoxLayout()
        for label, slot in (
            ("SHA-256", self.show_hash),
            ("Timeline CSV", self.show_timeline),
            ("Timestomp CSV", self.show_timestomp),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            action_row.addWidget(button)
        layout.addLayout(action_row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, AlignRight)

    def _require_image(self):
        if self.image_path and os.path.isfile(self.image_path):
            return True
        self.output.setPlainText("Откройте raw-образ через Ctrl+O перед DFIR-анализом.")
        return False

    def show_hash(self):
        if self._require_image():
            self.output.setPlainText("SHA-256: %s" % sha256_file(self.image_path))

    def search(self):
        if not self._require_image():
            return
        pattern = self.ed_pattern.text()
        if not pattern:
            self.output.setPlainText("Введите строку или regex для поиска.")
            return
        hits = list(
            grep_image(
                self.image_path,
                pattern,
                regex=self.chk_regex.isChecked(),
                encoding=self.cmb_encoding.currentData(),
                max_hits=200,
            )
        )
        if not hits:
            self.output.setPlainText("Совпадений не найдено.")
            return
        self.output.setPlainText(
            "\n".join(
                "0x%X (+%d): %s" % (h["offset"], h["length"], h["context"])
                for h in hits
            )
        )

    def _pick_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть timeline CSV", "", "CSV (*.csv);;Все файлы (*)"
        )
        return path

    def show_timeline(self):
        path = self._pick_csv()
        if not path:
            return
        records = list(read_timeline_csv(path))
        self.output.setPlainText(
            "\n".join(
                "%s\t%s\t%s\t%s" % (r.path, r.mtime or "", r.atime or "", r.ctime or "")
                for r in records[:1000]
            )
            + "\n\nRecords: %d" % len(records)
        )

    def show_timestomp(self):
        path = self._pick_csv()
        if not path:
            return
        findings = list(detect_timestomp(read_timeline_csv(path)))
        self.output.setPlainText(
            "\n".join(
                "%s\t%s" % (r.path, ", ".join(reasons))
                for r, reasons in findings[:1000]
            )
            + "\n\nFindings: %d" % len(findings)
        )


class ImageFileSearchDialog(QDialog):
    """Search filesystem entries by name across every detected image volume."""

    def __init__(self, parent):
        super().__init__(parent)
        self.owner = parent
        self.setWindowTitle("Поиск файла в образе")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Образ: %s" % (parent.image_path or "не открыт")))

        row = QHBoxLayout()
        self.pattern = QLineEdit("*")
        self.pattern.setPlaceholderText("Имя или маска, например *.db или config")
        self.pattern.returnPressed.connect(self.run_search)
        button = QPushButton("Искать во всём образе")
        button.clicked.connect(self.run_search)
        row.addWidget(self.pattern, 1)
        row.addWidget(button)
        layout.addLayout(row)

        self.status = QLabel("Введите имя или маску файла.")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Том", "ФС", "Путь", "Размер", "Offset"))
        self.table.setSelectionBehavior(SelectRows)
        self.table.setEditTriggers(NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.open_result)
        layout.addWidget(self.table, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, AlignRight)
        self.results = []

    def run_search(self):
        pattern = self.pattern.text().strip()
        if not pattern:
            self.status.setText("Введите имя или маску файла.")
            return
        self.status.setText("Поиск…")
        QApplication.processEvents()
        try:
            self.results = self.owner.search_files_in_image(pattern)
        except Exception as exc:
            self.results = []
            self.status.setText("Ошибка поиска: %s" % exc)
            return
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            values = (
                result["volume"],
                result["fstype"],
                result["path"],
                tc_style.fmt_size(result["size"]),
                "0x%X" % result["offset"],
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
        self.status.setText("Найдено файлов: %d" % len(self.results))

    def open_result(self, row, _column):
        if 0 <= row < len(self.results):
            self.accept()
            self.owner.open_image_search_result(self.results[row])


class PanelTable(QTableWidget):
    """File list with Total Commander key bindings."""

    def __init__(self, panel):
        super().__init__(0, len(COLUMNS))
        self.panel = panel
        self.setHorizontalHeaderLabels(list(COLUMNS))
        hdr = self.horizontalHeader()
        # Only Имя stretches. The other four get an explicit width, computed
        # from the header text and the longest value they hold: with
        # ResizeToContents they sized themselves to their content and, in a
        # narrow window, pushed Имя below the minimum section, which is why
        # the listing showed 's..' instead of folder names.
        hdr.setMinimumSectionSize(MIN_SECTION_WIDTH)
        hdr.setSectionResizeMode(COLUMN_NAME, Stretch)
        for c in range(1, len(COLUMNS)):
            hdr.setSectionResizeMode(c, HVC_Interactive)
        hdr.setStretchLastSection(False)
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(True)
        hdr.sectionClicked.connect(self.panel.on_header_clicked)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setAccessibleName("Список файлов панели %s" % panel.side)
        self.setWordWrap(False)
        # Total Commander elides in the middle: the tail of 'BACKUP_….bin'
        # stays visible, which is what tells two dumps apart.
        self.setTextElideMode(ElideMiddle)
        self.setSelectionBehavior(SelectRows)
        self.setSelectionMode(TableExtendedSelection)
        self.setEditTriggers(NoEditTriggers)
        self.doubleClicked.connect(lambda *_: panel.open_row(panel.table.currentRow()))
        self.setContextMenuPolicy(CustomContextMenu)
        self.customContextMenuRequested.connect(panel.show_context_menu)
        # A short hint only: the long legend used to pop up as a wide tooltip
        # that covered both panels. It now lives under an empty panel.
        self.setToolTip("Enter — открыть, Space — отметить, Tab — другая панель")
        self.apply_font()
        self.currentCellChanged.connect(lambda *_: panel.selection_changed())
        self.itemSelectionChanged.connect(panel.selection_changed)

    # ------------------------------------------------------- column widths
    def _text_width(self, text):
        fm = self.fontMetrics()
        advance = getattr(fm, "horizontalAdvance", None)
        return advance(text) if advance is not None else fm.width(text)

    def _column_widths(self):
        """Width of the four fixed columns, fitted to the current UI font.

        Everything is measured from real text, so the numbers follow the font
        size switch (Вид → Шрифт крупнее) and the Атрибуты header can never
        come out clipped at the right edge.
        """
        pad = 34
        return (
            max(52, self._text_width("Папка") + pad),
            max(76, self._text_width("7 818 182 656") + pad),
            max(104, self._text_width("19.01.2021 11:56") + pad),
            max(64, self._text_width(COLUMNS[4]) + pad),
        )

    def _relayout_columns(self):
        """Fit the columns to the viewport, protecting the Имя column.

        The fixed columns give width up first (down to `MIN_SECTION_WIDTH`),
        so a narrow window or a small high-DPI screen still shows readable
        file names instead of collapsing the name column.
        """
        viewport = self.viewport().width()
        if viewport <= 0:
            return
        widths = list(self._column_widths())
        # Secondary metadata yields to the file name on small screens.
        self.setColumnHidden(4, viewport < 560)
        self.setColumnHidden(3, viewport < 440)
        self.setColumnHidden(1, viewport < 320)
        fixed = sum(w for c, w in enumerate(widths, 1) if not self.isColumnHidden(c))
        self.horizontalHeader().setMinimumSectionSize(MIN_SECTION_WIDTH)
        if viewport - fixed < NAME_MIN_WIDTH:
            deficit = NAME_MIN_WIDTH - (viewport - fixed)
            visible = [c for c in range(1, len(COLUMNS)) if not self.isColumnHidden(c)]
            room = sum(max(0, widths[c - 1] - MIN_SECTION_WIDTH) for c in visible)
            if room > 0:
                for c in visible:
                    width = widths[c - 1]
                    widths[c - 1] = max(
                        MIN_SECTION_WIDTH,
                        width
                        - round(
                            (width - MIN_SECTION_WIDTH) * min(deficit, room) / room
                        ),
                    )
        for c, width in enumerate(widths, 1):
            self.setColumnWidth(c, width)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout_columns()

    def apply_font(self):
        """Re-fit the columns after the UI font size changed."""
        self.verticalHeader().setDefaultSectionSize(
            max(28, self.fontMetrics().height() + 12)
        )
        self._relayout_columns()

    def keyPressEvent(self, e):
        key = e.key()
        ctrl = bool(e.modifiers() & CtrlModifier)
        if key == Key_Backspace:
            self.panel.go_up()
            return
        if key in (Key_Return, Key_Enter):
            self.panel.open_row(self.currentRow())
            return
        if key == Key_Delete:
            self.panel.win.f8_delete()
            return
        if key == Key_Space:
            self.panel.toggle_mark(self.currentRow())
            return
        if key == Key_Insert:
            self.panel.toggle_mark(self.currentRow())
            self.panel.move_cursor(1)
            return
        if ctrl and key == Key_A:
            self.panel.mark_all()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.panel.win.set_active(self.panel.side)


class TcPanel(QWidget):
    """One panel: drive bar, folder tabs, path line, list, status line."""

    def __init__(self, side, win):
        super().__init__()
        self.side = side
        self.win = win
        self.mode = "img" if side == "L" else "local"
        self.path = "/" if self.mode == "img" else os.path.expanduser("~")
        self.provider = None
        self.writer = None
        self.writer_fh = None
        self.write_reason = ""
        self.volume_offset = 0
        self.volume_label = ""
        self.entries = []
        self.marked = set()
        self.tabs = [self.path]
        self.tab_index = 0
        self.sort_col = 0
        self.sort_rev = False
        self._listing_hint = None
        self.setObjectName("filePanel")
        self.setAttribute(getattr(Qt, "WidgetAttribute", Qt).WA_StyledBackground, True)
        self._build()
        self.populate_drives()
        self.refresh()

    # ----------------------------------------------------------------- UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        title_row = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("panelTitle")
        self.lbl_mode = QLabel()
        self.lbl_mode.setObjectName("sourceBadge")
        title_row.addWidget(self.lbl_title, 1)
        title_row.addWidget(self.lbl_mode)
        lay.addLayout(title_row)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.cmb_drive = QComboBox()
        self.cmb_drive.setMinimumWidth(110)
        self.cmb_drive.setMinimumContentsLength(12)
        self.cmb_drive.setSizeAdjustPolicy(ComboAdjustToMinimumContentsLengthWithIcon)
        self.cmb_drive.setAccessibleName("Источник файлов панели %s" % self.side)
        self.cmb_drive.setMaxVisibleItems(64)
        self.cmb_drive.currentIndexChanged.connect(self.drive_changed)
        bar.addWidget(self.cmb_drive, 1)
        self.btn_root = QPushButton()
        self.btn_root.setIcon(standard_icon(self, "SP_DirHomeIcon"))
        self.btn_root.setFixedWidth(34)
        self.btn_root.setToolTip("Корень диска")
        self.btn_root.setAccessibleName("Перейти в корень диска")
        self.btn_root.clicked.connect(self.go_root)
        bar.addWidget(self.btn_root)
        self.btn_up = QPushButton()
        self.btn_up.setIcon(standard_icon(self, "SP_ArrowUp"))
        self.btn_up.setFixedWidth(34)
        self.btn_up.setToolTip("На уровень вверх (Backspace)")
        self.btn_up.setAccessibleName("На уровень вверх")
        self.btn_up.clicked.connect(self.go_up)
        bar.addWidget(self.btn_up)
        lay.addLayout(bar)

        self.lbl_space = QLabel()
        self.lbl_space.setObjectName("panelSpace")
        self.lbl_space.setWordWrap(True)
        lay.addWidget(self.lbl_space)
        self.tabbar = QTabBar()
        self.tabbar.setExpanding(False)
        self.tabbar.setTabsClosable(True)
        self.tabbar.setUsesScrollButtons(True)
        self.tabbar.currentChanged.connect(self.tab_changed)
        self.tabbar.tabCloseRequested.connect(self.tab_closed)
        lay.addWidget(self.tabbar)
        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("panelPath")
        self.path_edit.returnPressed.connect(self.path_entered)
        self.path_edit.setToolTip("Введите путь и нажмите Enter")
        self.path_edit.setAccessibleName("Путь текущего каталога")
        lay.addWidget(self.path_edit)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("panelSearch")
        self.search_edit.setPlaceholderText("Фильтр по имени в этом каталоге…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Фильтр файлов текущего каталога")
        self.search_edit.setToolTip(
            "Фильтрует имена, не содержимое файлов. Ctrl+F — фокус"
        )
        self.search_edit.textChanged.connect(self.filter_changed)
        lay.addWidget(self.search_edit)

        self.table = PanelTable(self)
        lay.addWidget(self.table, 1)
        self.empty_state = QWidget()
        self.empty_state.setObjectName("emptyState")
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(12, 12, 12, 12)
        empty.setSpacing(6)
        empty.addStretch()
        self.empty_title = QLabel()
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setWordWrap(True)
        empty.addWidget(self.empty_title)
        self.empty_hint = QLabel()
        self.empty_hint.setObjectName("emptyHint")
        self.empty_hint.setWordWrap(True)
        empty.addWidget(self.empty_hint)
        self.empty_retry = QPushButton("Повторить")
        self.empty_retry.clicked.connect(self.refresh)
        empty.addWidget(self.empty_retry, 0, AlignRight)
        self.empty_open = QPushButton("Открыть образ…")
        self.empty_open.setObjectName("primaryButton")
        self.empty_open.setIcon(standard_icon(self, "SP_DialogOpenButton"))
        self.empty_open.clicked.connect(lambda: self.win.open_image())
        empty.addWidget(self.empty_open, 0, AlignRight)
        empty.addStretch()
        lay.addWidget(self.empty_state, 1)
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("panelStatus")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)
        for widget in (
            self.path_edit,
            self.search_edit,
            self.cmb_drive,
            self.tabbar,
            self.btn_root,
            self.btn_up,
            self.table.viewport(),
        ):
            widget.installEventFilter(self)
        self._rebuild_tabs()

    def eventFilter(self, obj, event):
        if event.type() in (
            getattr(QEvent, "Type", QEvent).FocusIn,
            getattr(QEvent, "Type", QEvent).MouseButtonPress,
        ):
            if hasattr(self.win, "panels"):
                self.win.set_active(self.side)
        return super().eventFilter(obj, event)

    def selection_changed(self):
        if hasattr(self.win, "panels"):
            self.win._sync_actions()

    def visible_entries(self):
        query = self.search_edit.text().strip().casefold()
        return [entry for entry in self.entries if query in entry["name"].casefold()]

    def filter_changed(self, _text):
        # Hidden marks must never be silently included in Copy/Delete.
        visible = {entry["name"] for entry in self.visible_entries()}
        self.marked.intersection_update(visible)
        self._fill(self._listing_hint)
        self.update_header()

    def _clear_navigation_state(self):
        self.marked.clear()
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)

    # --------------------------------------------------------------- paths
    def join(self, name):
        if self.mode == "local":
            return os.path.join(self.path, name)
        return (self.path.rstrip("/") + "/" + name) if self.path != "/" else "/" + name

    def up_path(self):
        if self.mode == "local":
            p = os.path.dirname(self.path.rstrip(os.sep)) or os.sep
            return None if p == self.path else p
        if self.path == "/":
            return None
        return self.path.rstrip("/").rsplit("/", 1)[0] or "/"

    def root_path(self):
        if self.mode == "local":
            if os.name == "nt":
                drive, _ = os.path.splitdrive(self.path)
                return (drive + os.sep) if drive else os.sep
            return "/"
        return "/"

    def cd(self, path):
        if path != self.path:
            self._clear_navigation_state()
        self.path = path
        self.tabs[self.tab_index] = path
        self._rebuild_tabs()
        self.refresh()

    def go_up(self):
        up = self.up_path()
        if up is not None:
            self.cd(up)

    def go_root(self):
        self.cd(self.root_path())

    def path_entered(self):
        text = self.path_edit.text().strip()
        if text.endswith("*.*"):
            text = text[:-3].rstrip("\\/")
        if not text:
            return
        self.win.try_cd(self, text)

    # ------------------------------------------------------ context menu
    def show_context_menu(self, pos):
        """Show file operations for the row under the right mouse button.

        A right click also makes this panel active and moves the cursor to the
        clicked row, matching the Total Commander interaction model. Existing
        Space-marked rows are preserved, so Copy/Move/Delete can still work on
        a batch selected by the user.
        """
        row = self.table.rowAt(pos.y())
        self.win.set_active(self.side)
        self.table.setFocus()
        if row >= 0:
            self.table.setCurrentCell(row, 0)

        entry = self.selected_entry()
        menu = QMenu(self.table)

        def add(text, slot, enabled=True):
            action = menu.addAction(text, slot)
            action.setEnabled(bool(enabled))
            return action

        is_real_entry = bool(entry and not entry.get("up"))
        is_file = bool(is_real_entry and not entry.get("is_dir"))
        can_write_image = self.mode != "img" or self.writer is not None

        is_text = bool(is_file and text_editor.is_text_file(entry["name"]))
        if entry and entry.get("is_dir"):
            open_label = "Открыть"
        elif is_text:
            open_label = "Открыть в Блокноте"
        else:
            open_label = "Просмотр (F3)"
        add(open_label, lambda: self.open_row(self.table.currentRow()), bool(entry))
        add("HEX-редактор", self.win.f3_hex_view, is_file)
        add("AI-анализ", self.win.ai_analyze, is_file)
        menu.addSeparator()
        add(
            "Копировать в другую панель (F5)",
            self.win.f5_copy,
            self.win.facts["F5"].isEnabled(),
        )
        add(
            "Переместить в другую панель (F6)",
            self.win.f6_move,
            self.win.facts["F6"].isEnabled(),
        )
        add(
            "Переименовать", self.win.rename_selected, is_real_entry and can_write_image
        )
        add("Удалить (F8)", self.win.f8_delete, is_real_entry and can_write_image)
        add("Копировать путь", self.copy_selected_path, is_real_entry)
        menu.addSeparator()
        add("Новая папка (F7)", self.win.f7_mkdir, can_write_image)
        add("Обновить", self.refresh)
        menu.addSeparator()
        add("Выделить всё (Ctrl+A)", self.mark_all)
        add("Снять выделение", self.unmark_all, bool(self.marked))

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def copy_selected_path(self):
        """Copy the selected local path or image-internal path to clipboard."""
        entry = self.selected_entry()
        if not entry or entry.get("up"):
            return
        path = self.join(entry["name"])
        if self.mode == "img" and self.win.image_path:
            path = "%s::%s" % (self.win.image_path, path)
        QApplication.clipboard().setText(path)
        self.win.log_msg("Путь скопирован: %s" % path)

    # ---------------------------------------------------------------- tabs
    def _rebuild_tabs(self):
        self.tabbar.blockSignals(True)
        while self.tabbar.count():
            self.tabbar.removeTab(0)
        for p in self.tabs:
            self.tabbar.addTab(self.tab_text(p))
            self.tabbar.setTabToolTip(self.tabbar.count() - 1, p)
        self.tab_index = min(self.tab_index, len(self.tabs) - 1)
        self.tabbar.setCurrentIndex(self.tab_index)
        self.tabbar.blockSignals(False)

    @staticmethod
    def tab_text(path):
        if path in ("/", "") or (len(path) == 3 and path.endswith(":\\")):
            return path
        return os.path.basename(path.rstrip("\\/")) or path

    def tab_changed(self, idx):
        if 0 <= idx < len(self.tabs):
            self._clear_navigation_state()
            self.tab_index = idx
            self.path = self.tabs[idx]
            self.refresh()

    def tab_closed(self, idx):
        if len(self.tabs) <= 1 or not (0 <= idx < len(self.tabs)):
            return
        self._clear_navigation_state()
        self.tabs.pop(idx)
        self.tab_index = max(0, min(self.tab_index, len(self.tabs) - 1))
        self.path = self.tabs[self.tab_index]
        self._rebuild_tabs()
        self.refresh()

    def new_tab(self):
        self.tabs.insert(self.tab_index + 1, self.path)
        self.tab_index += 1
        self._rebuild_tabs()
        self.win.log_msg("Новая вкладка: %s" % self.path)

    def close_tab(self):
        self.tab_closed(self.tab_index)

    # -------------------------------------------------------------- drives
    def drive_items(self):
        """(text, data) for the drive combo: local drives + image volumes."""
        items = []
        for label, path in local_drives():
            items.append(("%s  %s" % (label, path), ("local", path)))
        if self.win.image_path:
            if self.win.volumes:
                for v in self.win.volumes:
                    items.append(("Образ: %s" % v.desc, ("img", v.offset)))
            else:
                items.append(("Образ: целый образ (offset 0)", ("img", 0)))
        if not items:
            items.append((self.path, ("local", self.path)))
        return items

    def populate_drives(self):
        """Refill the combo and select the entry matching the current state."""
        items = self.drive_items()
        current = 0
        found = False
        for i, (_text, data) in enumerate(items):
            if (
                self.mode == "img"
                and data[0] == "img"
                and data[1] == self.volume_offset
                or (
                    self.mode == "local"
                    and data[0] == "local"
                    and os.path.normpath(data[1]) == os.path.normpath(self.path)
                )
            ):
                current, found = i, True
        if self.mode == "local" and not found:
            items.insert(0, ("Текущая: %s" % self.path, ("local", self.path)))
            current = 0
        self.cmb_drive.blockSignals(True)
        self.cmb_drive.clear()
        for text, data in items:
            self.cmb_drive.addItem(text, data)
        self.cmb_drive.setCurrentIndex(current)
        self.cmb_drive.blockSignals(False)

    def drive_changed(self, idx):
        data = self.cmb_drive.itemData(idx)
        if not data:
            return
        kind, value = data
        self._clear_navigation_state()
        if kind == "local":
            self.win.release_volume(self)
            self.mode = "local"
            self.tabs = [value]
            self.tab_index = 0
            self.path = value
            self._rebuild_tabs()
            self.refresh()
        else:
            if not self.win.mount_volume(self, value):
                self.populate_drives()

    # ----------------------------------------------------------------- data
    def refresh(self):
        self.entries = []
        hint = None
        if self.mode == "img":
            if self.provider is None:
                hint = (
                    "образ не открыт — «Файлы → Открыть образ…» "
                    "(Ctrl+O) выберите raw-дамп диска"
                )
            else:
                try:
                    self.entries = self.provider.list_dir(self.path)
                    self._enrich_attrs()
                except Exception as e:
                    hint = "ошибка чтения каталога: %s" % e
        else:
            try:
                with os.scandir(self.path) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            st = entry.stat()
                        except OSError:
                            continue
                        self.entries.append(
                            {
                                "name": entry.name,
                                "is_dir": is_dir,
                                "size": 0 if is_dir else int(st.st_size),
                                "date": tc_style.fmt_date(st.st_mtime),
                                "writable": bool(st.st_mode & 0o200),
                            }
                        )
            except OSError as e:
                hint = "ошибка чтения каталога: %s" % e
        self.marked.intersection_update(e["name"] for e in self.entries)
        self._listing_hint = hint
        self._sort_entries()
        self._fill(hint)
        self.update_header()

    def _enrich_attrs(self):
        """Attach the real FAT attribute byte when the volume is writable."""
        if self.writer is None or not self.entries:
            return
        try:
            d = self.writer.find_dir(self.path)
            if d is None:
                return
            attrs = {}
            for e in self.writer.list_dir(d.cluster):
                attrs[e.name.upper()] = e.attr
            for e in self.entries:
                found = attrs.get(e["name"].upper())
                if found is not None:
                    e["fat_attr"] = found
        except Exception:
            pass

    def _sort_entries(self):
        key = {
            0: lambda e: e["name"].lower(),
            1: lambda e: tc_style.file_type(e["name"], e["is_dir"]).lower(),
            2: lambda e: e.get("size") or 0,
            3: lambda e: e.get("date") or "",
            4: lambda e: tc_style.attrs_str(
                e["name"],
                is_dir=e["is_dir"],
                writable=e.get("writable", True),
                fat_attr=e.get("fat_attr"),
            ),
        }.get(self.sort_col, lambda e: e["name"].lower())
        dirs = [e for e in self.entries if e["is_dir"]]
        files = [e for e in self.entries if not e["is_dir"]]
        dirs.sort(key=key, reverse=self.sort_rev)
        files.sort(key=key, reverse=self.sort_rev)
        self.entries = dirs + files

    def on_header_clicked(self, col):
        self.win.set_active(self.side)
        if col == self.sort_col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col = col
            self.sort_rev = False
        self._sort_entries()
        self._fill(self._listing_hint)
        self.update_header()

    def _row_values(self, e):
        """The five cells of one row (name, type, size, date, attributes)."""
        name = e["name"]
        if e.get("up"):
            return (
                name,
                tc_style.DIR_TYPE_TEXT,
                DIR_SIZE_TEXT,
                "",
                tc_style.attrs_str("", is_dir=True),
            )
        attrs = tc_style.attrs_str(
            name,
            is_dir=e["is_dir"],
            writable=e.get("writable", True),
            fat_attr=e.get("fat_attr"),
        )
        kind = tc_style.file_type(name, e["is_dir"])
        if e["is_dir"]:
            return name, kind, DIR_SIZE_TEXT, e.get("date") or "", attrs
        return (
            name,
            kind,
            tc_style.fmt_size(e.get("size") or 0),
            e.get("date") or "",
            attrs,
        )

    def _fill(self, hint=None):
        self.table.clearSpans()
        cursor_name = self.cursor_name()
        visible = self.visible_entries()
        empty = hint is not None or not visible
        self.empty_state.setVisible(empty)
        self.table.setVisible(not empty)
        unopened = (
            self.mode == "img" and self.provider is None and not self.win.image_path
        )
        self.empty_open.setVisible(unopened)
        self.empty_retry.setVisible(hint is not None and not unopened)
        if empty:
            if unopened:
                title = "Откройте образ диска"
                detail = "Raw-образы и дампы NAND/eMMC.\nCtrl+O — выбрать файл для просмотра."
            elif hint:
                title, detail = "Каталог недоступен", hint
            elif self.search_edit.text().strip():
                title, detail = (
                    "Совпадений нет",
                    "Измените фильтр или очистите его крестиком справа.",
                )
            else:
                title, detail = (
                    "Каталог пуст",
                    "Здесь пока нет файлов. Backspace — на уровень вверх.",
                )
            self.empty_title.setText(title)
            self.empty_hint.setText(detail)
        if hint is not None:
            self.table.setRowCount(0)
            self.selection_changed()
            return

        rows = []
        up = self.up_path()
        if up is not None:
            rows.append(
                {"name": "..", "is_dir": True, "size": None, "date": "", "up": True}
            )
        rows.extend(visible)

        marked_bg = QBrush(tc_style.qcolor("marked"))
        marked_fg = QBrush(tc_style.qcolor("marked_text"))
        dir_fg = QBrush(tc_style.qcolor("text_dir"))
        text_fg = QBrush(tc_style.qcolor("text"))

        self.table.setRowCount(len(rows))
        for r, e in enumerate(rows):
            marked = e["name"] in self.marked and not e.get("up")
            for c, text in enumerate(self._row_values(e)):
                it = QTableWidgetItem(text)
                it.setFlags(ItemIsEnabled | ItemIsSelectable)
                if c == 0:
                    # Keep the complete name available even when Qt elides
                    # it to fit a narrow panel or a high-DPI display.
                    it.setToolTip(text)
                    it.setIcon(
                        standard_icon(
                            self, "SP_DirIcon" if e["is_dir"] else "SP_FileIcon"
                        )
                    )
                else:
                    it.setToolTip(text)
                if c == 2 and not e.get("up"):
                    it.setTextAlignment(AlignRight | AlignVCenter)
                if marked:
                    it.setBackground(marked_bg)
                    it.setForeground(marked_fg)
                elif e["is_dir"]:
                    it.setForeground(dir_fg)
                else:
                    it.setForeground(text_fg)
                self.table.setItem(r, c, it)

        row = self._row_of(cursor_name)
        if row < 0 and rows:
            row = 1 if rows[0].get("up") and len(rows) > 1 else 0
        self.table.setCurrentCell(row, 0)
        self.selection_changed()

    def cursor_name(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item is not None else None

    def _row_of(self, name):
        if name is None:
            return -1
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.text() == name:
                return r
        return -1

    def update_header(self):
        """Path line, tab label, free space readout and status line."""
        self._update_sort_indicator()
        suffix = "*.*"
        if self.mode == "local":
            sep = os.sep
            base = self.path if self.path.endswith(sep) else self.path + sep
            self.path_edit.setText(base + suffix)
        else:
            base = self.path if self.path.endswith("/") else self.path + "/"
            self.path_edit.setText(base + suffix)
        self.tabs[self.tab_index] = self.path
        self.tabbar.setTabText(self.tab_index, self.tab_text(self.path))

        active = self.win.active_side == self.side
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.lbl_title.setText(
            "Образ диска" if self.mode == "img" else "Локальные файлы"
        )
        if self.mode == "img":
            badge = (
                "Не открыт"
                if self.provider is None
                else "Запись доступна" if self.writer is not None else "Только чтение"
            )
            reason = (
                self.write_reason
                or "Изменения образа требуют подтверждения и резервной копии."
            )
        else:
            badge, reason = (
                "Локальный диск",
                "Операции выполняются с файлами на этом компьютере.",
            )
        self.lbl_mode.setText(badge)
        self.lbl_mode.setToolTip(reason)
        self.path_edit.setStyleSheet(
            "border:1px solid %s;" % tc_style.color("accent" if active else "border")
        )
        space = self._space_text()
        self.lbl_space.setText(space)
        self.lbl_space.setToolTip(space)
        self.lbl_status.setText(self._status_text())
        available = self.mode == "local" or self.provider is not None
        self.btn_root.setEnabled(available)
        self.btn_up.setEnabled(available and self.up_path() is not None)
        self.search_edit.setEnabled(available)
        self.path_edit.setEnabled(available)
        self.populate_drives()
        self.selection_changed()

    def _update_sort_indicator(self):
        """Arrow in the header of the sorted column (Total Commander style)."""
        hdr = self.table.horizontalHeader()
        hdr.setSortIndicator(
            self.sort_col, DescendingOrder if self.sort_rev else AscendingOrder
        )

    def _space_text(self):
        try:
            if self.mode == "img":
                if not self.win.image_path:
                    return "образ не открыт"
                total = os.path.getsize(self.win.image_path)
                free = self.writer.free_space() if self.writer is not None else None
                if self.writer is not None and self.writer.total_bytes:
                    total = max(total, self.writer.total_bytes)
            else:
                usage = shutil.disk_usage(self.path)
                total, free = usage.total, usage.free
        except Exception:
            return "свободно: н/д"
        if free is None:
            return "%s (свободно: н/д)" % tc_style.fmt_kb(total)
        return "%s из %s свободно" % (tc_style.fmt_kb(free), tc_style.fmt_kb(total))

    def _status_text(self):
        files = [e for e in self.entries if not e["is_dir"]]
        dirs = [e for e in self.entries if e["is_dir"]]
        if not files and not dirs:
            # An unopened or empty panel gets the plain key legend instead of
            # a meaningless '0 Кб из 0 Кб'; the image panel without an image
            # already carries the «образ не открыт» hint in its list.
            if self.mode == "img" and self.provider is None:
                return ""
            return PANEL_LEGEND
        mfiles = [e for e in files if e["name"] in self.marked]
        mdirs = [e for e in dirs if e["name"] in self.marked]
        parts = []
        if files:
            parts.append(
                "%s из %s"
                % (
                    tc_style.fmt_kb(sum(e.get("size") or 0 for e in mfiles)),
                    tc_style.fmt_kb(sum(e.get("size") or 0 for e in files)),
                )
            )
            parts.append("файлов: %d из %d" % (len(mfiles), len(files)))
        if dirs:
            parts.append("папок: %d из %d" % (len(mdirs), len(dirs)))
        return ", ".join(parts)

    # ------------------------------------------------------- selection API
    def entry_at(self, row):
        if row < 0 or row >= self.table.rowCount():
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        name = item.text()
        if name == "..":
            return {"name": "..", "is_dir": True, "up": True}
        return next((e for e in self.entries if e["name"] == name), None)

    def selected_entry(self):
        return self.entry_at(self.table.currentRow())

    def marked_entries(self):
        """Marked entries if anything is marked, otherwise the cursor row."""
        marked = [e for e in self.entries if e["name"] in self.marked]
        if marked:
            return marked
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        selected = [self.entry_at(row) for row in rows]
        selected = [entry for entry in selected if entry and not entry.get("up")]
        if selected:
            return selected
        e = self.selected_entry()
        return [e] if e and not e.get("up") else []

    def toggle_mark(self, row):
        e = self.entry_at(row)
        if not e or e.get("up"):
            return
        if e["name"] in self.marked:
            self.marked.discard(e["name"])
        else:
            self.marked.add(e["name"])
        self._fill()
        self.update_header()

    def mark_all(self):
        self.marked = {e["name"] for e in self.visible_entries()}
        self._fill()
        self.update_header()

    def unmark_all(self):
        self.marked.clear()
        self._fill()
        self.update_header()

    def invert_marks(self):
        self.marked = {
            e["name"] for e in self.visible_entries() if e["name"] not in self.marked
        }
        self._fill()
        self.update_header()

    def mark_mask(self, mask, on=True):
        for e in self.visible_entries():
            hit = fnmatch.fnmatch(e["name"].lower(), mask.lower())
            if not hit:
                continue
            if on:
                self.marked.add(e["name"])
            else:
                self.marked.discard(e["name"])
        self._fill()
        self.update_header()

    def move_cursor(self, delta):
        row = self.table.currentRow() + delta
        row = max(0, min(row, self.table.rowCount() - 1))
        self.table.setCurrentCell(row, -1)

    def open_row(self, row):
        e = self.entry_at(row)
        if not e:
            return
        if e.get("up"):
            self.go_up()
            return
        if e["is_dir"]:
            self.cd(self.join(e["name"]))
        elif text_editor.is_text_file(e["name"]):
            self.win.open_text_file(self)
        else:
            self.win.f3_hex_view()


class AiDialog(QDialog):
    """Кнопка «AI»: добавить файл для анализа и показать ответ модели.

    Источником может быть файл, выбранный в активной панели (в том числе
    внутри образа), либо произвольный файл на диске. Оба читаются через
    :func:`mountfile.ai.collect`, поэтому усечение видно прямо в интерфейсе,
    а результат одинаков для образа и локального диска. Ключ API в журнал
    не попадает - в журнал идёт только имя файла, бэкенд и итог.
    """

    def __init__(self, parent, panel, log=None):
        super().__init__(parent)
        self.panel = panel
        self.log = log or (lambda msg: None)
        self.setWindowTitle("AI — анализ файла")
        self.resize(780, 560)
        self._entry = None
        self._mode = None
        self._path = None
        self._size = 0

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_source = QLabel("Файл не выбран")
        top.addWidget(self.lbl_source, 1)
        btn_disk = QPushButton("Файл с диска…")
        btn_disk.clicked.connect(self.pick_file)
        top.addWidget(btn_disk)
        btn_panel = QPushButton("Из активной панели")
        btn_panel.clicked.connect(self.use_panel)
        top.addWidget(btn_panel)
        lay.addLayout(top)

        self.lbl_info = QLabel("")
        lay.addWidget(self.lbl_info)

        self.ed_prompt = QLineEdit()
        self.ed_prompt.setPlaceholderText("Что спросить у модели (необязательно)")
        lay.addWidget(self.ed_prompt)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        lay.addWidget(self.txt, 1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()
        lay.addWidget(self.bar)

        buttons = QDialogButtonBox()
        self.btn_run = buttons.addButton("Анализировать", DlgActionRole)
        self.btn_run.clicked.connect(self.run_analysis)
        buttons.addButton(DlgClose)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        # Активная панель - источник по умолчанию: чаще всего анализируют
        # именно файл, на котором стоит курсор.
        self.use_panel()

    # ---------------------------------------------------------- источник
    def _clear_source(self, label="Файл не выбран"):
        """Сбросить все поля источника как единое состояние."""
        self._entry, self._path, self._mode, self._size = None, None, None, 0
        self.lbl_source.setText(label)
        self.lbl_info.setText("")

    def use_panel(self):
        """Взять файл, выбранный в активной панели (может быть в образе)."""
        entry = self.panel.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            self._clear_source("В активной панели файл не выбран")
            return
        self._entry = entry
        self._mode = self.panel.mode
        self._path = None
        self._size = int(entry.get("size") or 0)
        self.lbl_source.setText(
            "Из панели: %s (%s)" % (entry["name"], tc_style.fmt_size(self._size))
        )
        self.lbl_info.setText("")

    def pick_file(self):
        """Взять произвольный файл с диска."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Файл для анализа", "", AI_FILE_FILTER
        )
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        self._entry = None
        self._mode = None
        self._path = path
        self._size = size
        self.lbl_source.setText(
            "С диска: %s (%s)" % (os.path.basename(path), tc_style.fmt_size(size))
        )
        self.lbl_info.setText("")

    def _name(self):
        if self._entry is not None:
            return self._entry["name"]
        return os.path.basename(self._path or "")

    def _reader(self):
        """``reader(offset, length)`` - одинаковый для образа и для диска."""
        if self._entry is not None:
            full = self.panel.join(self._entry["name"])
            if self._mode == "img":
                provider = self.panel.provider
                return lambda off, ln: provider.read_file_range(full, off, ln)
            return ai.local_reader(full)
        return ai.local_reader(self._path)

    # ------------------------------------------------------------ анализ
    def run_analysis(self):
        """Прочитать файл чанками и отдать его выбранному бэкенду.

        Сеть живёт в рабочем потоке, а диалог крутит локальный цикл
        событий: окно остаётся отзывчивым, но метод возвращается только
        после готовности результата (синхронная семантика для тестов).
        """
        if self._entry is None and not self._path:
            QMessageBox.information(self, "AI", "Сначала выберите файл для анализа.")
            return
        settings = tc_style.SETTINGS
        backend = ai.build_backend(settings)
        limit = ai.max_bytes(settings)
        name = self._name()
        prompt = self.ed_prompt.text().strip() or None
        box = []

        def worker():
            try:
                data, truncated = ai.collect(self._reader(), max_bytes=limit)
                result = backend.analyze(
                    name,
                    data,
                    prompt=prompt,
                    truncated=truncated,
                    size=self._size,
                )
            except Exception as e:
                result = ai.AiResult(ok=False, backend=backend.name, error=str(e))
            box.append(result)

        self.bar.show()
        self.btn_run.setEnabled(False)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # Локальный цикл событий вместо блокировки GUI-потока: запрос к
        # бэкенду может занять до минуты таймаута, окно не должно «замерзать».
        loop = QEventLoop()
        timer = QTimer(self)
        timer.setInterval(50)
        timer.timeout.connect(lambda: None if t.is_alive() else loop.quit())
        timer.start()
        try:
            loop.exec()
        finally:
            timer.stop()
            t.join(timeout=1)
            self.bar.hide()
            self.btn_run.setEnabled(True)
        result = (
            box[0]
            if box
            else ai.AiResult(
                ok=False, backend=backend.name, error="Анализ не завершился."
            )
        )
        self._show(name, result)

    def _show(self, name, result):
        """Результат в поле диалога + одна строка в журнал окна."""
        head = "Файл: %s\nРазмер: %s байт · прочитано: %d байт%s\nБэкенд: %s" % (
            name,
            tc_style.fmt_size(self._size),
            result.bytes_read or 0,
            " · данные усечены" if result.truncated else "",
            result.backend,
        )
        if result.model:
            head += " (%s)" % result.model
        if result.ok:
            self.txt.setPlainText(head + "\n\n" + result.text)
            self.log("AI-анализ: %s — готово (%s)" % (name, result.backend))
        else:
            self.txt.setPlainText(head + "\n\nОшибка: " + result.error)
            self.log(
                "AI-анализ: %s — ошибка: %s"
                % (name, result.error.splitlines()[0] if result.error else "?")
            )


class SettingsDialog(QDialog):
    """Конфигурация → Настройки: theme, font size, log visibility."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        lay = QFormLayout(self)
        self.cmb_theme = QComboBox()
        for label, key in sorted(tc_style.THEME_BY_LABEL.items()):
            self.cmb_theme.addItem(label, key)
        idx = self.cmb_theme.findData(tc_style.theme())
        self.cmb_theme.setCurrentIndex(max(0, idx))
        lay.addRow("Тема интерфейса:", self.cmb_theme)
        self.spn_font = QSpinBox()
        self.spn_font.setRange(7, 16)
        self.spn_font.setValue(int(tc_style.SETTINGS["font_size"]))
        lay.addRow("Размер шрифта:", self.spn_font)
        self.chk_log = QCheckBox("Показывать журнал внизу окна")
        self.chk_log.setChecked(bool(tc_style.SETTINGS["show_log"]))
        lay.addRow("", self.chk_log)
        # --- AI: бэкенд анализа для кнопки «AI» (ключ не пишется в журнал)
        self.cmb_ai = QComboBox()
        for key, (label, _factory) in sorted(ai.BACKENDS.items()):
            self.cmb_ai.addItem(label, key)
        idx = self.cmb_ai.findData(tc_style.SETTINGS.get("ai_backend") or "none")
        self.cmb_ai.setCurrentIndex(max(0, idx))
        lay.addRow("AI-бэкенд:", self.cmb_ai)
        self.ed_ai_url = QLineEdit(str(tc_style.SETTINGS.get("ai_base_url") or ""))
        lay.addRow("Адрес сервера:", self.ed_ai_url)
        # Отдельный адрес OpenAI-бэкенда: локальный адрес Ollama по умолчанию
        # не должен «протекать» в облачный эндпоинт.
        self.ed_ai_url_openai = QLineEdit(
            str(tc_style.SETTINGS.get("ai_base_url_openai") or "")
        )
        self.ed_ai_url_openai.setPlaceholderText("пусто = https://api.openai.com")
        lay.addRow("Адрес OpenAI-сервера:", self.ed_ai_url_openai)
        self.ed_ai_model = QLineEdit(str(tc_style.SETTINGS.get("ai_model") or ""))
        lay.addRow("Модель:", self.ed_ai_model)
        self.btn_ai_detect = QPushButton("Проверить и настроить Ollama")
        self.btn_ai_detect.setToolTip(
            "Найдёт локальный сервер и выберет одну из уже загруженных моделей"
        )
        self.btn_ai_detect.clicked.connect(self._detect_ollama)
        lay.addRow("Локальный AI:", self.btn_ai_detect)
        self.ed_ai_key = QLineEdit(str(tc_style.SETTINGS.get("ai_api_key") or ""))
        self.ed_ai_key.setEchoMode(EchoPassword)
        lay.addRow("Ключ API:", self.ed_ai_key)
        self.spn_ai_bytes = QSpinBox()
        self.spn_ai_bytes.setRange(4, 65536)
        self.spn_ai_bytes.setSuffix(" Кб")
        self.spn_ai_bytes.setValue(
            int(tc_style.SETTINGS.get("ai_max_bytes") or 262144) // 1024
        )
        lay.addRow("Читать до:", self.spn_ai_bytes)
        # --- Privacy-safe Format Advisor: локальные рекомендации по метаданным
        self.chk_advise = QCheckBox(
            "Format Advisor: показывать локальные советы по метаданным при открытии"
        )
        self.chk_advise.setChecked(bool(tc_style.SETTINGS.get("ai_format_autohook")))
        lay.addRow("", self.chk_advise)
        self.chk_consensus = QCheckBox(
            "AI-гипотезы: включать консенсус второго доступного бэкенда"
        )
        self.chk_consensus.setChecked(bool(tc_style.SETTINGS.get("ai_consensus")))
        lay.addRow("", self.chk_consensus)
        self.ed_advise_repo = QLineEdit(
            str(tc_style.SETTINGS.get("ai_github_repo") or "")
        )
        self.ed_advise_repo.setPlaceholderText("owner/name")
        lay.addRow("Репозиторий отчётов:", self.ed_advise_repo)
        self.ed_advise_token = QLineEdit(
            str(tc_style.SETTINGS.get("ai_github_token") or "")
        )
        self.ed_advise_token.setEchoMode(EchoPassword)
        lay.addRow("Токен GitHub (отчёты):", self.ed_advise_token)
        # --- OLMA Watch: слежение за ошибками + отчёт на GitHub (opt-in)
        self.chk_olma = QCheckBox(
            "Следить за ошибками и отправлять отчёт на GitHub (OLMA Watch)"
        )
        self.chk_olma.setChecked(bool(tc_style.SETTINGS.get("olma_watch")))
        lay.addRow("", self.chk_olma)
        self.ed_olma_token = QLineEdit(str(tc_style.SETTINGS.get("olma_token") or ""))
        self.ed_olma_token.setEchoMode(EchoPassword)
        self.ed_olma_token.setPlaceholderText(
            "пусто = брать из переменной MOUNTFILE_OLMA_TOKEN"
        )
        lay.addRow("GitHub-токен:", self.ed_olma_token)
        buttons = QDialogButtonBox(DlgOk | DlgCancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addRow(buttons)

    def _detect_ollama(self):
        """Probe Ollama only after an explicit click in Settings."""
        settings = {"ai_model": self.ed_ai_model.text().strip()}
        result = ai.configure_local_ollama(settings, self.ed_ai_url.text().strip())
        if result["ok"]:
            idx = self.cmb_ai.findData("ollama")
            self.cmb_ai.setCurrentIndex(max(0, idx))
            self.ed_ai_url.setText(settings["ai_base_url"])
            self.ed_ai_model.setText(settings["ai_model"])
            QMessageBox.information(
                self,
                "AI настроен",
                "Ollama доступна. Выбрана модель: %s\n\nДоступно моделей: %s"
                % (settings["ai_model"], ", ".join(result["models"])),
            )
            return
        if result["reachable"]:
            detail = (
                "Ollama запущена, но модель не загружена.\n\n"
                "Откройте PowerShell и выполните:\n"
                "ollama pull llama3.2:3b\n\n"
                "Затем нажмите кнопку ещё раз."
            )
        else:
            detail = (
                "Ollama не найдена по адресу http://127.0.0.1:11434.\n\n"
                "Установите Ollama с https://ollama.com/download/windows,\n"
                "запустите её и загрузите модель:\n"
                "ollama pull llama3.2:3b"
            )
        QMessageBox.warning(self, "AI не настроен", detail + "\n\n" + result["error"])


class MainWindow(QMainWindow):
    #: Сколько ждать завершения этапа обновления, прежде чем сбросить
    #: индикатор в строке состояния (мс). 0 — сторож выключен.
    UPDATE_WATCHDOG_MS = 15 * 60 * 1000

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mount File — режим Total Commander")
        self.setMinimumSize(640, 400)
        self._apply_app_icon()

        self.image_path = None
        self.volumes = []
        self._loaded = False

        self.active_side = "L"
        self.updater = None
        self._closing = False
        #: Каталог временных копий текстовых файлов из образов (см.
        #: :meth:`open_text_file`); создаётся при первом обращении.
        self._text_tmp = None

        self._build_ui()
        self._fit_to_screen()
        act_fs = QAction("Полный экран (F11)", self)
        act_fs.setShortcut(QKeySequence("F11"))
        act_fs.triggered.connect(self.toggle_fullscreen)
        self.addAction(act_fs)
        self._apply_theme(tc_style.theme())
        self._set_loaded(False)
        self._update_cmd_prompt()
        self._start_updater(startup=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.height() < 680
        header = self.findChild(QWidget, "workspaceHeader")
        if header is not None:
            header.setVisible(not compact)
        for panel in getattr(self, "panels", {}).values():
            panel.layout().setSpacing(4 if compact else 8)
            panel.lbl_space.setVisible(not compact)
            panel.empty_hint.setVisible(not compact)
            panel.table.apply_font()

    # ----------------------------------------------------------------- icon
    def _apply_app_icon(self):
        """Window icon from the packaged artwork (cosmetic, never raises)."""
        try:
            from . import appicon

            icon = appicon.app_icon()
            if icon is not None and not icon.isNull():
                self.setWindowIcon(icon)
        except Exception:  # pragma: no cover - icons are cosmetic
            pass

    # -------------------------------------------------------- screen fit
    def screen_geometry(self):
        """Work area of the monitor the window lives on (None if unknown)."""
        screen = None
        try:
            screen = self.screen()
        except Exception:
            screen = None
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _fit_to_screen(self):
        """Fill the monitor work area instead of the fixed 1180x720 size.

        Keeps the window centered on the monitor it is on and follows the
        real resolution (DPI-scaled), so the panel columns and the log dock
        always have all the space the monitor can give.
        """
        geo = self.screen_geometry()
        if geo is None:
            self.resize(1180, 720)
            return
        self.resize(geo.width(), geo.height())
        self.move(geo.x(), geo.y())

    def toggle_fullscreen(self):
        """F11: true fullscreen <-> back to the maximized window."""
        if self.isFullScreen():
            self.showNormal()
            self.showMaximized()
            self.log_msg("Полный экран выключен (F11)")
        else:
            self.showFullScreen()
            self.log_msg("Полный экран включен (F11 — выход)")

    # ----------------------------------------------------------- updater
    def _start_updater(self, startup=False):
        """Run an explicit/manual update check without making startup online."""
        if startup and updates_disabled():
            self.log_msg(
                "Автопроверка обновлений выключена; для opt-in задайте "
                "MOUNTFILE_ENABLE_UPDATE_CHECK=1"
            )
            return
        if self.updater is not None and self.updater.isRunning():
            return
        self.log_msg("Проверка обновлений (установлено v%s)…" % app_version())
        # parent=None: дочерний QThread удаляется вместе с окном, а удаление
        # работающего QThread — это аварийное завершение всего процесса.
        self.updater = StartupUpdater(None)
        self.updater.status.connect(self.log_msg)
        self.updater.available.connect(self._on_update_available)
        self.updater.installing.connect(
            lambda info: self.log_msg(
                "Устанавливаю v%s и перезапускаюсь…" % info.get("version")
            )
        )
        self.updater.progress.connect(self._update_progress)
        self.updater.stage.connect(self._update_stage)
        self.updater.failed.connect(self._update_hide)
        self.updater.restarting.connect(self._on_update_restarting)
        self.updater.installed.connect(self._on_update_installed)
        self.updater.start()

    def _on_update_available(self, info):
        if getattr(self, "_closing", False):
            return
        version = info.get("version") or "?"
        asset = info.get("asset")
        if not asset:
            if info.get("url"):
                QMessageBox.information(
                    self,
                    "Доступна новая версия",
                    "Версия v%s уже вышла, но сборки для вашей системы "
                    "в релизе нет.\n\nСтраница релиза:\n%s" % (version, info["url"]),
                )
            return
        if not is_frozen():
            self.log_msg(
                "Новая версия v%s. Запуск из исходников — файлы не меняются: %s"
                % (version, info.get("url") or "")
            )
            return
        if auto_install():
            return
        if self._show_update_dialog(info):
            return
        ret = QMessageBox.question(
            self,
            "Обновление Mount File",
            "Доступна версия v%s (у вас v%s).\n\n"
            "Скачать %s и установить сейчас? Программа перезапустится сама.\n"
            "Подробности — в журнале внизу окна."
            % (version, app_version(), asset.get("name")),
            MB_Yes | MB_No,
        )
        if ret == MB_Yes:
            self.updater.install(info, self)

    def _show_update_dialog(self, info):
        """Показать «Центр обновлений» вместо простого вопроса QMessageBox.

        Возвращает False, если модуль окна недоступен (нет Qt / файл не
        доставлен) — тогда вызывающий код показывает прежний вопрос.
        """
        try:
            from .update_dialog import UpdateDialog
        except Exception as exc:  # pragma: no cover - только без Qt/файла
            self.log_msg(
                "Окно обновления недоступно (%s) — простое подтверждение." % exc
            )
            return False
        dialog = UpdateDialog.from_release(info, app_version(), self)
        self._update_hide()
        dialog.exec()
        return True

    def _on_update_restarting(self):
        self.statusBar().showMessage("Установка обновления — перезапуск…")

    # --------------------------------------- индикатор хода обновления
    def _update_stage(self, text):
        """Этап обновления: текст в статус-баре, полоса — режим «занят»."""
        self._update_stage_label.setText(text)
        self._update_stage_label.show()
        self._update_bar.setRange(0, 0)
        self._update_bar.show()
        if self.UPDATE_WATCHDOG_MS:
            self._update_watchdog.start(self.UPDATE_WATCHDOG_MS)

    def _update_progress(self, value):
        """Ход скачивания: 0–100 %; ``-1`` — размер неизвестен («занят»)."""
        if value < 0:
            self._update_bar.setRange(0, 0)
        else:
            self._update_bar.setRange(0, 100)
            self._update_bar.setValue(value)
        self._update_bar.show()
        self._update_stage_label.show()

    def _update_hide(self):
        """Скрыть индикатор (нечего показывать / установка провалилась)."""
        watchdog = getattr(self, "_update_watchdog", None)
        if watchdog is not None:
            watchdog.stop()
        self._update_stage_label.hide()
        self._update_bar.hide()

    def _update_timeout(self):
        """Сторож сработал: сбросить «висящий» индикатор и сказать в журнал.

        Без него полоса «занят» оставалась на экране навсегда, если поток
        установки умирал молча, — именно так выглядела заморозка при
        обновлении.
        """
        self._update_hide()
        self.log_msg(
            "Обновление не завершилось за %d мин — индикатор сброшен. "
            "Журнал установки: %s"
            % (
                self.UPDATE_WATCHDOG_MS // 60000,
                os.path.join(tempfile.gettempdir(), "mountfile-update.log"),
            )
        )

    def _on_update_installed(self):
        """Обновление поставлено: сразу закрываемся, ничего не ожидая.

        Скрипт подмены уже запущен и сам ждёт наш PID, поэтому окно должно
        закрыться немедленно: пока процесс жив, файл занят и ``move`` не
        проходит. Раньше здесь стояло ``wait_installer(3000)`` — поток GUI
        замирал на три секунды в самый ненужный момент.
        """
        self._close_writer()
        QApplication.quit()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self._build_toolbar()
        self._build_panels()
        self._build_command_area()
        self._build_menus()
        self._build_log()
        self._build_update_status()
        self.statusBar().showMessage("Откройте образ диска (Ctrl+O), чтобы начать")

    def _build_update_status(self):
        """Постоянные виджеты хода обновления в строке состояния.

        Скрыты, пока обновление не началось; этап показывается текстом,
        рядом — полоса прогресса (проценты скачивания, режим «занят»
        на этапах проверки суммы и установки). Постоянные виджеты
        (addPermanentWidget) не затираются временными showMessage().
        """
        sb = self.statusBar()
        self._update_stage_label = QLabel(self)
        self._update_bar = QProgressBar(self)
        self._update_bar.setFixedWidth(140)
        sb.addPermanentWidget(self._update_stage_label)
        sb.addPermanentWidget(self._update_bar)
        # Сторож против «висящего» индикатора: если этап обновления не
        # закончился за UPDATE_WATCHDOG_MS, индикатор сбрасывается сам.
        self._update_watchdog = QTimer(self)
        self._update_watchdog.setSingleShot(True)
        self._update_watchdog.timeout.connect(self._update_timeout)
        self._update_hide()

    def _icon(self, name):
        """Standard Qt icon by name (empty icon when the style has none)."""
        return standard_icon(self, name)

    def _action(self, text, slot, shortcut=None, icon=None, tip=None):
        act = QAction(text, self)
        if icon:
            act.setIcon(self._icon(icon))
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.setToolTip(tip or text)
        act.setStatusTip(tip or text)
        act.triggered.connect(lambda *_: slot())
        self.addAction(act)
        return act

    def _bind_fkeys(self):
        """F3..F8 as window-wide shortcuts (also the toolbar buttons)."""
        self.facts = {}
        icons = (
            "SP_FileDialogContentsView",
            "SP_FileIcon",
            "SP_DialogSaveButton",
            "SP_ArrowRight",
            "SP_FileDialogNewFolder",
            "SP_TrashIcon",
        )
        for index, (key, label) in enumerate(FKEYS):
            icon = icons[index]
            act = QAction("%s %s" % (key, label), self)
            act.setIcon(self._icon(icon))
            act.setToolTip("%s (%s)" % (label, key))
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(lambda *_a, k=key: self._press_fkey(k))
            self.addAction(act)
            self.facts[key] = act

    def _build_toolbar(self):
        tb = QToolBar("Панель инструментов", self)
        tb.setMovable(False)
        tb.setObjectName("mainToolbar")
        tb.setToolButtonStyle(TB_TextBesideIcon)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        self._bind_fkeys()

        self.act_open = self._action(
            "Открыть образ",
            self.open_image,
            "Ctrl+O",
            "SP_DialogOpenButton",
            "Открыть образ диска (Ctrl+O)",
        )
        self.act_reload = self._action(
            "Обновить",
            self.refresh_panels,
            "Ctrl+R",
            "SP_BrowserReload",
            "Обновить активную панель (Ctrl+R)",
        )
        self.act_up = self._action(
            "Вверх",
            lambda: self.active_panel().go_up(),
            None,
            "SP_ArrowUp",
            "На уровень вверх (Backspace)",
        )
        tb.addAction(self.act_open)
        tb.addAction(self.act_reload)
        tb.addAction(self.act_up)
        tb.addSeparator()
        self.act_cli = self._action(
            "Командный центр",
            self.open_command_center,
            "Ctrl+Shift+X",
            "SP_ComputerIcon",
            "Все команды Mount File CLI (Ctrl+Shift+X)",
        )
        tb.addAction(self.act_cli)
        tb.addAction(
            self._action(
                "Настройки",
                self.open_settings,
                None,
                "SP_FileDialogInfoView",
                "Настройки интерфейса",
            )
        )
        tb.addAction(
            self._action(
                "Справка",
                self.about,
                "F1",
                "SP_DialogHelpButton",
                "О программе (F1)",
            )
        )
        # Кнопка AI — последняя на панели, сразу за «Справкой».
        self.act_ai = self._action(
            "AI",
            self.ai_analyze,
            "Ctrl+I",
            "SP_ComputerIcon",
            "AI: добавить файл для анализа (Ctrl+I)",
        )
        tb.addAction(self.act_ai)

        act_menu = QAction(self)
        act_menu.setShortcut(QKeySequence("F9"))
        act_menu.triggered.connect(lambda *_: self.f9_menu())
        self.addAction(act_menu)

        act_swap = QAction(self)
        act_swap.setShortcut(QKeySequence("Ctrl+U"))
        act_swap.triggered.connect(self.swap_panels)
        self.addAction(act_swap)

        act_newtab = QAction(self)
        act_newtab.setShortcut(QKeySequence("Ctrl+T"))
        act_newtab.triggered.connect(lambda: self.active_panel().new_tab())
        self.addAction(act_newtab)

        act_closetab = QAction(self)
        act_closetab.setShortcut(QKeySequence("Ctrl+W"))
        act_closetab.triggered.connect(lambda: self.active_panel().close_tab())
        self.addAction(act_closetab)
        self._action(
            "Фильтр каталога",
            lambda: self.active_panel().search_edit.setFocus(),
            "Ctrl+F",
        )

    def _build_panels(self):
        self.panelL = TcPanel("L", self)
        self.panelR = TcPanel("R", self)
        self.panels = {"L": self.panelL, "R": self.panelR}
        for p in self.panels.values():
            p.table.installEventFilter(self)

        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(self.panelL)
        self.splitter.addWidget(self.panelR)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([500, 500])

    def _build_command_area(self):
        self.cmd_prompt = QLabel("Путь")
        self.cmd_prompt.setObjectName("panelSpace")
        self.cmdline = QLineEdit()
        self.cmdline.setObjectName("commandLine")
        self.cmdline.setAccessibleName("Командная строка активной панели")
        self.cmdline.setPlaceholderText(
            "Командная строка: «cd <путь>» — перейти в каталог активной панели"
        )
        self.cmdline.returnPressed.connect(self.on_cmdline)

        cmd_row = QHBoxLayout()
        cmd_row.setContentsMargins(2, 0, 2, 0)
        cmd_row.setSpacing(3)
        cmd_row.addWidget(self.cmd_prompt)
        cmd_row.addWidget(self.cmdline, 1)

        self.fbtns = []
        fk_row = QHBoxLayout()
        fk_row.setContentsMargins(2, 0, 2, 2)
        fk_row.setSpacing(2)
        for key, label in FKEYS:
            short = {"F5": "Копировать", "F6": "Переместить", "F7": "Папка"}.get(
                key, label
            )
            btn = QPushButton("%s  %s" % (key, short))
            btn.setObjectName("dangerButton" if key == "F8" else "functionButton")
            btn.setToolTip("%s (%s)" % (label, key))
            btn.setAccessibleName("%s (%s)" % (label, key))
            btn.clicked.connect(lambda *_a, k=key: self._press_fkey(k))
            btn.setSizePolicy(SizePolicyExpanding, SizePolicyFixed)
            fk_row.addWidget(btn)
            self.fbtns.append(btn)
        btn_exit = QPushButton("Выход")
        btn_exit.setObjectName("functionButton")
        btn_exit.setToolTip("Закрыть приложение (Alt+F4)")
        btn_exit.clicked.connect(self.close)
        btn_exit.setSizePolicy(SizePolicyExpanding, SizePolicyFixed)
        fk_row.addWidget(btn_exit)
        self.fbtns.append(btn_exit)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)
        header = QWidget()
        header.setObjectName("workspaceHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(6, 4, 6, 4)
        brand = QLabel("MOUNT FILE")
        brand.setObjectName("panelTitle")
        header_row.addWidget(brand)
        self.workspace_hint = QLabel("Образы дисков · просмотр и извлечение")
        self.workspace_hint.setObjectName("panelSpace")
        self.workspace_hint.setWordWrap(True)
        header_row.addWidget(self.workspace_hint, 1)
        v.addWidget(header)
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(self.splitter)
        v.addWidget(self.workspace_stack, 1)
        v.addLayout(cmd_row)
        v.addLayout(fk_row)
        self.setCentralWidget(central)

    def _show_panels(self):
        """Return from an embedded tool page to the two file panels."""
        current = self.workspace_stack.currentWidget()
        if isinstance(current, HexEditorPage):
            self.workspace_stack.removeWidget(current)
            current.deleteLater()
        self.workspace_stack.setCurrentWidget(self.splitter)
        self.workspace_hint.setText("Образы дисков · просмотр и извлечение")

    def _show_hex_page(self, page):
        self.workspace_stack.addWidget(page)
        self.workspace_stack.setCurrentWidget(page)
        self.workspace_hint.setText("HEX-редактор · Ctrl+S сохраняет изменения")

    def _press_fkey(self, key):
        slot = {
            "F3": self.f3_view,
            "F4": self.f4_edit,
            "F5": self.f5_copy,
            "F6": self.f6_move,
            "F7": self.f7_mkdir,
            "F8": self.f8_delete,
        }.get(key)
        if slot is not None:
            slot()

    def _build_menus(self):
        mb = self.menuBar()

        m_file = mb.addMenu("Файлы")
        m_file.addAction(self.act_open)
        m_file.addAction(
            self._action(
                "Открыть раздел…",
                lambda: self.active_panel().cmb_drive.setFocus(),
            )
        )
        m_file.addSeparator()
        m_file.addAction(self._action("Выход", self.close, "Alt+F4"))

        m_sel = mb.addMenu("Выделение")
        m_sel.addAction(
            self._action(
                "Выделить всё (Ctrl+A)", lambda: self.active_panel().mark_all()
            )
        )
        m_sel.addAction(
            self._action("Снять выделение", lambda: self.active_panel().unmark_all())
        )
        m_sel.addAction(
            self._action(
                "Инвертировать выделение", lambda: self.active_panel().invert_marks()
            )
        )
        m_sel.addSeparator()
        m_sel.addAction(self._action("Выделить по маске +…", lambda: self._mask(True)))
        m_sel.addAction(self._action("Снять по маске -…", lambda: self._mask(False)))

        m_cmd = mb.addMenu("Команды")
        for key, _label in FKEYS:
            m_cmd.addAction(self.facts[key])
        m_cmd.addSeparator()
        m_cmd.addAction(self._action("Выход", self.close, "Alt+F4"))

        m_tool = mb.addMenu("Инструменты")
        m_tool.addAction(self.act_cli)
        m_tool.addSeparator()
        m_tool.addAction(
            self._action(
                "Подобрать том автоматически…",
                lambda: self._auto_pick(quiet=False),
            )
        )
        m_tool.addAction(self._action("Сводка образа и тома…", self.show_image_info))
        m_tool.addAction(self._action("Обзор всего образа…", self.show_whole_image))
        m_tool.addAction(
            self._action(
                "Поиск файла в образе…", self.search_image_files, "Ctrl+Shift+F"
            )
        )
        m_tool.addAction(
            self._action("DFIR triage (только чтение)…", self.open_forensic_triage)
        )
        m_tool.addSeparator()
        m_tool.addAction(self._action("Декодировать дамп RT809…", self.tool_decode))
        m_tool.addAction(self._action("Разбить дамп по udev…", self.tool_split))
        m_tool.addSeparator()
        m_tool.addAction(self.act_ai)
        # Format Advisor живёт только в меню: тулбар-тесты закрепляют
        # кнопку AI последней иконкой панели, лишняя кнопка на тулбаре
        # сломала бы позиционные проверки (tests/test_ai_ui.py).
        self.act_advise = self._action(
            "Format Advisor (offline, metadata-only)",
            self._on_advise,
            "Ctrl+Shift+I",
            "SP_ComputerIcon",
            "Локальные рекомендации по имени, расширению и размеру; файл не читается (Ctrl+Shift+I)",
        )
        m_tool.addAction(self.act_advise)
        self.act_ai_hypotheses = self._action(
            "AI: гипотезы о формате файла…",
            self._on_ai_hypotheses,
            "Ctrl+Alt+I",
            "SP_FileDialogContentsView",
            "Сопоставить сигнатуру и энтропию; запрос модели выполняется только по выбору этого пункта",
        )
        m_tool.addAction(self.act_ai_hypotheses)

        m_view = mb.addMenu("Вид")
        m_view.addAction(self.act_reload)
        m_view.addAction(self._action("Сменить панели (Ctrl+U)", self.swap_panels))
        m_view.addSeparator()
        self.act_log = QAction("Журнал (Ctrl+L)", self)
        self.act_log.setCheckable(True)
        self.act_log.setChecked(bool(tc_style.SETTINGS["show_log"]))
        self.act_log.setShortcut(QKeySequence("Ctrl+L"))
        self.act_log.triggered.connect(self.toggle_log)
        m_view.addAction(self.act_log)
        self.act_menu_toggle = QAction("Меню (F9)", self)
        self.act_menu_toggle.setCheckable(True)
        self.act_menu_toggle.setChecked(True)
        self.act_menu_toggle.triggered.connect(self.f9_menu)
        m_view.addAction(self.act_menu_toggle)
        m_view.addSeparator()
        m_view.addAction(self._action("Шрифт крупнее", lambda: self.bump_font(1)))
        m_view.addAction(self._action("Шрифт мельче", lambda: self.bump_font(-1)))
        m_view.addAction(self._action("Сбросить шрифт", lambda: self.bump_font(0)))

        m_cfg = mb.addMenu("Конфигурация")
        m_cfg.addAction(self._action("Настройки…", self.open_settings))
        self.act_install_components = self._action(
            "Установить компоненты…", self.install_components
        )
        m_cfg.addAction(self.act_install_components)
        m_cfg.addSeparator()
        self.theme_acts = {}
        for label, key in sorted(tc_style.THEME_BY_LABEL.items()):
            act = QAction("Тема: %s" % label, self)
            act.setCheckable(True)
            act.setChecked(tc_style.theme() == key)
            act.triggered.connect(lambda *_a, k=key: self._apply_theme(k))
            m_cfg.addAction(act)
            self.theme_acts[key] = act
        m_cfg.addSeparator()
        m_cfg.addAction(self._action("Сохранить настройки", tc_style.save_settings))

        m_start = mb.addMenu("Запуск")
        m_start.addAction(self._action("Обновить активную панель", self.refresh_panels))
        m_start.addAction(self._action("Обновить обе панели", self.refresh_all))
        m_start.addSeparator()
        m_start.addAction(self._action("Проверить обновления", self._start_updater))
        m_start.addAction(
            self._action("Страница релизов на GitHub", self.open_releases)
        )

        m_help = QMenu("Справка", self)
        m_help.addAction(self._action("Проверить обновления", self._start_updater))
        m_help.addAction(self._action("README на GitHub", self.open_repo))
        m_help.addSeparator()
        m_help.addAction(self._action("О программе", self.about, "F1"))
        help_btn = QToolButton(self)
        help_btn.setText("Справка")
        help_btn.setMenu(m_help)
        help_btn.setPopupMode(TB_InstantPopup)
        mb.setCornerWidget(help_btn)

    def _build_log(self):
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.dock = QDockWidget("Журнал", self)
        self.dock.setWidget(self.log)
        self.addDockWidget(BottomDock, self.dock)
        self.dock.setVisible(bool(tc_style.SETTINGS["show_log"]))

    # ------------------------------------------------------------- theme
    def _apply_theme(self, name):
        tc_style.SETTINGS["theme"] = name
        tc_style.apply_theme(QApplication.instance(), name)
        for key, act in getattr(self, "theme_acts", {}).items():
            act.setChecked(key == name)
        for p in getattr(self, "panels", {}).values():
            p._fill(p._listing_hint)
            p.update_header()
            p.table.apply_font()
        self.log_msg("Тема интерфейса: %s" % tc_style.THEME_LABEL.get(name, name))

    def bump_font(self, delta):
        size = int(tc_style.SETTINGS["font_size"])
        size = 9 if delta == 0 else max(7, min(16, size + delta))
        tc_style.SETTINGS["font_size"] = size
        tc_style.apply_theme(QApplication.instance(), tc_style.theme())
        for p in self.panels.values():
            p._fill(p._listing_hint)
            p.update_header()
            p.table.apply_font()

    def open_settings(self):
        dlg = SettingsDialog(self)
        if exec_dialog(dlg) != DialogAccepted:
            return
        tc_style.SETTINGS["font_size"] = int(dlg.spn_font.value())
        tc_style.SETTINGS["show_log"] = bool(dlg.chk_log.isChecked())
        tc_style.SETTINGS["ai_backend"] = dlg.cmb_ai.currentData() or "none"
        tc_style.SETTINGS["ai_base_url"] = dlg.ed_ai_url.text().strip()
        tc_style.SETTINGS["ai_model"] = dlg.ed_ai_model.text().strip()
        tc_style.SETTINGS["ai_api_key"] = dlg.ed_ai_key.text().strip()
        tc_style.SETTINGS["ai_max_bytes"] = int(dlg.spn_ai_bytes.value()) * 1024
        tc_style.SETTINGS["ai_base_url_openai"] = dlg.ed_ai_url_openai.text().strip()
        tc_style.SETTINGS["ai_format_autohook"] = bool(dlg.chk_advise.isChecked())
        tc_style.SETTINGS["ai_consensus"] = bool(dlg.chk_consensus.isChecked())
        tc_style.SETTINGS["ai_github_repo"] = dlg.ed_advise_repo.text().strip()
        tc_style.SETTINGS["ai_github_token"] = dlg.ed_advise_token.text().strip()
        tc_style.SETTINGS["olma_watch"] = bool(dlg.chk_olma.isChecked())
        tc_style.SETTINGS["olma_token"] = dlg.ed_olma_token.text().strip()
        self.dock.setVisible(bool(dlg.chk_log.isChecked()))
        tc_style.save_settings()
        self._apply_theme(dlg.cmb_theme.currentData() or tc_style.theme())

    def install_components(self):
        """Offer the optional system installer; never alter an image itself."""
        if not components.is_windows():
            QMessageBox.information(
                self,
                "Установка компонентов",
                "В графическом интерфейсе автоустановка доступна только в Windows.\n\n"
                "%s" % components.installation_hint(),
            )
            return
        answer = QMessageBox.question(
            self,
            "Установить компоненты Windows",
            "Будет открыт отдельный PowerShell-установщик для записи образов:\n"
            "• отдельный WSL/Ubuntu-дистрибутив MountFileTools для debugfs и ext2/3/4 "
            "(около 325 МБ загрузки и место на диске);\n"
            "• подписанный драйвер ImDisk для поддерживаемых Windows-томов.\n\n"
            "Нужны интернет и подтверждение прав администратора (UAC). После установки "
            "может потребоваться перезагрузка. Образа не будут записываться этой операцией.\n\n"
            "Открыть установщик?",
            MB_Yes | MB_No,
            MB_No,
        )
        if answer != MB_Yes:
            return
        try:
            components.launch_windows_component_installer()
        except components.ComponentInstallError as exc:
            QMessageBox.warning(self, "Установка компонентов", str(exc))
            self.log_msg("Установщик компонентов не запущен: %s" % exc)
            return
        QMessageBox.information(
            self,
            "Установщик запущен",
            "PowerShell открыт в отдельном окне; результат установки пока неизвестен. "
            "Дождитесь сообщений в этом окне (возможна перезагрузка), затем переоткройте "
            "образ или перезапустите MountFile.",
        )
        self.log_msg(
            "Запущен отдельный установщик компонентов Windows; образ не изменялся."
        )

    def toggle_log(self, checked=None):
        visible = self.dock.isVisible() if checked is None else bool(checked)
        self.dock.setVisible(visible)
        tc_style.SETTINGS["show_log"] = visible
        if hasattr(self, "act_log"):
            self.act_log.setChecked(visible)

    def about(self):
        QMessageBox.information(
            self,
            "О программе",
            "Mount File v%s — файловый менеджер для raw-образов дисков\n"
            "Интерфейс: Total Commander (две панели, F3…F8)\n"
            "Python + Qt + The Sleuth Kit, лицензия MIT\n\n"
            "Обновления: %s"
            % (app_version(), "выключены" if updates_disabled() else "при старте"),
        )

    def ai_analyze(self):
        """«AI»: диалог анализа файла из панели или с диска.

        Диалог модальный: анализ синхронный, поэтому кнопка «Анализировать»
        блокируется и показывается полоса хода, чтобы окно не выглядело
        зависшим. Результат диалог сам кладёт в журнал через ``log_msg``.
        """
        dlg = AiDialog(self, self.active_panel(), log=self.log_msg)
        exec_dialog(dlg)

    def _on_advise(self):
        """Show offline, metadata-only format recommendations."""
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            QMessageBox.information(
                self, "AI-советник", "Сначала выберите файл в активной панели."
            )
            return
        from mountfile.ai_format_advisor import Remember, advise_from_metadata

        filename = entry["name"]
        remember = Remember()
        fingerprint, recipes = advise_from_metadata(
            filename, size=entry.get("size"), remember=remember
        )
        text = self._format_metadata_advice(fingerprint, recipes)
        answer = QMessageBox.question(
            self,
            "Format Advisor (offline, metadata-only)",
            text
            + "\n\nСохранить верхнюю рекомендацию локально в ~/.mountfile/ai_kb.json?",
            MB_Yes | MB_No,
            MB_No,
        )
        if answer == MB_Yes:
            try:
                remember.remember(fingerprint, recipes[0])
                self.log_msg(
                    "Format Advisor: сохранена локальная рекомендация для %s"
                    % fingerprint.filename
                )
            except OSError as exc:
                QMessageBox.information(
                    self,
                    "Format Advisor",
                    "Не удалось сохранить рекомендацию: %s" % exc,
                )

    def _on_ai_hypotheses(self):
        """Run the opt-in multi-hypothesis advisor on a selected file."""
        panel = self.active_panel()
        entry = panel.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            QMessageBox.information(
                self, "AI-анализ", "Сначала выберите файл в активной панели."
            )
            return
        from mountfile.ai_format_advisor_v2 import advise_and_show

        try:
            path = panel.join(entry["name"])
            if panel.mode == "img":
                reader = lambda offset, length: panel.provider.read_file_range(
                    path, offset, length
                )
            else:
                reader = ai.local_reader(path)
            head, _truncated = ai.collect(reader, max_bytes=4096)
            backend = ai.build_backend(tc_style.SETTINGS)
            backends = [backend]
            if tc_style.SETTINGS.get("ai_consensus"):
                alternate_key = "openai" if backend.name == "ollama" else "ollama"
                alternate_settings = dict(tc_style.SETTINGS)
                alternate_settings["ai_backend"] = alternate_key
                alternate = ai.build_backend(alternate_settings)
                if alternate.available():
                    backends.append(alternate)
            github = None
            repo = tc_style.SETTINGS.get("ai_github_repo") or ""
            token = tc_style.SETTINGS.get("ai_github_token") or ""
            if repo and token:
                from mountfile.ai_format_advisor_v2 import GitHubReporter

                try:
                    github = GitHubReporter(repo, token)
                except ValueError:
                    github = None
            self.log_msg(
                "AI-гипотезный анализ: %s — ограниченный слепок, бэкенды %s"
                % (entry["name"], ", ".join(item.name for item in backends))
            )
            advise_and_show(
                path,
                backends,
                github=github,
                parent=self,
                head=head,
                size=int(entry.get("size") or 0),
            )
        except Exception as exc:
            QMessageBox.information(
                self, "AI-анализ", "Не удалось запустить анализ: %s" % exc
            )

    @staticmethod
    def _format_metadata_advice(fingerprint, recipes):
        lines = [
            "Использованы только имя, расширение и размер. Файл не открывался; сеть и модель не вызывались.",
            "Файл: %s | расширение: %s | размер: %s"
            % (
                fingerprint.filename,
                fingerprint.extension or "неизвестно",
                fingerprint.size if fingerprint.size is not None else "неизвестен",
            ),
            "",
        ]
        for recipe in recipes:
            lines.append(
                "%d. %s (уверенность %.0f%%) — %s"
                % (
                    recipe["rank"],
                    recipe["format"],
                    100.0 * recipe["confidence"],
                    recipe["evidence"],
                )
            )
            lines.append(
                "   Инструменты (не запускаются автоматически): %s"
                % ", ".join(recipe.get("tools", []))
            )
            lines.extend("   • %s" % step for step in recipe["steps"])
        return "\n".join(lines)

    def _maybe_advise(self, path, size=None):
        """Log an offline metadata-only hint for an uncertain opened file.

        Включается галочкой «ai_format_autohook» в Настройках; никак не
        может сломать открытие файла — все ошибки глушатся в журнал.
        """
        if not tc_style.SETTINGS.get("ai_format_autohook"):
            return
        try:
            from mountfile.ai_format_advisor import advise_from_metadata

            fingerprint, recipes = advise_from_metadata(path, size=size)
            if recipes[0]["confidence"] < 0.7:
                self.log_msg(
                    "Format Advisor (offline): %s — %s; файл и сеть не использовались"
                    % (fingerprint.filename, recipes[0]["action"])
                )
        except Exception as e:  # the hook must never break opening a file
            self.log_msg("Format Advisor: %s" % e)

    def open_releases(self):
        webbrowser.open("https://github.com/Alexkkkkk/mount-file/releases/latest")

    def open_repo(self):
        webbrowser.open("https://github.com/Alexkkkkk/mount-file")

    def show_image_info(self):
        """Display the GUI equivalent of the CLI ``info``/``volumes`` commands."""
        if not self.image_path:
            QMessageBox.information(
                self, "Сводка образа", "Сначала откройте образ (Ctrl+O)."
            )
            return
        try:
            size = os.path.getsize(self.image_path)
            parts = list_partitions(self.image_path)
        except Exception as exc:
            QMessageBox.warning(
                self, "Сводка образа", "Не удалось прочитать образ:\n%s" % exc
            )
            return
        lines = [
            "Файл: %s" % self.image_path,
            "Размер: %s (%d байт)" % (tc_style.fmt_kb(size), size),
            "Томов найдено: %d" % len(parts),
            "",
        ]
        if parts:
            for part in parts:
                lines.append(
                    "%d. %s | offset 0x%X | %s MB | LBA %d"
                    % (part.index, part.name, part.offset, part.size_mb, part.start_lba)
                )
        else:
            lines.append(
                "Таблица разделов не найдена — доступен целый образ (offset 0)."
            )
        lines.extend(["", "Открытые панели:"])
        for panel in self.panels.values():
            if panel.mode == "img":
                lines.append(
                    "%s: offset 0x%X, путь %s, запись %s"
                    % (
                        panel.side,
                        panel.volume_offset,
                        panel.path,
                        "доступна" if panel.writer is not None else "только чтение",
                    )
                )
        QMessageBox.information(self, "Сводка образа и тома", "\n".join(lines))

    def show_whole_image(self):
        """Show every detected volume together instead of one at a time.

        Filesystems remain separate because their offsets and metadata are
        independent. Double-clicking a row mounts that complete volume in the
        active panel; the table itself is the whole-image view.
        """
        if not self.image_path:
            QMessageBox.information(
                self, "Обзор всего образа", "Сначала откройте образ (Ctrl+O)."
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(
            "Обзор всего образа — %s" % os.path.basename(self.image_path)
        )
        dialog.resize(900, 460)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Образ: %s\nРазмер: %s. Найдено томов: %d. "
                "Двойной клик открывает том целиком в активной панели."
                % (
                    self.image_path,
                    tc_style.fmt_kb(os.path.getsize(self.image_path)),
                    len(self.volumes),
                )
            )
        )
        table = QTableWidget(dialog)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(
            ("№", "Имя/описание", "ФС", "Offset", "Размер", "Источник")
        )
        table.setRowCount(len(self.volumes))
        table.setSelectionBehavior(SelectRows)
        table.setEditTriggers(NoEditTriggers)
        for row, volume in enumerate(self.volumes):
            offset = int(getattr(volume, "offset", 0))
            size = getattr(volume, "size", None)
            values = (
                str(row + 1),
                str(getattr(volume, "desc", "том")),
                str(getattr(volume, "fstype", "unknown")),
                "0x%X / LBA %d" % (offset, offset // 512),
                tc_style.fmt_kb(size) if size else "н/д",
                str(getattr(volume, "source", "detector")),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        table.cellDoubleClicked.connect(
            lambda row, _column: self._open_overview_volume(dialog, row)
        )
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox()
        close_button = buttons.addButton(DlgClose)
        close_button.clicked.connect(dialog.reject)
        layout.addWidget(buttons)
        exec_dialog(dialog)

    def _open_overview_volume(self, dialog, row):
        """Mount one complete volume selected from the whole-image table."""
        if row < 0 or row >= len(self.volumes):
            return
        volume = self.volumes[row]
        dialog.accept()
        self.mount_volume(self.active_panel(), int(volume.offset))
        self.log_msg("Открыт том из обзора образа: %s" % volume.desc)

    def search_image_files(self):
        """Open the filesystem-aware filename search for the whole image."""
        if not self.image_path:
            QMessageBox.information(
                self, "Поиск файла", "Сначала откройте образ (Ctrl+O)."
            )
            return
        exec_dialog(ImageFileSearchDialog(self))

    def search_files_in_image(self, pattern, *, max_results=2000, max_nodes=30000):
        """Return bounded filename matches from all detected image volumes."""
        from .fat32 import Fat32Error, Fat32Provider
        from .vfs import (
            RecognizedFilesystemError,
            TskError,
            open_image_provider,
            volume_size_for_offset,
        )

        volumes = self.volumes or [
            _PartitionVolume(
                type(
                    "Whole",
                    (),
                    {
                        "offset": 0,
                        "name": "whole image",
                        "type_name": "raw",
                        "size_mb": os.path.getsize(self.image_path) / 1048576.0,
                        "start_lba": 0,
                    },
                )()
            )
        ]
        wanted = pattern.lower()
        results = []
        for volume in volumes:
            offset = int(getattr(volume, "offset", 0))
            provider = None
            owned = False
            try:
                current = self.active_panel()
                if (
                    current.mode == "img"
                    and current.volume_offset == offset
                    and current.provider
                ):
                    provider = current.provider
                else:
                    try:
                        provider = open_image_provider(
                            self.image_path,
                            offset=offset,
                            volume_size=volume_size_for_offset(self.image_path, offset),
                        )
                    except RecognizedFilesystemError:
                        raise
                    except TskError:
                        provider = Fat32Provider(self.image_path, offset=offset)
                    owned = True
                stack = ["/"]
                visited = 0
                while stack and visited < max_nodes and len(results) < max_results:
                    directory = stack.pop()
                    visited += 1
                    for entry in provider.list_dir(directory):
                        name = str(entry.get("name", ""))
                        child = (
                            directory.rstrip("/") + "/" + name
                            if directory != "/"
                            else "/" + name
                        )
                        if (
                            fnmatch.fnmatch(name.lower(), wanted)
                            or wanted in name.lower()
                        ):
                            results.append(
                                {
                                    "volume": str(getattr(volume, "desc", "offset 0")),
                                    "fstype": str(getattr(volume, "fstype", "unknown")),
                                    "path": child,
                                    "size": int(entry.get("size") or 0),
                                    "offset": offset,
                                }
                            )
                            if len(results) >= max_results:
                                break
                        if entry.get("is_dir") and name not in (".", ".."):
                            stack.append(child)
            except (OSError, TskError, Fat32Error, RuntimeError) as exc:
                self.log_msg("Поиск: том @ 0x%X пропущен: %s" % (offset, exc))
            finally:
                if owned and provider is not None and hasattr(provider, "close"):
                    provider.close()
            if len(results) >= max_results:
                break
        return results

    def open_image_search_result(self, result):
        """Mount and navigate to a filename selected from search results."""
        panel = self.active_panel()
        if not self.mount_volume(panel, int(result["offset"])):
            return
        parent = str(PurePosixPath(result["path"]).parent)
        panel.cd(parent if parent != "." else "/")
        self.log_msg("Найден файл: %s" % result["path"])

    def open_forensic_triage(self):
        """Open bounded read-only hash/search/timeline helpers."""
        exec_dialog(ForensicTriageDialog(self, self.image_path))

    # ------------------------------------------------------ rt809 tools
    # ------------------------------------------------------- auto pick
    def _find_udev(self):
        """A udev file lying next to the image, if there is one."""
        if not self.image_path:
            return None
        try:
            found = autopick.find_udev_files(self.image_path)
        except OSError:
            return None
        return found[0] if found else None

    def _auto_pick(self, panel=None, quiet=False):
        """Pick the working volume automatically and mount it.

        Candidates come from the partition table, from a udev file next to the
        image and from filesystem signatures (including a search *inside* a
        partition). Every candidate is confirmed by a real mount, so the
        ``FS_Info_Con ... invalid bytes-per-sector`` error is answered with a
        working offset instead of a message box.
        """
        if not self.image_path:
            if not quiet:
                QMessageBox.information(
                    self, "Авто-подбор", "Сначала откройте образ (Ctrl+O)."
                )
            return False

        panel = panel or self.active_panel()
        udev = self._find_udev()
        self._autopick_busy = True
        try:
            best, candidates = autopick.auto_select(self.image_path, udev=udev)
        except Exception as e:
            self.log_msg("Авто-подбор не удался: %s" % e)
            if not quiet:
                QMessageBox.warning(
                    self, "Авто-подбор", "Не удалось выполнить:\n%s" % e
                )
            return False
        finally:
            self._autopick_busy = False

        if udev:
            self.log_msg("Авто-подбор: файл udev — %s" % udev)
        for line in autopick.format_candidates(candidates, limit=12).splitlines():
            self.log_msg(line)
        if best is None:
            self.log_msg("Авто-подбор: рабочий том не найден: %s" % self.image_path)
            if not quiet:
                QMessageBox.warning(
                    self,
                    "Авто-подбор",
                    "Рабочий том не найден.\n"
                    "Проверены разделы MBR/GPT, смещения udev и сигнатуры ФС.\n"
                    "Попробуйте указать смещение вручную или выбрать другой файл udev.",
                )
            return False

        for line in autopick.format_choice(best).splitlines():
            self.log_msg(line)
        mounted = self.mount_volume(panel, best.offset)
        if mounted:
            self.log_msg(
                "Авто-подбор: открыт том @ 0x%X (%s, вес %d)"
                % (best.offset, best.fstype, best.score)
            )
        return mounted

    def tool_decode(self):
        """Decode an RT809H-encrypted MST/MTK dump into <dump>_decode.bin."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Декодировать дамп RT809 (MST/MTK)", "", IMAGE_FILTER
        )
        if not path:
            return
        try:
            from .rt809 import decode_dump

            out = decode_dump(path)
        except Exception as e:
            self.log_msg("Ошибка декодирования: %s" % e)
            QMessageBox.warning(
                self, "Декодирование", "Не удалось декодировать:\n%s" % e
            )
            return
        if out is None:
            self.log_msg(
                "Дамп не зашифрован (маркер найден, флаг JC сброшен): %s" % path
            )
            return
        self.log_msg("Декодированный дамп записан: %s" % out)

    def tool_split(self):
        """Split a dump into partitions by a udev file.

        The udev file is parsed first, the partition table is written to the
        log, and nothing is extracted until the confirmation dialog is
        answered Yes - as in the original RT809H script.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Разбить дамп на партиции", "", IMAGE_FILTER
        )
        if not path:
            return
        udev, _ = QFileDialog.getOpenFileName(
            self, "Выбрать файл udev", "", "udev (*.*)"
        )
        if not udev:
            return
        try:
            from .rt809 import format_parts_table, parse_udev, split_by_udev

            parts = parse_udev(udev)
        except Exception as e:
            self.log_msg("Ошибка разбиения: %s" % e)
            QMessageBox.warning(
                self, "Разбиение", "Не удалось разобрать файл udev:\n%s" % e
            )
            return
        if not parts:
            self.log_msg("В файле udev нет партиций: %s" % udev)
            return
        self.log_msg("Файл udev: %s (%d партиций)" % (udev, len(parts)))
        for line in format_parts_table(parts).splitlines():
            self.log_msg(line)
        answer = QMessageBox.question(
            self,
            "Разбиение дампа",
            "Извлечь партиции?",
            MB_Yes | MB_No,
            MB_No,
        )
        if answer != MB_Yes:
            self.log_msg("Разбиение отменено — файлы не записаны.")
            return
        try:
            _, written = split_by_udev(path, udev)
        except Exception as e:
            self.log_msg("Ошибка разбиения: %s" % e)
            QMessageBox.warning(self, "Разбиение", "Не удалось разбить дамп:\n%s" % e)
            return
        for out in written:
            self.log_msg(
                "Записан: %s (%s) OK"
                % (os.path.basename(out), tc_style.fmt_kb(os.path.getsize(out)))
            )
        self.log_msg(
            "Извлечено %d партиций из %s" % (len(written), os.path.basename(path))
        )

    def open_command_center(self):
        """Launch the complete packaged CLI through its safe Qt front end."""
        from .gui_cli import open_command_center

        open_command_center(self)

    def _backup_before_write(self, panel):
        """Offer the CLI-equivalent ``--backup`` safeguard for GUI writes."""
        if panel.mode != "img" or not self.image_path:
            return True
        backup = self.image_path + ".bak"
        suffix = 1
        while os.path.exists(backup):
            backup = self.image_path + ".bak.%d" % suffix
            suffix += 1
        answer = QMessageBox.question(
            self,
            "Резервная копия образа",
            "Перед записью создать резервную копию?\n\n%s" % backup,
            MB_Yes | MB_No,
            MB_Yes,
        )
        if answer != MB_Yes:
            self.log_msg("Запись отменена: резервная копия не создана")
            return False
        try:
            shutil.copy2(self.image_path, backup)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Резервная копия не создана",
                "Операция отменена, образ не изменён:\n%s" % exc,
            )
            return False
        self.log_msg("Резервная копия создана: %s" % backup)
        return True

    def _mask(self, on):
        p = self.active_panel()
        mask, ok = QInputDialog.getText(
            self,
            "Выделение по маске",
            "%s файлы по маске (например, *.bin):" % ("Выделить" if on else "Снять"),
        )
        if ok and mask.strip():
            p.mark_mask(mask.strip(), on)

    # ----------------------------------------------------------- plumbing
    def log_msg(self, msg):
        self.log.appendPlainText(msg)
        self.statusBar().showMessage(msg, 8000)

    def active_panel(self):
        return self.panels[self.active_side]

    def other_panel(self):
        return self.panels["R" if self.active_side == "L" else "L"]

    def set_active(self, side):
        if side not in self.panels:
            return
        if self.active_side == side:
            self._sync_actions()
            return
        self.active_side = side
        for p in self.panels.values():
            p.update_header()
        self._update_cmd_prompt()

    def switch_panel(self):
        self.set_active("R" if self.active_side == "L" else "L")
        self.active_panel().table.setFocus()

    def swap_panels(self):
        left, right = self.panelL, self.panelR
        if left.mode != right.mode:
            self.log_msg(
                "Смена панелей: панели показывают разные источники "
                "(образ и локальные файлы) — переключите диски в списке"
            )
            return
        l_path, r_path = left.path, right.path
        left.tabs[left.tab_index] = r_path
        right.tabs[right.tab_index] = l_path
        left.path, right.path = r_path, l_path
        left._clear_navigation_state()
        right._clear_navigation_state()
        left._rebuild_tabs()
        right._rebuild_tabs()
        self.refresh_all()
        self.log_msg("Панели поменяны местами")

    def refresh_panels(self):
        self.active_panel().refresh()
        self._update_cmd_prompt()

    def refresh_all(self):
        for p in self.panels.values():
            p.refresh()
        self._update_cmd_prompt()

    def _update_cmd_prompt(self):
        p = self.active_panel()
        sep = os.sep if p.mode == "local" else "/"
        path = p.path if p.path.endswith(sep) else p.path + sep
        self.cmd_prompt.setText("Путь ›")
        self.cmd_prompt.setToolTip(path)
        self.cmdline.setToolTip("Активный каталог: %s" % path)
        if self.image_path:
            self.workspace_hint.setText(os.path.basename(self.image_path))
            self.workspace_hint.setToolTip(self.image_path)
        else:
            self.workspace_hint.setText("Образы дисков · просмотр и извлечение")
        prefix = (
            "[%s] " % (self.image_path or "образ не открыт") if p.mode == "img" else ""
        )
        self.setWindowTitle("%sMount File — режим Total Commander" % prefix)

    def _set_loaded(self, loaded):
        self._loaded = loaded
        for p in self.panels.values():
            p.populate_drives()
        self._sync_actions()

    def _sync_actions(self):
        """Keep menus, shortcuts and buttons aligned with real capabilities."""
        if not hasattr(self, "panels"):
            return
        panel, other = self.active_panel(), self.other_panel()
        entry = panel.selected_entry()
        available = panel.mode == "local" or panel.provider is not None
        writable = available and (panel.mode == "local" or panel.writer is not None)
        destination = other.mode == "local" or other.writer is not None
        entries = panel.marked_entries() if available else []
        can_copy = (
            bool(entries) and destination and not (panel.mode == other.mode == "img")
        )
        is_file = bool(entry and not entry.get("is_dir") and not entry.get("up"))
        editable = (
            is_file
            and writable
            and (
                (panel.mode == "local" and text_editor.is_text_file(entry["name"]))
                or (
                    os.path.splitext(entry["name"])[1].lower()
                    in EDITABLE_TEXT_EXTENSIONS
                    and int(entry.get("size") or 0) <= MAX_EDIT_BYTES
                )
            )
        )
        states = {
            "F3": is_file and available,
            "F4": editable,
            "F5": can_copy,
            "F6": can_copy and panel.mode == "local",
            "F7": writable,
            "F8": bool(entries) and writable,
        }
        reasons = {
            "F3": "Выберите файл для просмотра",
            "F4": "Выберите доступный для записи текстовый файл",
            "F5": "Выберите файлы и доступную для записи другую панель",
            "F6": "Перемещение поддерживается только из локальной панели",
            "F7": "Источник недоступен или открыт только для чтения",
            "F8": "Выберите файлы в доступной для записи панели",
        }
        for index, (key, label) in enumerate(FKEYS):
            action = self.facts[key]
            action.setEnabled(bool(states[key]))
            tip = "%s (%s)" % (label, key) if states[key] else reasons[key]
            action.setToolTip(tip)
            action.setStatusTip(tip)
            if index < len(getattr(self, "fbtns", [])):
                self.fbtns[index].setEnabled(bool(states[key]))
                self.fbtns[index].setToolTip(tip)
        self.act_up.setEnabled(available and panel.up_path() is not None)
        self.act_reload.setEnabled(available)

    def eventFilter(self, obj, e):
        if e.type() == QEvent_KeyPress and e.key() == Key_Tab:
            self.switch_panel()
            return True
        return super().eventFilter(obj, e)

    # ---------------------------------------------------------- image mount
    def open_image(self, path=None):
        """Open a raw image: an explicit path, or the file dialog."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Открыть образ диска", "", IMAGE_FILTER
            )
        if not path:
            return
        self.load_image(path)

    def load_image(self, path):
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Открыть образ", "Файл не найден:\n%s" % path)
            return False
        self.image_path = path
        for panel in self.panels.values():
            panel._clear_navigation_state()
        self.release_volume(self.panelL)
        self.release_volume(self.panelR)
        # The volume list is only a convenience: a broken partition table
        # must not stop the image itself from opening.
        self.volumes = self.scan_volumes(path)
        self.panelL.populate_drives()
        self.panelR.populate_drives()
        if self.panelR.mode == "img":
            self.panelR.mode = "local"
        # Mount the first partition when the image has a partition table,
        # otherwise treat the whole file as one volume (offset 0).
        offset = self.volumes[0].offset if self.volumes else 0
        mounted = self.mount_volume(self.panelL, offset)
        if not mounted:
            self.panelL.refresh()
        self._set_loaded(True)
        self.log_msg(
            "Образ открыт: %s (%s, томов: %d)"
            % (path, tc_style.fmt_kb(os.path.getsize(path)), len(self.volumes))
        )
        self._update_cmd_prompt()
        return True

    def scan_volumes(self, path):
        try:
            try:
                from .firmware import detect_volumes

                volumes = list(detect_volumes(path))
            except ImportError:
                volumes = [_PartitionVolume(p) for p in list_partitions(path)]
            # Signature scanning can see the same filesystem through more than
            # one compatible probe. Present one row per physical volume so the
            # whole-image view and drive selector do not show duplicates.
            unique = []
            seen = set()
            for volume in volumes:
                key = (
                    int(getattr(volume, "offset", 0)),
                    str(getattr(volume, "fstype", "")),
                    getattr(volume, "size", None),
                )
                if key in seen:
                    continue
                seen.add(key)
                unique.append(volume)
            return unique
        except Exception as e:
            self.log_msg("Сканирование томов не удалось: %s" % e)
            return []

    def mount_volume(self, panel, offset):
        """Mount one volume of the current image into `panel`."""
        if not self.image_path:
            return False
        reset_navigation = getattr(panel, "_clear_navigation_state", None)
        if reset_navigation is not None:
            reset_navigation()
        else:
            panel.marked.clear()
        self.release_volume(panel)
        panel.write_reason = ""
        volume_size = next(
            (
                getattr(volume, "size", None)
                for volume in (self.volumes or [])
                if getattr(volume, "offset", None) == offset
            ),
            None,
        )
        if volume_size is None:
            image_size = os.path.getsize(self.image_path)
            next_offsets = [
                int(getattr(volume, "offset"))
                for volume in (self.volumes or [])
                if getattr(volume, "offset", -1) > offset
            ]
            next_boundary = min(next_offsets) if next_offsets else image_size
            volume_size = max(0, next_boundary - offset)
        from .vfs import RecognizedFilesystemError, TskError, open_image_provider

        try:
            panel.provider = open_image_provider(
                self.image_path,
                offset=offset,
                volume_size=volume_size,
            )
        except RecognizedFilesystemError as recognized_error:
            panel.provider = None
            panel.refresh()
            self.log_msg(
                "Том @ offset %d распознан, но не поддерживается безопасным "
                "профилем: %s" % (offset, recognized_error)
            )
            QMessageBox.warning(
                self,
                "Том открыт только для чтения",
                "Распознанный формат или профиль не поддерживается безопасным "
                "native backend. Другой файловый парсер не будет запускаться.\n\n%s"
                % recognized_error,
            )
            return False
        except TskError as tsk_error:
            from .fat32 import Fat32Error, Fat32Provider

            try:
                panel.provider = Fat32Provider(self.image_path, offset=offset)
            except (Fat32Error, OSError, ValueError) as fat_error:
                panel.provider = None
                self.log_msg(
                    "Том @ offset %d не открылся — The Sleuth Kit: %s; FAT32: %s"
                    % (offset, tsk_error, fat_error)
                )
                panel.refresh()
                # A partition table (or a udev offset) that does not point at a
                # filesystem is the normal case in NAND/eMMC dumps: pick the
                # working volume automatically instead of stopping on an error.
                if getattr(self, "_autopick_busy", False):
                    QMessageBox.warning(
                        self,
                        "Не удалось смонтировать том",
                        "The Sleuth Kit: %s\nFAT32: %s" % (tsk_error, fat_error),
                    )
                    return False
                if self._auto_pick(panel, quiet=True):
                    return True
                QMessageBox.warning(
                    self,
                    "Не удалось смонтировать том",
                    "The Sleuth Kit: %s\nFAT32: %s\n\n"
                    "Авто-подбор не нашёл рабочий том — подробности в журнале."
                    % (tsk_error, fat_error),
                )
                return False

        panel.mode = "img"
        panel.volume_offset = offset
        panel.volume_label = "ofs %d" % offset

        from .fat32 import Fat32, Fat32Error

        fat_writer_ready = False
        try:
            fh = open(self.image_path, "r+b")
        except OSError as e:
            fh = None
            fat_error = e
        else:
            try:
                panel.writer = Fat32(fh, offset=offset)
                panel.writer_fh = fh
                panel.volume_label = "FAT32 ofs %d" % offset
                self.log_msg(
                    "Том @ offset %d смонтирован — запись в FAT32 включена" % offset
                )
                fat_writer_ready = True
            except Fat32Error as e:
                fh.close()
                fat_error = e

        # FAT32 keeps its native writer. The ordinary GUI enables writes only
        # for MountFile's in-process clean-ext2 profile; legacy debugfs is a
        # library-only explicit compatibility backend.
        if not fat_writer_ready:
            ext_writer = None
            try:
                from .ext_debugfs_writer import Ext234ImageWriter

                ext_writer = Ext234ImageWriter(
                    self.image_path, offset=offset, volume_size=volume_size
                )
            except Exception as ext_error:
                fat_error = "%s; ext: %s" % (fat_error, ext_error)
            if ext_writer is not None and not ext_writer.writable:
                panel.writer = None
                panel.writer_fh = None
                panel.write_reason = (
                    "Запись заблокирована проверкой безопасности ext2: "
                    + ext_writer.native_unsafe_error
                    if ext_writer.native_unsafe_error
                    else "Запись доступна только для поддерживаемого чистого "
                    "native-ext2 профиля; legacy debugfs отключён по умолчанию."
                )
                self.log_msg(panel.write_reason)
            elif ext_writer is not None:
                adapter = _ExtPanelWriter(ext_writer, panel.provider)
                panel.writer = adapter
                panel.writer_fh = adapter
                panel.volume_label = "%s ofs %d" % (ext_writer.fs_type, offset)
                if ext_writer.write_strategy == "native-ext2":
                    self.log_msg(
                        "Том @ offset %d открыт — встроенная запись чистого ext2 включена"
                        % offset
                    )
                else:
                    self.log_msg(
                        "Том @ offset %d смонтирован — включена запись %s"
                        % (offset, ext_writer.fs_type)
                    )
            else:
                try:
                    from .universal_fs_rw import UniversalFSRW

                    filesystem = UniversalFSRW(
                        self.image_path, offset=offset, auto_backup=False
                    )
                    adapter = _UniversalImageWriter(filesystem, panel.provider)
                    if not adapter.writable:
                        adapter.close()
                        panel.writer = None
                        panel.writer_fh = None
                        if adapter.write_strategy == "read-only-platform":
                            fs_name = str(adapter.fs_type).lower()
                            if fs_name.startswith(("ext2", "ext3", "ext4", "ext")):
                                panel.write_reason = (
                                    "Для записи ext2/3/4 нужен debugfs/e2fsprogs. "
                                    + components.installation_hint()
                                )
                            else:
                                panel.write_reason = (
                                    "Windows не поддерживает запись этого типа "
                                    "файловой системы (%s) через ImDisk. Поддерживаются "
                                    "NTFS, exFAT и FAT12/16; раздел открыт только "
                                    "для чтения." % adapter.fs_type
                                )
                        elif adapter.write_strategy == "read-only-mount-tools":
                            panel.write_reason = (
                                "Для записи через loop-mount не найдены системные "
                                "утилиты mount/umount; образ открыт только для чтения."
                            )
                        elif adapter.write_strategy == "read-only-imdisk-tools":
                            panel.write_reason = (
                                "Для записи в Windows установите ImDisk Virtual Disk "
                                "Driver. "
                                + components.installation_hint()
                                + " После установки перезапустите MountFile от имени "
                                "администратора."
                            )
                        elif adapter.write_strategy == "read-only-admin":
                            panel.write_reason = (
                                "Для записи через ImDisk перезапустите MountFile "
                                "от имени администратора."
                            )
                        else:
                            panel.write_reason = (
                                "Выбранный backend не поддерживает запись "
                                "в этот образ."
                            )
                        self.log_msg(
                            "Том @ offset %d — только чтение (%s)"
                            % (offset, panel.write_reason)
                        )
                        adapter = None
                except Exception as e:
                    panel.writer = None
                    panel.write_reason = str(e)
                    self.log_msg(
                        "Том @ offset %d — только чтение (FAT32: %s; RW: %s)"
                        % (offset, fat_error, e)
                    )
                else:
                    if adapter is not None:
                        panel.writer = adapter
                        panel.writer_fh = adapter
                        panel.volume_label = "%s ofs %d" % (
                            adapter.fs_type.upper(),
                            offset,
                        )
                        self.log_msg(
                            "Том @ offset %d смонтирован — запись включена (%s)"
                            % (offset, adapter.write_strategy)
                        )

        panel.path = "/"
        panel.tabs = ["/"]
        panel.tab_index = 0
        panel.marked.clear()
        panel._rebuild_tabs()
        panel.refresh()
        panel.populate_drives()
        self._update_cmd_prompt()
        return True

    def release_volume(self, panel):
        """Drop the mounted volume (and its write handle) of one panel."""
        if panel.writer_fh is not None:
            try:
                panel.writer_fh.close()
            except OSError:
                pass
        panel.writer_fh = None
        panel.writer = None
        provider, panel.provider = panel.provider, None
        if provider is not None and hasattr(provider, "close"):
            provider.close()

    def _close_writer(self, panel=None):
        """Close write handles (all panels when called without arguments)."""
        targets = [panel] if panel is not None else list(self.panels.values())
        for p in targets:
            if p.writer_fh is not None:
                try:
                    p.writer_fh.close()
                except OSError:
                    pass
            p.writer_fh = None
            p.writer = None

    def try_cd(self, panel, target):
        """Change directory with validation; returns True when it worked."""
        ok = False
        try:
            if panel.mode == "img":
                if panel.provider is not None:
                    panel.provider.list_dir(target)
                    ok = True
            else:
                ok = os.path.isdir(target)
        except Exception:
            ok = False
        if ok:
            panel.cd(target)
            self.log_msg("cd %s" % target)
        else:
            self.log_msg("cd: каталог не найден: %s" % target)
        return ok

    # ------------------------------------------------------------- commands
    def on_cmdline(self):
        text = self.cmdline.text().strip()
        if not text:
            return
        p = self.active_panel()
        low = text.lower()
        target = text[3:].strip() if low.startswith("cd ") else text
        if p.mode == "img" and not target.startswith("/"):
            target = p.join(target)
        if not self.try_cd(p, target):
            self.log_msg("Команда «%s» не найдена. Доступно: cd <путь>" % text)
        self.cmdline.clear()

    # -------------------------------------------------- текстовый редактор
    def _text_temp_dir(self):
        """Один временный каталог на сессию для копий текстовых файлов."""
        if self._text_tmp is None or not os.path.isdir(self._text_tmp):
            self._text_tmp = tempfile.mkdtemp(prefix="mountfile-text-")
        return self._text_tmp

    def _cleanup_text_temp(self):
        """Remove extracted editor copies once the window is being closed.

        Image edits are offered for import synchronously in
        :meth:`_open_text_entry`, so a confirmed save is committed before this
        session directory is removed.
        """
        directory, self._text_tmp = self._text_tmp, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def open_text_file(self, panel=None):
        """Открыть выбранный текстовый файл в системном редакторе.

        Windows - Блокнот (``notepad.exe``), macOS - ``open -W -t``, Linux -
        wait-capable ``$MOUNTFILE_EDITOR`` / ``$VISUAL`` / ``$EDITOR``, иначе
        gedit/kate с режимом ожидания. Detached launchers не используются,
        чтобы временную копию не удалить до закрытия редактора. Файл внутри образа сначала
        извлекается во временный каталог; после закрытия редактора изменённая
        копия предлагается к записи обратно в образ.
        """
        p = panel or self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            QMessageBox.information(
                self, "Блокнот", "Сначала выберите файл в активной панели."
            )
            return
        self._open_text_entry(p, entry)

    def _open_text_entry(self, panel, entry):
        """Открыть `entry` в редакторе и записать результат в журнал окна."""
        name = entry["name"]
        original = None
        try:
            if panel.mode == "img":
                if panel.provider is None:
                    raise RuntimeError("том не открыт")
                full = panel.join(name)
                if int(entry.get("size") or 0) > MAX_EDIT_BYTES:
                    raise ValueError("файл превышает лимит редактора 2 MiB")
                original = panel.provider.read_file(full)
                _, encoding = text_editor.decode_for_editor(original)
                if (
                    encoding == "binary"
                    or _decode_editable_text(original, encoding) is None
                ):
                    raise ValueError(
                        "файл бинарный или превышает лимит редактора 2 MiB"
                    )
                target, prepared_encoding = text_editor.prepare_image_text(
                    panel.provider,
                    full,
                    self._text_temp_dir(),
                    data=original,
                )
                if prepared_encoding != encoding:
                    raise RuntimeError(
                        "не удалось согласовать кодировку временной копии"
                    )
                note = "временная копия из образа: %s" % target
                argv = text_editor.open_in_editor(target)
            else:
                target = panel.join(name)
                encoding = "utf-8"
                note = str(target)
                argv = text_editor.open_in_editor(target)
        except Exception as e:
            QMessageBox.critical(self, "Блокнот", str(e))
            self.log_msg("Блокнот: не удалось открыть %s: %s" % (name, e))
            return
        self.log_msg(
            "Блокнот: %s -> %s (%s; кодировка %s)"
            % (name, " ".join(argv), note, encoding)
        )
        if panel.mode == "img":
            if panel.writer is None:
                reason = getattr(panel, "write_reason", "") or (
                    "для этого тома нет доступного writer"
                )
                QMessageBox.information(
                    self,
                    "Образ только для чтения",
                    "Изменения не будут записаны в образ. Временная копия: "
                    "%s\n\nПричина: %s" % (target, reason),
                )
                self.log_msg(
                    "Копия редактора не импортирована: том только для чтения (%s)"
                    % name
                )
                return
            answer = QMessageBox.question(
                self,
                "Сохранить изменения в образ",
                "Сохраните файл в редакторе и закройте его. Затем вернитесь сюда "
                "и нажмите «Да», чтобы проверить копию и записать изменения "
                "в образ. Перед записью будет создана резервная копия.\n\n%s" % target,
                MB_Yes | MB_No,
                MB_No,
            )
            if answer == MB_Yes:
                self._import_image_text_copy(panel, entry, target, original, encoding)
            else:
                self.log_msg("Копия редактора не импортирована: %s" % name)

    def _import_image_text_copy(self, panel, entry, target, original, encoding):
        """Safely import the confirmed external-editor copy into its image."""
        try:
            before = os.stat(target)
            if before.st_size > MAX_EDIT_BYTES:
                raise ValueError("редактор сохранил файл больше лимита 2 MiB")
            with open(target, "rb") as stream:
                edited = stream.read()
            after = os.stat(target)
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RuntimeError("временная копия ещё изменяется; импорт отменён")
            decoded = _decode_editable_text(edited)
            if decoded is None:
                decoded = _decode_editable_text(edited, encoding)
            if decoded is None:
                raise ValueError("редактор сохранил бинарные данные")
            text = decoded[0]
            full = panel.join(entry["name"])
            metadata = _decode_editable_text(original, encoding)
            if metadata is None:
                raise ValueError("исходный файл больше не является текстовым")
            _, _, bom, newline = metadata
            updated = _encode_editable_text(text, encoding, bom, newline)
            if updated == original:
                self.log_msg("Копия редактора не содержит изменений: %s" % full)
                return
            current = panel.provider.read_file(full)
            if current != original:
                raise RuntimeError(
                    "файл в образе изменился после открытия редактора; "
                    "обновление отменено во избежание потери изменений"
                )
            if panel.writer is None:
                raise RuntimeError("том открыт только для чтения: writer недоступен")
            if not self._backup_before_write(panel):
                return
            if panel.provider.read_file(full) != original:
                raise RuntimeError(
                    "файл в образе изменился во время резервного копирования; "
                    "запись отменена"
                )
            if panel.writer is None:
                raise RuntimeError("том открыт только для чтения: writer недоступен")
            panel.writer.write_file(panel.path, entry["name"], updated)
            panel.refresh()
            self.log_msg(
                "Изменения из Блокнота записаны в образ: %s (%d байт)"
                % (full, len(updated))
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Сохранение из Блокнота не удалось",
                "Образ не изменён или операция остановлена:\n%s" % exc,
            )
            self.log_msg("Блокнот: сохранение в образ не удалось: %s" % exc)

    def f3_view(self):
        """F3 / Enter: текстовый файл идёт в редактор, остальное в HEX."""
        p = self.active_panel()
        entry = p.selected_entry()
        if (
            entry
            and not entry.get("is_dir")
            and text_editor.is_text_file(entry["name"])
        ):
            self.open_text_file(p)
            return
        self.f3_hex_view()

    def f3_hex_view(self):
        """Open the selected file in the full interactive HEX editor."""
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("is_dir"):
            QMessageBox.information(
                self, "Просмотр", "Сначала выберите файл в активной панели."
            )
            return
        local_identity = None
        try:
            full = p.join(entry["name"])
            if p.mode == "img":
                data = p.provider.read_file(full)
            else:
                with open(full, "rb") as f:
                    data = f.read()
                    # Snapshot the path metadata used by the atomic writer's
                    # subsequent checks; expected_data also guards stale bytes.
                    local_identity = _local_file_identity(
                        os.stat(full, follow_symlinks=False)
                    )
        except Exception as e:
            QMessageBox.critical(self, "Просмотр", str(e))
            return

        def save_edited(updated):
            nonlocal data, local_identity
            answer = QMessageBox.warning(
                self,
                "Подтверждение записи HEX",
                "Записать изменения в файл?\n\n%s\nРазмер: %d байт"
                % (full, len(updated)),
                MB_Yes | MB_No,
                MB_No,
            )
            if answer != MB_Yes:
                return False
            if p.mode == "img":
                if p.writer is None:
                    raise RuntimeError("том открыт только для чтения")
                if not self._backup_before_write(p):
                    return False
                parent = str(PurePosixPath(full).parent)
                p.writer.write_file(parent, entry["name"], updated)
            else:
                _atomic_write_local(
                    full,
                    updated,
                    expected_data=data,
                    expected_identity=local_identity,
                )
                data = updated
                local_identity = _local_file_identity(
                    os.stat(full, follow_symlinks=False)
                )
            p.refresh()
            self.log_msg("HEX: записаны изменения %s (%d байт)" % (full, len(updated)))
            return True

        save_callback = save_edited if p.mode != "img" or p.writer is not None else None
        page = HexEditorPage(
            self,
            entry["name"],
            entry.get("size") or 0,
            data,
            save_callback=save_callback,
            close_callback=self._show_panels,
        )
        self._show_hex_page(page)
        self._maybe_advise(full, size=entry.get("size"))
        self.log_msg("HEX-редактор: %s" % entry["name"])

    def f4_edit(self):
        """F4: edit image text in-app; open local text in the system editor."""
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            QMessageBox.information(self, "Правка", "Выберите текстовый файл.")
            return
        name = entry["name"]
        if p.mode != "img" and text_editor.is_text_file(name):
            # Локальный файл: F4 по-прежнему отдаёт его системному редактору.
            self.open_text_file(p)
            return
        if os.path.splitext(name)[1].lower() not in EDITABLE_TEXT_EXTENSIONS:
            QMessageBox.information(
                self,
                "Правка",
                "F4 редактирует только текстовые файлы известных форматов.",
            )
            return
        if int(entry.get("size") or 0) > MAX_EDIT_BYTES:
            QMessageBox.information(
                self, "Правка", "Файл больше лимита редактирования 2 MiB."
            )
            return
        full = p.join(name)
        local_identity = None
        try:
            if p.mode == "img":
                if p.writer is None:
                    reason = getattr(p, "write_reason", "") or "нет доступного writer"
                    raise RuntimeError("том открыт только для чтения: %s" % reason)
                data = p.provider.read_file(full)
            else:
                with open(full, "rb") as stream:
                    data = stream.read(2 * 1024 * 1024 + 1)
                    local_identity = _local_file_identity(os.fstat(stream.fileno()))
        except Exception as exc:
            QMessageBox.warning(self, "Правка", "Не удалось прочитать файл:\n%s" % exc)
            return
        decoded = _decode_editable_text(data)
        if decoded is None:
            QMessageBox.information(
                self,
                "Правка",
                "Файл бинарный или больше 2 MiB; редактирование отменено.",
            )
            return
        text, encoding, bom, newline = decoded
        dialog = TextEditorDialog(self, name, text)
        if exec_dialog(dialog) != DialogAccepted:
            return
        try:
            updated = _encode_editable_text(dialog.text(), encoding, bom, newline)
        except UnicodeEncodeError as exc:
            QMessageBox.warning(
                self,
                "Правка",
                "Текст нельзя сохранить в исходной кодировке:\n%s" % exc,
            )
            return
        if updated == data:
            self.log_msg("Изменений нет: %s" % name)
            return
        if p.mode == "img":
            answer = QMessageBox.warning(
                self,
                "Подтвердите запись",
                "Записать изменения в образ?\n\n%s\n\n"
                "Перед записью будет создана резервная копия образа." % full,
                MB_Yes | MB_No,
            )
            if answer != MB_Yes or not self._backup_before_write(p):
                return
            try:
                p.writer.write_file(p.path, name, updated)
                p.refresh()
            except Exception as exc:
                QMessageBox.critical(self, "Сохранение не удалось", str(exc))
                return
            self.log_msg(
                "Изменения записаны в образ: %s (%d байт)" % (full, len(updated))
            )
            return
        try:
            _atomic_write_local(
                full,
                updated,
                expected_data=data,
                expected_identity=local_identity,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Сохранение не удалось", str(exc))
            return
        p.refresh()
        self.log_msg("Файл сохранён: %s (%d байт)" % (full, len(updated)))

    # -------------------------------------------------------------- copying
    def _gather(self, panel):
        entries = panel.marked_entries()
        if not entries:
            QMessageBox.information(
                self,
                "Mount File",
                "Отметьте файлы (Space) или поставьте курсор на файл.",
            )
        return entries

    def f5_copy(self):
        p = self.active_panel()
        other = self.other_panel()
        entries = self._gather(p)
        if not entries:
            return
        if p.mode == "img" and other.mode == "local":
            self._copy_img_to_local(p, other, entries)
        elif p.mode == "local" and other.mode == "img":
            self._copy_local_to_img(p, other, entries, delete_src=False)
        elif p.mode == "local" and other.mode == "local":
            self._copy_local_to_local(p, other, entries, delete_src=False)
        else:
            self.log_msg(
                "Копирование между двумя панелями одного образа не поддерживается"
            )

    def f6_move(self):
        p = self.active_panel()
        other = self.other_panel()
        entries = self._gather(p)
        if not entries:
            return
        if p.mode == "img" and other.mode == "local":
            self._copy_img_to_local(p, other, entries)
            self.log_msg(
                "Источник в образе не удалён: движок FAT32 не поддерживает удаление"
            )
        elif p.mode == "local" and other.mode == "img":
            self._copy_local_to_img(p, other, entries, delete_src=True)
        elif p.mode == "local" and other.mode == "local":
            self._copy_local_to_local(p, other, entries, delete_src=True)
        else:
            self.log_msg("Перемещение внутри образа не поддерживается")

    def _image_tree(self, provider, path, rel=""):
        """[(image_path, relative_path, is_dir)] for a subtree of the image."""
        out = []
        try:
            entries = provider.list_dir(path)
        except Exception as e:
            self.log_msg("Пропущен каталог %s: %s" % (path, e))
            return out
        for entry in entries:
            child = (
                (path.rstrip("/") + "/" + entry["name"])
                if path != "/"
                else "/" + entry["name"]
            )
            r = (rel + "/" + entry["name"]) if rel else entry["name"]
            out.append((child, r, entry["is_dir"]))
            if entry["is_dir"]:
                out.extend(self._image_tree(provider, child, r))
        return out

    def _copy_img_to_local(self, panel, other, entries):
        if panel.provider is None:
            return
        target_dir = other.path
        files, dirs = [], 0
        for e in entries:
            if e["is_dir"]:
                tree = self._image_tree(panel, panel.join(e["name"]), e["name"])
                for _src, rel, is_dir in tree:
                    if is_dir:
                        dirs += 1
                    else:
                        files.append((_src, os.path.join(target_dir, rel)))
            else:
                files.append(
                    (panel.join(e["name"]), os.path.join(target_dir, e["name"]))
                )
        total = sum(1 for _s, d in files if os.path.exists(d))
        if total:
            ret = QMessageBox.question(
                self,
                "Копирование",
                "%d файл(ов) уже есть в папке назначения и будет перезаписан(о).\n\n"
                "Продолжить?" % total,
                MB_Yes | MB_No,
            )
            if ret != MB_Yes:
                return
        written = 0
        for src, dst in files:
            try:
                d = os.path.dirname(dst)
                if d and not os.path.isdir(d):
                    os.makedirs(d)
                with open(dst, "wb") as f:
                    f.write(panel.provider.read_file(src))
                written += 1
            except Exception as e:
                self.log_msg("Ошибка извлечения %s: %s" % (src, e))
        other.refresh()
        self.log_msg(
            "Извлечено: файлов %d, папок %d -> %s" % (written, dirs, target_dir)
        )

    def _import_path(self, writer, src, dst_dir, name, counter):
        if os.path.isdir(src):
            writer.mkdir(dst_dir, name)
            counter["dirs"] += 1
            new_dir = (
                (dst_dir.rstrip("/") + "/" + name) if dst_dir != "/" else "/" + name
            )
            for child in sorted(os.listdir(src)):
                self._import_path(
                    writer, os.path.join(src, child), new_dir, child, counter
                )
        else:
            with open(src, "rb") as f:
                data = f.read()
            writer.write_file(dst_dir, name, data)
            counter["files"] += 1

    def _copy_local_to_img(self, panel, other, entries, delete_src):
        if other.writer is None:
            QMessageBox.warning(
                self,
                "Копирование в образ",
                "Целевой том открыт только для чтения: нужен доступный "
                "backend записи его файловой системы (FAT32 встроен, ext2/3/4 "
                "требует debugfs, NTFS — ntfs-3g на Linux или ImDisk на Windows).\n\n%s"
                % (getattr(other, "write_reason", "") or "Причина не указана."),
            )
            return
        target_dir = other.path
        listing = "\n".join(
            "  %s -> %s" % (panel.join(e["name"]), other.join(e["name"]))
            for e in entries[:8]
        )
        if len(entries) > 8:
            listing += "\n  … ещё %d" % (len(entries) - 8)
        ret = QMessageBox.warning(
            self,
            "Подтвердите запись",
            "Эта операция ЗАПИШЕТ данные в файл образа на диске:\n\n%s\n\n"
            "Убедитесь, что у образа есть резервная копия. Продолжить?" % listing,
            MB_Yes | MB_No,
        )
        if ret != MB_Yes:
            return
        if not self._backup_before_write(other):
            return
        counter = {"files": 0, "dirs": 0}
        for e in entries:
            src = panel.join(e["name"])
            try:
                self._import_path(other.writer, src, target_dir, e["name"], counter)
            except Exception as exc:
                self.log_msg("Запись не удалась (%s): %s" % (e["name"], exc))
                continue
            if delete_src:
                try:
                    if e["is_dir"]:
                        shutil.rmtree(src)
                    else:
                        os.remove(src)
                except OSError as exc:
                    self.log_msg("Не удалось удалить источник %s: %s" % (src, exc))
        panel.refresh()
        other.refresh()
        self.log_msg(
            "Записано в образ: файлов %d, папок %d -> %s"
            % (counter["files"], counter["dirs"], target_dir)
        )

    def _copy_local_to_local(self, panel, other, entries, delete_src):
        target_dir = other.path
        collisions = []
        for entry in entries:
            src = panel.join(entry["name"])
            dst = os.path.join(target_dir, entry["name"])
            source, destination = os.path.realpath(src), os.path.realpath(dst)
            same = source == destination
            if os.path.exists(dst):
                same = same or os.path.samefile(src, dst)
            nested = False
            if entry["is_dir"]:
                try:
                    nested = os.path.commonpath([source, destination]) in (
                        source,
                        destination,
                    )
                except ValueError:  # Different drives on Windows.
                    pass
            if same or nested:
                QMessageBox.warning(
                    self,
                    "Копирование отменено",
                    "Источник и назначение совпадают или вложены друг в друга.\n"
                    "Выберите другой каталог назначения.\n\n%s" % dst,
                )
                return
            if os.path.lexists(dst):
                collisions.append(dst)
        if collisions:
            preview = "\n".join(collisions[:8])
            if len(collisions) > 8:
                preview += "\n… ещё %d" % (len(collisions) - 8)
            answer = QMessageBox.warning(
                self,
                "Подтвердите замену",
                "В назначении уже существуют объекты: %d.\n\n%s\n\n"
                "Заменить их? Существующие папки будут заменены целиком."
                % (len(collisions), preview),
                MB_Yes | MB_No,
                MB_No,
            )
            if answer != MB_Yes:
                return
        for e in entries:
            src = panel.join(e["name"])
            dst = os.path.join(target_dir, e["name"])
            try:
                if e["is_dir"]:
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    if delete_src:
                        shutil.move(src, dst)
                    else:
                        shutil.copytree(src, dst)
                elif delete_src:
                    shutil.move(src, dst)
                else:
                    shutil.copy2(src, dst)
            except Exception as exc:
                self.log_msg("Ошибка %s: %s" % (e["name"], exc))
        if delete_src:
            panel.marked.clear()
        panel.refresh()
        other.refresh()
        self.log_msg(
            "%s: %d объект(ов) -> %s"
            % ("Перемещено" if delete_src else "Скопировано", len(entries), target_dir)
        )

    def f7_mkdir(self):
        p = self.active_panel()
        name, ok = QInputDialog.getText(self, "Новая папка", "Создать в %s:" % p.path)
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            if p.mode == "img":
                if p.writer is None:
                    raise RuntimeError("том открыт только для чтения")
                if not self._backup_before_write(p):
                    return
                p.writer.mkdir(p.path, name)
            else:
                os.mkdir(p.join(name))
        except Exception as e:
            QMessageBox.critical(self, "Создание папки не удалось", str(e))
            return
        p.refresh()
        self.log_msg("Создана папка %s" % p.join(name))

    def rename_selected(self):
        """Rename the cursor entry from the context menu.

        Image panels use the active filesystem writer; local panels use
        ``os.rename``. The operation intentionally targets one cursor entry,
        while batch actions continue to use the Space-marked set.
        """
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("up"):
            return
        if p.mode == "img" and p.writer is None:
            QMessageBox.information(
                self,
                "Переименование",
                "Том открыт только для чтения: backend записи недоступен.",
            )
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Переименовать",
            "Новое имя для %s:" % entry["name"],
            text=entry["name"],
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == entry["name"]:
            return
        if new_name in (".", "..") or any(sep in new_name for sep in ("/", "\\")):
            QMessageBox.warning(
                self, "Переименование", "Имя не должно содержать / или \\"
            )
            return
        try:
            if p.mode == "img":
                if not self._backup_before_write(p):
                    return
                p.writer.rename(p.join(entry["name"]), new_name)
            else:
                os.rename(p.join(entry["name"]), p.join(new_name))
        except Exception as exc:
            QMessageBox.critical(self, "Переименование не удалось", str(exc))
            return
        p.marked.discard(entry["name"])
        p.refresh()
        self.log_msg("Переименовано: %s → %s" % (entry["name"], new_name))

    def f8_delete(self):
        p = self.active_panel()
        entries = self._gather(p)
        if not entries:
            return
        if p.mode == "img":
            if p.writer is None:
                QMessageBox.information(
                    self,
                    "Удалить",
                    "Том открыт только для чтения: backend записи недоступен.",
                )
                return
            names = "\n".join("  %s" % p.join(e["name"]) for e in entries[:8])
            if len(entries) > 8:
                names += "\n  … ещё %d" % (len(entries) - 8)
            ret = QMessageBox.warning(
                self,
                "Удалить из образа",
                "Эта операция изменит образ и удалит %d объект(ов):\n\n%s\n\n"
                "Продолжить?" % (len(entries), names),
                MB_Yes | MB_No,
            )
            if ret != MB_Yes or not self._backup_before_write(p):
                return
            deleted = 0
            failed = 0
            for entry in entries:
                try:
                    p.writer.delete(
                        p.join(entry["name"]), recursive=bool(entry["is_dir"])
                    )
                    deleted += 1
                except Exception as exc:
                    failed += 1
                    self.log_msg("Удаление не удалось (%s): %s" % (entry["name"], exc))
            p.marked.clear()
            p.refresh()
            self.log_msg(
                "Удалено объектов из образа: %d; не удалось: %d" % (deleted, failed)
            )
            return
        names = "\n".join("  %s" % p.join(e["name"]) for e in entries[:8])
        if len(entries) > 8:
            names += "\n  … ещё %d" % (len(entries) - 8)
        ret = QMessageBox.warning(
            self,
            "Удалить",
            "Удалить навсегда %d объект(ов), без корзины?\n\n%s"
            % (len(entries), names),
            MB_Yes | MB_No,
            MB_No,
        )
        if ret != MB_Yes:
            return
        deleted = 0
        failed = 0
        for e in entries:
            try:
                if e["is_dir"]:
                    shutil.rmtree(p.join(e["name"]))
                else:
                    os.remove(p.join(e["name"]))
                deleted += 1
            except OSError as exc:
                failed += 1
                self.log_msg("Удаление не удалось (%s): %s" % (e["name"], exc))
        p.marked.clear()
        p.refresh()
        self.log_msg("Удалено объектов: %d; не удалось: %d" % (deleted, failed))

    def f9_menu(self, checked=None):
        mb = self.menuBar()
        visible = (not mb.isVisible()) if checked is None else bool(checked)
        mb.setVisible(visible)
        if hasattr(self, "act_menu_toggle"):
            self.act_menu_toggle.setChecked(visible)

    def _detach_updater(self):
        """Отпустить поток обновления, не ожидая его в потоке GUI.

        Раньше здесь стояло ``self.updater.wait(1500)``: окно замирало на
        полторы секунды, а во время скачивания обновления — на всё время
        загрузки. Если проверка не укладывалась в таймаут (медленная сеть,
        прокси), QThread оставался работающим, терял последнюю ссылку и
        уничтожался — процесс падал с ``Fatal Python error: Aborted``.
        Теперь поток отвязывается и доживает в :data:`_ORPHAN_THREADS`.
        """
        updater = getattr(self, "updater", None)
        self.updater = None
        if updater is None:
            return
        try:
            if updater.isRunning():
                updater.requestInterruption()
        except Exception:  # pragma: no cover - заглушка без Qt
            pass
        _ORPHAN_THREADS.append(updater)

    def closeEvent(self, e):
        page = getattr(self, "workspace_stack", None)
        page = page.currentWidget() if page is not None else None
        if isinstance(page, HexEditorPage) and page.hex_view.isModified():
            answer = QMessageBox.warning(
                self,
                "Несохранённые изменения",
                "Выйти без сохранения изменений HEX?",
                MB_Yes | MB_No,
                MB_No,
            )
            if answer != MB_Yes:
                e.ignore()
                return
        if self._text_tmp and text_editor.has_active_editors(self._text_tmp):
            QMessageBox.information(
                self,
                "Текстовый редактор ещё открыт",
                "Сохраните изменения и закройте внешний редактор, затем "
                "повторите закрытие Mount File. Временные копии пока сохранены.",
            )
            e.ignore()
            return
        self._closing = True
        self._close_writer()
        self._cleanup_text_temp()
        tc_style.save_settings()
        self._detach_updater()
        super().closeEvent(e)


class _PartitionVolume:
    """Adapter presenting a Partition as a FoundVolume-like object."""

    def __init__(self, p):
        self.offset = p.offset
        self.desc = "%d  %s  %s  %.1f MB @ LBA %d" % (
            p.index,
            p.name,
            p.type_name,
            p.size_mb,
            p.start_lba,
        )
