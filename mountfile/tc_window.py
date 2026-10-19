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

    F3  view the selected file in the hex viewer
    F4  edit (not supported - images are modified by an F5 import)
    F5  copy between panels (image -> local = extract,
        local -> image = FAT32 import via the write engine)
    F6  move (source removed when the engine can remove it)
    F7  make a directory in the active panel
    F8  delete (local filesystem only; the FAT32 engine has no delete)
    F9  toggle the menu bar

Keys inside a panel: Enter / double click opens, Backspace goes up, Tab
switches the active panel, Space marks the cursor row (Insert marks and
moves down), Ctrl+A marks everything, Ctrl+T opens a tab, Ctrl+W closes it,
Ctrl+U swaps the panels. Entries marked with Space are what F5/F6/F8 act
on. The command line understands ``cd <path>``.

All colours, fonts and column formatting live in :mod:`mountfile.tc_style`.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import threading
import webbrowser

from . import ai, autopick, tc_style
from .dfir import detect_timestomp, grep_image, read_timeline_csv, sha256_file
from .hexview import HexView
from .partition import list_partitions
from .qt_compat import (AlignRight, AlignVCenter, AscendingOrder, BottomDock,
                        ComboAdjustToContents, CtrlModifier, DescendingOrder,
                        DialogAccepted, DlgActionRole, DlgCancel, DlgClose,
                        DlgOk, EchoPassword, ElideMiddle, HVC_Interactive,
                        ItemIsEnabled, ItemIsSelectable, Key_A, Key_Backspace,
                        Key_Delete, Key_Enter, Key_Insert, Key_Return,
                        Key_Space, Key_Tab, MB_No, MB_Yes, NoEditTriggers,
                        QAction, QApplication, QBrush, QCheckBox, QComboBox,
                        QDialog, QDialogButtonBox, QDockWidget,
                        QEvent_KeyPress, QEventLoop, QFileDialog, QFormLayout,
                        QHBoxLayout, QInputDialog, QKeySequence, QLabel,
                        QLineEdit, QMainWindow, QMenu, QMessageBox,
                        QPlainTextEdit, QProgressBar, QPushButton, QSize,
                        QSpinBox, QSplitter, QTabBar, QTableWidget,
                        QTableWidgetItem, QTextEdit, QTimer, QToolBar,
                        QToolButton, QVBoxLayout, QWidget, SelectRows,
                        SizePolicyExpanding, SizePolicyFixed, Stretch,
                        TableExtendedSelection, TB_IconOnly, TB_InstantPopup,
                        CustomContextMenu,
                        exec_dialog, standard_icon)
from .updater import StartupUpdater, app_version, auto_install
from .updater import disabled as updates_disabled
from .updater import is_frozen

#: The five columns of the reference interface, in order.
COLUMNS = ("Имя", "Тип", "Размер", "Дата", "Атрибуты")

#: What Total Commander prints in the size column for a directory.
DIR_SIZE_TEXT = "<Папка>"

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

#: One-line reminder of the panel keys, shown under an empty panel.
PANEL_LEGEND = (
    "Enter — открыть, Backspace — вверх, Space — отметить, "
    "Ctrl+T — вкладка, Tab — другая панель"
)

#: F3..F8 as shown by the function key bar (the F9 menu toggle stays a
#: shortcut only, exactly like the reference window).
FKEYS = (
    ("F3", "Просмотр"),
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


class ViewerDialog(QDialog):
    """F3 viewer: hex dump of the first 1 MiB of the selected file."""

    def __init__(self, parent, name, size, data):
        super().__init__(parent)
        self.setWindowTitle("Просмотр — %s" % name)
        self._fit_to_screen()
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "%s   ·   %s байт   ·   показаны первые %d байт"
                % (name, tc_style.fmt_size(size), len(data))
            )
        )
        hv = HexView()
        hv.setData(data)
        lay.addWidget(hv, 1)
        btn = QPushButton("Закрыть (Esc)")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn, 0, AlignRight)

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
        self.ed_pattern.setPlaceholderText("Строка или regex для поиска в байтах образа")
        self.cmb_encoding = QComboBox()
        for label, value in (("UTF-8", "utf-8"), ("UTF-16LE", "utf-16le"), ("Latin-1", "latin-1"), ("Raw", "raw")):
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
        for label, slot in (("SHA-256", self.show_hash), ("Timeline CSV", self.show_timeline), ("Timestomp CSV", self.show_timestomp)):
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
        hits = list(grep_image(self.image_path, pattern, regex=self.chk_regex.isChecked(), encoding=self.cmb_encoding.currentData(), max_hits=200))
        if not hits:
            self.output.setPlainText("Совпадений не найдено.")
            return
        self.output.setPlainText("\n".join("0x%X (+%d): %s" % (h["offset"], h["length"], h["context"]) for h in hits))

    def _pick_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть timeline CSV", "", "CSV (*.csv);;Все файлы (*)")
        return path

    def show_timeline(self):
        path = self._pick_csv()
        if not path:
            return
        records = list(read_timeline_csv(path))
        self.output.setPlainText("\n".join("%s\t%s\t%s\t%s" % (r.path, r.mtime or "", r.atime or "", r.ctime or "") for r in records[:1000]) + "\n\nRecords: %d" % len(records))

    def show_timestomp(self):
        path = self._pick_csv()
        if not path:
            return
        findings = list(detect_timestomp(read_timeline_csv(path)))
        self.output.setPlainText("\n".join("%s\t%s" % (r.path, ", ".join(reasons)) for r, reasons in findings[:1000]) + "\n\nFindings: %d" % len(findings))


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
        self._relayout_columns()

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
        pad = 18
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
        fixed = sum(widths)
        if viewport - fixed < NAME_MIN_WIDTH:
            deficit = NAME_MIN_WIDTH - (viewport - fixed)
            room = sum(w - MIN_SECTION_WIDTH for w in widths)
            if room > 0:
                take = min(deficit, room)
                widths = [
                    max(
                        MIN_SECTION_WIDTH,
                        w - int(round((w - MIN_SECTION_WIDTH) * take / room)),
                    )
                    for w in widths
                ]
        for c, width in enumerate(widths, start=1):
            self.setColumnWidth(c, width)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout_columns()

    def apply_font(self):
        """Re-fit the columns after the UI font size changed."""
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
        self.volume_offset = 0
        self.volume_label = ""
        self.entries = []
        self.marked = set()
        self.tabs = [self.path]
        self.tab_index = 0
        self.sort_col = 0
        self.sort_rev = False
        self._build()
        self.populate_drives()
        self.refresh()

    # ----------------------------------------------------------------- UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.cmb_drive = QComboBox()
        self.cmb_drive.setMinimumWidth(230)
        self.cmb_drive.setSizeAdjustPolicy(ComboAdjustToContents)
        self.cmb_drive.currentIndexChanged.connect(self.drive_changed)
        bar.addWidget(self.cmb_drive)
        self.lbl_space = QLabel("")
        self.lbl_space.setToolTip("Свободное место на текущем диске / томе")
        bar.addWidget(self.lbl_space, 1)
        self.btn_root = QPushButton("\\")
        self.btn_root.setFixedWidth(24)
        self.btn_root.setToolTip("Корень диска")
        self.btn_root.clicked.connect(self.go_root)
        bar.addWidget(self.btn_root)
        self.btn_up = QPushButton("*")
        self.btn_up.setFixedWidth(24)
        self.btn_up.setToolTip("На уровень вверх (Backspace)")
        self.btn_up.clicked.connect(self.go_up)
        bar.addWidget(self.btn_up)
        lay.addLayout(bar)

        self.tabbar = QTabBar()
        self.tabbar.setExpanding(False)
        self.tabbar.setTabsClosable(True)
        self.tabbar.currentChanged.connect(self.tab_changed)
        self.tabbar.tabCloseRequested.connect(self.tab_closed)
        lay.addWidget(self.tabbar)

        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(self.path_entered)
        self.path_edit.setToolTip("Текущий каталог — можно ввести путь и нажать Enter")
        lay.addWidget(self.path_edit)

        self.table = PanelTable(self)
        lay.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        lay.addWidget(self.lbl_status)
        self._rebuild_tabs()

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
        if row >= 0:
            self.table.setCurrentCell(row, -1)
            self.win.set_active(self.side)

        entry = self.selected_entry()
        menu = QMenu(self.table)

        def add(text, slot, enabled=True):
            action = menu.addAction(text, slot)
            action.setEnabled(bool(enabled))
            return action

        is_real_entry = bool(entry and not entry.get("up"))
        is_file = bool(is_real_entry and not entry.get("is_dir"))
        can_write_image = self.mode != "img" or self.writer is not None

        add("Открыть" if entry and entry.get("is_dir") else "Просмотр (F3)",
            lambda: self.open_row(self.table.currentRow()), bool(entry))
        add("AI-анализ", self.win.ai_analyze, is_file)
        menu.addSeparator()
        add("Копировать в другую панель (F5)", self.win.f5_copy, is_real_entry)
        add("Переместить в другую панель (F6)", self.win.f6_move, is_real_entry)
        add("Переименовать", self.win.rename_selected, is_real_entry and can_write_image)
        add("Удалить (F8)", self.win.f8_delete, is_real_entry and self.mode != "img")
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
            self.tab_index = idx
            self.path = self.tabs[idx]
            self.refresh()

    def tab_closed(self, idx):
        if len(self.tabs) <= 1 or not (0 <= idx < len(self.tabs)):
            return
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
        if col == self.sort_col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col = col
            self.sort_rev = False
        self.refresh()

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
        if hint is not None:
            self.table.setRowCount(1)
            it = QTableWidgetItem(hint)
            it.setFlags(ItemIsEnabled)
            it.setForeground(QBrush(tc_style.qcolor("text_dim")))
            self.table.setItem(0, 0, it)
            self.table.setSpan(0, 0, 1, len(COLUMNS))
            return

        rows = []
        up = self.up_path()
        if up is not None:
            rows.append(
                {"name": "..", "is_dir": True, "size": None, "date": "", "up": True}
            )
        rows.extend(self.entries)

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

        self.table.setCurrentCell(self._row_of(cursor_name), -1)

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
        self.path_edit.setStyleSheet(
            "background:%s; color:%s; font-weight:bold; padding:1px 4px;"
            % (
                tc_style.color("accent") if active else tc_style.color("panel"),
                "#ffffff" if active else tc_style.color("text"),
            )
        )
        self.lbl_space.setText(self._space_text())
        self.lbl_status.setText(self._status_text())
        self.lbl_status.setStyleSheet(
            "color:%s; padding:1px 4px;" % tc_style.color("text")
        )
        self.populate_drives()

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
        e = self.selected_entry()
        if e and not e.get("up"):
            return [e]
        return []

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
        self.marked = {e["name"] for e in self.entries}
        self._fill()
        self.update_header()

    def unmark_all(self):
        self.marked.clear()
        self._fill()
        self.update_header()

    def invert_marks(self):
        self.marked = {e["name"] for e in self.entries if e["name"] not in self.marked}
        self._fill()
        self.update_header()

    def mark_mask(self, mask, on=True):
        for e in self.entries:
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
        else:
            self.win.f3_view()


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
    def use_panel(self):
        """Взять файл, выбранный в активной панели (может быть в образе)."""
        entry = self.panel.selected_entry()
        if not entry or entry.get("is_dir") or entry.get("up"):
            self.lbl_source.setText("В активной панели файл не выбран")
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mount File — режим Total Commander")
        self.setMinimumSize(640, 400)

        self.image_path = None
        self.volumes = []
        self._loaded = False

        self.active_side = "L"
        self.updater = None

        self._build_ui()
        self._fit_to_screen()
        act_fs = QAction("Полный экран (F11)", self)
        act_fs.setShortcut(QKeySequence("F11"))
        act_fs.triggered.connect(self.toggle_fullscreen)
        self.addAction(act_fs)
        self._apply_theme(tc_style.theme())
        self._set_loaded(False)
        self._start_updater()

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
    def _start_updater(self):
        """Check GitHub Releases on every start (background thread)."""
        if updates_disabled():
            self.log_msg("Автопроверка обновлений выключена (MOUNTFILE_NO_UPDATE=1)")
            return
        if self.updater is not None and self.updater.isRunning():
            return
        self.log_msg("Проверка обновлений (установлено v%s)…" % app_version())
        self.updater = StartupUpdater(self)
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

    def _on_update_restarting(self):
        self.statusBar().showMessage("Установка обновления — перезапуск…")

    # --------------------------------------- индикатор хода обновления
    def _update_stage(self, text):
        """Этап обновления: текст в статус-баре, полоса — режим «занят»."""
        self._update_stage_label.setText(text)
        self._update_stage_label.show()
        self._update_bar.setRange(0, 0)
        self._update_bar.show()

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
        self._update_stage_label.hide()
        self._update_bar.hide()

    def _on_update_installed(self):
        self._close_writer()
        if self.updater is not None:
            self.updater.wait_installer(3000)
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
        act.triggered.connect(lambda *_: slot())
        self.addAction(act)
        return act

    def _bind_fkeys(self):
        """F3..F8 as window-wide shortcuts (also the toolbar buttons)."""
        self.facts = {}
        for key, _label in FKEYS:
            act = QAction(self)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(lambda *_a, k=key: self._press_fkey(k))
            self.addAction(act)
            self.facts[key] = act

    def _build_toolbar(self):
        tb = QToolBar("Панель инструментов", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(TB_IconOnly)
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
        for key, _label in FKEYS:
            tb.addAction(self.facts[key])
        tb.addSeparator()

        self.act_pack = self._action(
            "Упаковать",
            lambda: self._unsupported("Упаковка"),
            None,
            "SP_DriveFDIcon",
            "Упаковка файлов (не поддерживается)",
        )
        self.act_unpack = self._action(
            "Распаковать",
            lambda: self._unsupported("Распаковка"),
            None,
            "SP_DriveHDIcon",
            "Распаковка архивов (не поддерживается)",
        )
        tb.addAction(self.act_pack)
        tb.addAction(self.act_unpack)
        tb.addSeparator()

        tb.addAction(
            self._action(
                "FTP",
                lambda: self._unsupported("FTP-соединение"),
                None,
                "SP_DriveNetIcon",
                "FTP-соединение (не поддерживается)",
            )
        )
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
        act_menu.triggered.connect(self.f9_menu)
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

    def _build_panels(self):
        self.panelL = TcPanel("L", self)
        self.panelR = TcPanel("R", self)
        self.panels = {"L": self.panelL, "R": self.panelR}
        for p in self.panels.values():
            p.table.installEventFilter(self)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.panelL)
        self.splitter.addWidget(self.panelR)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([500, 500])

    def _build_command_area(self):
        self.cmd_prompt = QLabel("")
        self.cmdline = QLineEdit()
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
            btn = QPushButton("%s %s" % (key, label))
            btn.clicked.connect(lambda *_a, k=key: self._press_fkey(k))
            btn.setSizePolicy(SizePolicyExpanding, SizePolicyFixed)
            fk_row.addWidget(btn)
            self.fbtns.append(btn)
        btn_exit = QPushButton("Alt+F4 Выход")
        btn_exit.clicked.connect(self.close)
        btn_exit.setSizePolicy(SizePolicyExpanding, SizePolicyFixed)
        fk_row.addWidget(btn_exit)
        self.fbtns.append(btn_exit)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(self.splitter, 1)
        v.addLayout(cmd_row)
        v.addLayout(fk_row)
        self.setCentralWidget(central)

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
        for key, label in FKEYS:
            act = QAction("%s %s" % (key, label), self)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(lambda *_a, k=key: self._press_fkey(k))
            m_cmd.addAction(act)
        m_cmd.addSeparator()
        m_cmd.addAction(self._action("Выход", self.close, "Alt+F4"))

        m_tool = mb.addMenu("Инструменты")
        m_tool.addAction(
            self._action(
                "Подобрать том автоматически…",
                lambda: self._auto_pick(quiet=False),
            )
        )
        m_tool.addAction(
            self._action("Сводка образа и тома…", self.show_image_info)
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

        m_net = mb.addMenu("Сеть")
        m_net.addAction(
            self._action("FTP-соединение…", lambda: self._unsupported("FTP-соединение"))
        )
        m_net.addAction(
            self._action("Сетевые диски…", lambda: self._unsupported("Сетевые диски"))
        )

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
            p.refresh()
            p.table.apply_font()
        self.log_msg("Тема интерфейса: %s" % tc_style.THEME_LABEL.get(name, name))

    def bump_font(self, delta):
        size = int(tc_style.SETTINGS["font_size"])
        size = 9 if delta == 0 else max(7, min(16, size + delta))
        tc_style.SETTINGS["font_size"] = size
        tc_style.apply_theme(QApplication.instance(), tc_style.theme())
        for p in self.panels.values():
            p.refresh()
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
        tc_style.SETTINGS["ai_github_repo"] = dlg.ed_advise_repo.text().strip()
        tc_style.SETTINGS["ai_github_token"] = dlg.ed_advise_token.text().strip()
        tc_style.SETTINGS["olma_watch"] = bool(dlg.chk_olma.isChecked())
        tc_style.SETTINGS["olma_token"] = dlg.ed_olma_token.text().strip()
        self.dock.setVisible(bool(dlg.chk_log.isChecked()))
        tc_style.save_settings()
        self._apply_theme(dlg.cmb_theme.currentData() or tc_style.theme())

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
            text + "\n\nСохранить верхнюю рекомендацию локально в ~/.mountfile/ai_kb.json?",
            MB_Yes | MB_No,
            MB_No,
        )
        if answer == MB_Yes:
            try:
                remember.remember(fingerprint, recipes[0])
                self.log_msg("Format Advisor: сохранена локальная рекомендация для %s" % fingerprint.filename)
            except OSError as exc:
                QMessageBox.information(self, "Format Advisor", "Не удалось сохранить рекомендацию: %s" % exc)

    @staticmethod
    def _format_metadata_advice(fingerprint, recipes):
        lines = [
            "Использованы только имя, расширение и размер. Файл не открывался; сеть и модель не вызывались.",
            "Файл: %s | расширение: %s | размер: %s" % (
                fingerprint.filename,
                fingerprint.extension or "неизвестно",
                fingerprint.size if fingerprint.size is not None else "неизвестен",
            ),
            "",
        ]
        for recipe in recipes:
            lines.append("%d. %s (уверенность %.0f%%) — %s" % (
                recipe["rank"], recipe["format"], 100.0 * recipe["confidence"], recipe["evidence"]
            ))
            lines.append("   Инструменты (не запускаются автоматически): %s" % ", ".join(recipe.get("tools", [])))
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
            QMessageBox.information(self, "Сводка образа", "Сначала откройте образ (Ctrl+O).")
            return
        try:
            size = os.path.getsize(self.image_path)
            parts = list_partitions(self.image_path)
        except Exception as exc:
            QMessageBox.warning(self, "Сводка образа", "Не удалось прочитать образ:\n%s" % exc)
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
            lines.append("Таблица разделов не найдена — доступен целый образ (offset 0).")
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

    def _unsupported(self, what):
        self.log_msg("%s: не поддерживается в этой сборке" % what)

    def _backup_before_write(self, panel):
        """Offer the CLI-equivalent ``--backup`` safeguard for GUI writes."""
        if panel.mode != "img" or not self.image_path:
            return True
        backup = self.image_path + ".bak"
        answer = QMessageBox.question(
            self,
            "Резервная копия образа",
            "Перед записью создать резервную копию?\n\n%s" % backup,
            MB_Yes | MB_No,
            MB_Yes,
        )
        if answer != MB_Yes:
            return True
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
        self.cmd_prompt.setText("%s>" % path)
        prefix = (
            "[%s] " % (self.image_path or "образ не открыт") if p.mode == "img" else ""
        )
        self.setWindowTitle("%sMount File — режим Total Commander" % prefix)

    def _set_loaded(self, loaded):
        self._loaded = loaded
        for p in self.panels.values():
            p.populate_drives()
        for b in getattr(self, "fbtns", []):
            b.setEnabled(True)

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

                return list(detect_volumes(path))
            except ImportError:
                return [_PartitionVolume(p) for p in list_partitions(path)]
        except Exception as e:
            self.log_msg("Сканирование томов не удалось: %s" % e)
            return []

    def mount_volume(self, panel, offset):
        """Mount one volume of the current image into `panel`."""
        if not self.image_path:
            return False
        self.release_volume(panel)
        from .vfs import TskError, TskImage

        try:
            panel.provider = TskImage(self.image_path, offset=offset)
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

        try:
            fh = open(self.image_path, "r+b")
        except OSError as e:
            self.log_msg("Том @ offset %d — только чтение (%s)" % (offset, e))
        else:
            try:
                panel.writer = Fat32(fh, offset=offset)
                panel.writer_fh = fh
                panel.volume_label = "FAT32 ofs %d" % offset
                self.log_msg(
                    "Том @ offset %d смонтирован — запись в FAT32 включена" % offset
                )
            except Fat32Error as e:
                fh.close()
                self.log_msg("Том @ offset %d — только чтение: %s" % (offset, e))

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

    def f3_view(self):
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("is_dir"):
            QMessageBox.information(
                self, "Просмотр", "Сначала выберите файл в активной панели."
            )
            return
        try:
            full = p.join(entry["name"])
            if p.mode == "img":
                data = p.provider.read_file_range(full, 0, PREVIEW_LIMIT)
            else:
                with open(full, "rb") as f:
                    data = f.read(PREVIEW_LIMIT)
        except Exception as e:
            QMessageBox.critical(self, "Просмотр", str(e))
            return
        exec_dialog(ViewerDialog(self, entry["name"], entry.get("size") or 0, data))
        self._maybe_advise(full, size=entry.get("size"))
        self.log_msg("Просмотр: %s" % entry["name"])

    def f4_edit(self):
        p = self.active_panel()
        entry = p.selected_entry()
        if p.mode == "img" and entry and not entry.get("is_dir"):
            self.log_msg(
                "Правка (F4): файлы внутри образа изменяются только импортом "
                "через F5 (%s)" % entry["name"]
            )
            return
        self.log_msg(
            "Правка (F4) не поддерживается — образ изменяется только "
            "импортом через F5"
        )

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
                "Том в левой панели открыт только для чтения: запись "
                "поддерживается только в FAT32.",
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

        FAT32 uses its native writer; local panels use ``os.rename``. The
        operation intentionally targets one cursor entry, while batch actions
        continue to use the Space-marked set.
        """
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("up"):
            return
        if p.mode == "img" and p.writer is None:
            QMessageBox.information(
                self,
                "Переименование",
                "Том открыт только для чтения: переименование доступно "
                "только в FAT32.",
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
            QMessageBox.warning(self, "Переименование", "Имя не должно содержать / или \\")
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
            QMessageBox.information(
                self,
                "Удалить",
                "Удаление из образа не поддерживается движком FAT32 "
                "(создание, перезапись и mkdir).",
            )
            return
        names = "\n".join("  %s" % p.join(e["name"]) for e in entries[:8])
        if len(entries) > 8:
            names += "\n  … ещё %d" % (len(entries) - 8)
        ret = QMessageBox.warning(
            self,
            "Удалить",
            "Удалить %d объект(ов)?\n\n%s" % (len(entries), names),
            MB_Yes | MB_No,
        )
        if ret != MB_Yes:
            return
        for e in entries:
            try:
                if e["is_dir"]:
                    shutil.rmtree(p.join(e["name"]))
                else:
                    os.remove(p.join(e["name"]))
            except OSError as exc:
                self.log_msg("Удаление не удалось (%s): %s" % (e["name"], exc))
        p.marked.clear()
        p.refresh()
        self.log_msg("Удалено объектов: %d" % len(entries))

    def f9_menu(self, checked=None):
        mb = self.menuBar()
        visible = (not mb.isVisible()) if checked is None else bool(checked)
        mb.setVisible(visible)
        if hasattr(self, "act_menu_toggle"):
            self.act_menu_toggle.setChecked(visible)

    def closeEvent(self, e):
        self._close_writer()
        tc_style.save_settings()
        if self.updater is not None and self.updater.isRunning():
            self.updater.wait(1500)
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
