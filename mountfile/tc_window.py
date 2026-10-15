# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Total Commander-style dual-pane main window (Qt5 / Qt6 via qt_compat).

Left panel browses the mounted disk image, right panel browses the local
filesystem of the host. Function keys (also work as real shortcuts):

    F3  view selected file in the hex viewer
    F4  edit (not supported - images are modified via F5 import only)
    F5  copy between panels (image -> local = extract,
        local -> image = FAT32 import via the write engine)
    F6  move (local -> image/image -> local, source removed when possible)
    F7  make directory in the active panel
    F8  delete (local filesystem only; the FAT32 engine has no delete)
    F9  toggle menu bar

Keys inside a panel: Enter / double-click opens, Backspace goes up,
Tab switches the active panel. The command line understands "cd <path>".
"""

from __future__ import annotations

import os
import time

from .hexview import HexView
from .partition import list_partitions
from .qt_compat import (AlignRight, AlignVCenter, BottomDock,
                        HVC_ResizeToContents, ItemIsEnabled, ItemIsSelectable,
                        Key_Backspace, Key_Enter, Key_Return, Key_Tab, MB_No,
                        MB_Yes, NoEditTriggers, QAction, QApplication,
                        QComboBox, QDialog, QDockWidget, QEvent_KeyPress,
                        QFileDialog, QHBoxLayout,
                        QInputDialog, QKeySequence, QLabel, QLineEdit,
                        QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                        QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                        QWidget, SC_Open, SelectRows, Stretch,
                        TableSingleSelection, exec_dialog)
from .updater import (StartupUpdater, app_version, auto_install,
                      disabled as updates_disabled, is_frozen)


def fmt_size(n):
    """1234 -> '1.2K', 512 -> '512' (Total Commander style, no spaces)."""
    n = float(n)
    for unit in ("", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            if unit == "":
                return "%d" % n
            s = "%.1f" % n
            return s[:-2] if s.endswith(".0") else s + unit
        n /= 1024
    return "%d" % n


def fmt_date(ts):
    try:
        if not ts or ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


class ViewerDialog(QDialog):
    """F3 viewer: hex dump of the first 1 MiB of the selected file."""

    def __init__(self, parent, name, size, data):
        super().__init__(parent)
        self.setWindowTitle("Просмотр — %s" % name)
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "%s   ·   %s байт   ·   показаны первые %d байт"
                % (name, size, len(data))
            )
        )
        hv = HexView()
        hv.setData(data)
        lay.addWidget(hv, 1)
        btn = QPushButton("Закрыть (Esc)")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn, 0, AlignRight)


class PanelTable(QTableWidget):
    """File list with Total Commander key bindings."""

    def __init__(self, panel):
        super().__init__(0, 4)
        self.panel = panel
        self.setHorizontalHeaderLabels(["Имя", "Расш", "Размер", "Дата"])
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, HVC_ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setSelectionBehavior(SelectRows)
        self.setSelectionMode(TableSingleSelection)
        self.setEditTriggers(NoEditTriggers)
        self.doubleClicked.connect(lambda *_: panel.open_row(self.currentRow()))

    def keyPressEvent(self, e):
        if e.key() == Key_Backspace:
            self.panel.go_up()
            return
        if e.key() in (Key_Return, Key_Enter):
            self.panel.open_row(self.currentRow())
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.panel.win.set_active(self.panel.side)


class TcPanel(QWidget):
    """One file panel: path header + flat listing of the current directory."""

    def __init__(self, side, win):
        super().__init__()
        self.side = side
        self.win = win
        self.mode = "img" if side == "L" else "local"
        self.path = "/" if self.mode == "img" else os.path.expanduser("~")
        self.entries = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.header = QLabel()
        lay.addWidget(self.header)
        self.table = PanelTable(self)
        lay.addWidget(self.table)
        self.refresh()

    # -- paths ---------------------------------------------------------
    def join(self, name):
        if self.mode == "local":
            return os.path.join(self.path, name)
        return (self.path.rstrip("/") + "/" + name) if self.path != "/" else "/" + name

    def up_path(self):
        if self.mode == "local":
            p = os.path.dirname(self.path)
            return None if not p or p == self.path else p
        if self.path == "/":
            return None
        return self.path.rstrip("/").rsplit("/", 1)[0] or "/"

    def cd(self, path):
        self.path = path
        self.refresh()

    def go_up(self):
        up = self.up_path()
        if up is not None:
            self.cd(up)

    # -- data ------------------------------------------------------------
    def refresh(self):
        self.entries = []
        hint = None
        if self.mode == "img":
            if self.win.provider is None:
                hint = (
                    "образ не открыт — нажмите «Открыть образ…» в панели инструментов"
                )
            else:
                try:
                    self.entries = self.win.provider.list_dir(self.path)
                except Exception as e:
                    hint = "ошибка чтения каталога: %s" % e
        else:
            try:
                with os.scandir(self.path) as it:
                    for e in it:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                            st = e.stat()
                        except OSError:
                            continue
                        self.entries.append(
                            {
                                "name": e.name,
                                "is_dir": is_dir,
                                "size": 0 if is_dir else int(st.st_size),
                                "date": fmt_date(st.st_mtime),
                            }
                        )
            except OSError as e:
                hint = "ошибка чтения каталога: %s" % e
        self.entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        self._fill(hint)

    def _fill(self, hint=None):
        self.table.clearSpans()
        if hint is not None:
            self.table.setRowCount(1)
            it = QTableWidgetItem(hint)
            it.setFlags(ItemIsEnabled)
            self.table.setItem(0, 0, it)
            self.table.setSpan(0, 0, 1, 4)
            self.update_header()
            return

        rows = []
        up = self.up_path()
        if up is not None:
            rows.append(
                {"name": "..", "is_dir": True, "size": None, "date": "", "up": True}
            )
        rows.extend(self.entries)

        self.table.setRowCount(len(rows))
        for r, e in enumerate(rows):
            name = e["name"]
            it = QTableWidgetItem(name + ("/" if e["is_dir"] and name != ".." else ""))
            it.setFlags(ItemIsEnabled | ItemIsSelectable)
            self.table.setItem(r, 0, it)
            if name == "..":
                vals = ("", "", "")
            elif e["is_dir"]:
                vals = ("<папка>", "", e.get("date", ""))
            else:
                stem, dot, ext = name.rpartition(".")
                vals = (
                    ext if dot and stem else "",
                    fmt_size(e["size"]),
                    e.get("date", ""),
                )
            for c, text in enumerate(vals, start=1):
                x = QTableWidgetItem(text)
                x.setFlags(ItemIsEnabled | ItemIsSelectable)
                if c == 2:
                    x.setTextAlignment(AlignRight | AlignVCenter)
                self.table.setItem(r, c, x)
        self.table.setCurrentCell(-1, -1)
        self.update_header()

    def update_header(self):
        if self.mode == "img":
            src = os.path.basename(self.win.image_path) if self.win.image_path else "—"
            text = "[%s · %s] %s" % (src, self.win.fs_label or "?", self.path)
        else:
            text = self.path
        self.header.setText(text)
        if self.win.active_side == self.side:
            self.header.setStyleSheet(
                "background:#3875d7; color:white; padding:3px 6px;"
                " border-radius:4px; font-weight:bold;"
            )
        else:
            self.header.setStyleSheet(
                "padding:3px 6px; border:1px solid #808080;"
                " border-radius:4px; font-weight:bold;"
            )

    # -- selection / actions ----------------------------------------------
    def selected_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        name = item.text().rstrip("/")
        if name == "..":
            return {"name": "..", "is_dir": True, "up": True}
        return next((e for e in self.entries if e["name"] == name), None)

    def open_row(self, row):
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        name = item.text().rstrip("/")
        if name == "..":
            self.go_up()
            return
        entry = next((e for e in self.entries if e["name"] == name), None)
        if entry is None:
            return
        if entry["is_dir"]:
            self.cd(self.join(name))
        else:
            self.win.f3_view()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mount File — режим Total Commander")
        self.resize(1100, 700)

        self.image_path = None
        self.provider = None
        self.fs_label = None
        self.writer = None
        self.writer_fh = None
        self.active_side = "L"

        self.updater = None
        self._build_ui()
        self._set_loaded(False)
        self._start_updater()

    # ----------------------------------------------------------- обновление
    def _start_updater(self):
        """Проверка обновлений GitHub при КАЖДОМ старте (фоновым потоком)."""
        if updates_disabled():
            self.log_msg(
                "Автопроверка обновлений выключена (MOUNTFILE_NO_UPDATE=1)"
            )
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
        self.updater.restarting.connect(self._on_update_restarting)
        self.updater.installed.connect(self._on_update_installed)
        self.updater.start()

    def _on_update_available(self, info):
        """Диалог «установить сейчас?» (или ничего, если режим авто)."""
        version = info.get("version") or "?"
        asset = info.get("asset")
        if not asset:
            if info.get("url"):
                QMessageBox.information(
                    self,
                    "Доступна новая версия",
                    "Версия v%s уже вышла, но сборки для вашей системы "
                    "в релизе нет.\n\nСтраница релиза:\n%s"
                    % (version, info["url"]),
                )
            return
        if not is_frozen():
            self.log_msg(
                "Новая версия v%s. Запуск из исходников — файлы не меняются: "
                "%s" % (version, info.get("url") or "")
            )
            return
        if auto_install():
            return  # установку уже запустил StartupUpdater
        ret = QMessageBox.question(
            self,
            "Обновление Mount File",
            "Доступна версия v%s (у вас v%s).\n\n"
            "Скачать %s и установить сейчас? Программа перезапустится "
            "сама.\n"
            "Подробности — в журнале внизу окна."
            % (version, app_version(), asset.get("name")),
            MB_Yes | MB_No,
        )
        if ret == MB_Yes:
            self.updater.install(info, self)

    def _on_update_restarting(self):
        self.statusBar().showMessage("Установка обновления — перезапуск…")

    def _on_update_installed(self):
        """Обновление развёрнуто: закрываемся, чтобы .cmd смог подменить exe."""
        self._close_writer()
        if self.updater is not None:
            self.updater.wait_installer(3000)
        QApplication.quit()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        tb = self.addToolBar("Main")
        self.act_open = QAction("Открыть образ…", self)
        self.act_open.setShortcut(SC_Open)
        self.act_open.triggered.connect(self.open_image)
        tb.addAction(self.act_open)

        tb.addWidget(QLabel("  Том: "))
        self.cmb_parts = QComboBox()
        self.cmb_parts.setMinimumWidth(360)
        self.cmb_parts.currentIndexChanged.connect(self.volume_changed)
        tb.addWidget(self.cmb_parts)

        self.panelL = TcPanel("L", self)
        self.panelR = TcPanel("R", self)
        self.panels = {"L": self.panelL, "R": self.panelR}
        for p in self.panels.values():
            p.table.installEventFilter(self)

        splitter = QSplitter()
        splitter.addWidget(self.panelL)
        splitter.addWidget(self.panelR)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self.cmdline = QLineEdit()
        self.cmdline.setPlaceholderText(
            "Командная строка: «cd /путь» — сменить каталог активной панели, "
            "Enter — выполнить"
        )
        self.cmdline.returnPressed.connect(self.on_cmdline)

        keys = [
            ("F3", "Просмотр", self.f3_view),
            ("F4", "Правка", self.f4_edit),
            ("F5", "Копировать", self.f5_copy),
            ("F6", "Переместить", self.f6_move),
            ("F7", "Папка", self.f7_mkdir),
            ("F8", "Удалить", self.f8_delete),
            ("F9", "Меню", self.f9_menu),
        ]
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(4, 0, 4, 4)
        self.fbtns = []
        self.facts = {}
        for key, label, slot in keys:
            b = QPushButton("%s %s" % (key, label))
            b.clicked.connect(slot)
            btn_row.addWidget(b)
            self.fbtns.append(b)
            act = QAction(self)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(slot)
            self.addAction(act)
            self.facts[key] = act

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(splitter, 1)
        v.addWidget(self.cmdline)
        v.addLayout(btn_row)
        self.setCentralWidget(central)

        mb = self.menuBar()
        m_file = mb.addMenu("Файлы")
        m_file.addAction(self.act_open)
        act_quit = QAction("Выход", self)
        act_quit.setShortcut(QKeySequence("Alt+F4"))
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)
        m_cmd = mb.addMenu("Команды")
        for key in ("F3", "F4", "F5", "F6", "F7", "F8"):
            m_cmd.addAction(self.facts[key])
        m_help = mb.addMenu("Справка")
        act_upd = QAction("Проверить обновления", self)
        act_upd.triggered.connect(self._start_updater)
        m_help.addAction(act_upd)
        act_about = QAction("О программе", self)
        act_about.triggered.connect(self.about)
        m_help.addAction(act_about)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        dock = QDockWidget("Журнал", self)
        dock.setWidget(self.log)
        self.addDockWidget(BottomDock, dock)

        self.statusBar().showMessage("Откройте образ, чтобы начать")

    def about(self):
        QMessageBox.information(
            self,
            "О программе",
            "Mount File v%s — файловый менеджер для raw-образов дисков\n"
            "Режим интерфейса: Total Commander (две панели)\n"
            "Python + Qt + The Sleuth Kit, лицензия MIT\n\n"
            "Обновления: %s"
            % (app_version(), "выключены" if updates_disabled() else "при старте"),
        )

    # ------------------------------------------------------------- plumbing
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

    def switch_panel(self):
        self.set_active("R" if self.active_side == "L" else "L")
        self.active_panel().table.setFocus()

    def eventFilter(self, obj, e):
        if e.type() == QEvent_KeyPress and e.key() == Key_Tab:
            self.switch_panel()
            return True
        return super().eventFilter(obj, e)

    def _set_loaded(self, loaded):
        self.cmb_parts.setEnabled(loaded)
        for b in self.fbtns:
            b.setEnabled(loaded)

    # ---------------------------------------------------------- image mount
    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть образ диска",
            "",
            "Образы дисков (*.img *.bin *.raw *.dump *.dd);;Все файлы (*)",
        )
        if not path:
            return
        self.image_path = path
        self._close_writer()
        self.cmb_parts.blockSignals(True)
        self.cmb_parts.clear()
        self.cmb_parts.addItem("Целый образ (offset 0)", 0)
        try:
            try:
                from .firmware import detect_volumes

                vols = detect_volumes(path)
            except ImportError:
                vols = []
                for p in list_partitions(path):
                    vols.append(_PartitionVolume(p))
            for v in vols:
                self.cmb_parts.addItem(v.desc, v.offset)
        except Exception as e:
            self.log_msg("Сканирование томов не удалось: %s" % e)
        self.cmb_parts.blockSignals(False)
        self.cmb_parts.setCurrentIndex(0)
        self.panelL.path = "/"
        self._mount_volume(0)
        self._set_loaded(True)
        self.log_msg("Образ открыт: %s" % path)

    def volume_changed(self, idx):
        if self.image_path:
            self._mount_volume(self.cmb_parts.itemData(idx))

    def _mount_volume(self, offset):
        from .vfs import TskError, TskImage

        try:
            self.provider = TskImage(self.image_path, offset=offset)
        except TskError as e:
            QMessageBox.warning(
                self,
                "Не удалось смонтировать том",
                "The Sleuth Kit не смог открыть этот том:\n%s\n\n"
                "Совет: выберите конкретный раздел или установите pytsk3." % e,
            )
            self.provider = None
            self.panelL.refresh()
            return
        self.fs_label = "ofs %d" % offset
        self.panelL.path = "/"
        self.panelL.refresh()

        self._close_writer()
        from .fat32 import Fat32, Fat32Error

        try:
            self.writer_fh = open(self.image_path, "r+b")
            self.writer = Fat32(self.writer_fh, offset=offset)
            self.fs_label = "FAT32 ofs %d" % offset
            self.log_msg(
                "Том смонтирован @ offset %d — запись в FAT32 включена" % offset
            )
        except (Fat32Error, OSError) as e:
            self.writer = None
            self.log_msg(
                "Том смонтирован @ offset %d (только чтение: %s)" % (offset, e)
            )

    def _close_writer(self):
        if self.writer_fh:
            try:
                self.writer_fh.close()
            except OSError:
                pass
        self.writer_fh = None
        self.writer = None

    # ------------------------------------------------------------- commands
    def on_cmdline(self):
        text = self.cmdline.text().strip()
        if not text:
            return
        p = self.active_panel()
        low = text.lower()
        if low.startswith("cd "):
            target = text[3:].strip()
            if p.mode == "img" and not target.startswith("/"):
                target = p.join(target)
            ok = False
            try:
                if p.mode == "img":
                    self.provider.list_dir(target)
                    ok = True
                else:
                    ok = os.path.isdir(target)
            except Exception:
                ok = False
            if ok:
                p.cd(target)
                self.log_msg("cd %s" % target)
            else:
                self.log_msg("cd: каталог не найден: %s" % target)
        else:
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
                data = self.provider.read_file_range(full, 0, 1 << 20)
            else:
                with open(full, "rb") as f:
                    data = f.read(1 << 20)
        except Exception as e:
            QMessageBox.critical(self, "Просмотр", str(e))
            return
        exec_dialog(ViewerDialog(self, entry["name"], entry["size"], data))
        self.log_msg("Просмотр: %s" % entry["name"])

    def f4_edit(self):
        self.log_msg(
            "Правка (F4) не поддерживается — образ изменяется "
            "только импортом через F5"
        )

    def _checked_file(self):
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("up"):
            QMessageBox.information(
                self, "Mount File", "Сначала выберите файл в активной панели."
            )
            return None, None
        if entry["is_dir"]:
            QMessageBox.information(
                self,
                "Mount File",
                "Каталоги в этом режиме не поддерживаются — выберите файл.",
            )
            return None, None
        return p, entry

    def f5_copy(self):
        p, entry = self._checked_file()
        if p is None:
            return
        if p.mode == "img":
            self._extract(entry)
        else:
            self._import(entry, delete_src=False)

    def f6_move(self):
        p, entry = self._checked_file()
        if p is None:
            return
        if p.mode == "img":
            self._extract(entry)
            self.log_msg(
                "Исходный файл в образе не удалён: движок FAT32 "
                "не поддерживает удаление"
            )
        else:
            self._import(entry, delete_src=True)

    def _extract(self, entry):
        p = self.active_panel()
        other = self.other_panel()
        if other.mode != "local":
            QMessageBox.warning(
                self, "Копировать", "Правая панель должна быть локальной папкой."
            )
            return
        dst, _ = QFileDialog.getSaveFileName(
            self, "Копировать в…", os.path.join(other.path, entry["name"])
        )
        if not dst:
            return
        try:
            data = self.provider.read_file(p.join(entry["name"]))
            with open(dst, "wb") as f:
                f.write(data)
        except Exception as e:
            QMessageBox.critical(self, "Копирование не удалось", str(e))
            return
        other.refresh()
        self.log_msg("Извлечено %s (%d байт) -> %s" % (entry["name"], len(data), dst))

    def _import(self, entry, delete_src):
        p = self.active_panel()
        other = self.other_panel()
        if other.mode != "img" or self.writer is None:
            QMessageBox.warning(
                self,
                "Копировать",
                "Левая панель должна показывать FAT32-том образа "
                "(запись в другие ФС не поддерживается).",
            )
            return
        src = p.join(entry["name"])
        target_dir = other.path
        ret = QMessageBox.warning(
            self,
            "Подтвердите запись",
            "Эта операция ЗАПИШЕТ данные в файл образа на диске:\n\n"
            "  %s\n  -> %s%s\n\n"
            "Убедитесь, что у образа есть резервная копия. Продолжить?"
            % (src, target_dir, entry["name"]),
            MB_Yes | MB_No,
        )
        if ret != MB_Yes:
            return
        try:
            with open(src, "rb") as f:
                data = f.read()
            self.writer.write_file(target_dir, entry["name"], data)
        except Exception as e:
            QMessageBox.critical(self, "Запись не удалась", str(e))
            return
        other.refresh()
        if delete_src:
            try:
                os.remove(src)
            except OSError as e:
                self.log_msg("Не удалось удалить источник: %s" % e)
        self.log_msg(
            "Записано в образ %s (%d байт) -> %s%s"
            % (entry["name"], len(data), target_dir, entry["name"])
        )

    def f7_mkdir(self):
        p = self.active_panel()
        name, ok = QInputDialog.getText(self, "Новая папка", "Создать в %s:" % p.path)
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            if p.mode == "img":
                if self.writer is None:
                    raise RuntimeError("том открыт только для чтения")
                self.writer.mkdir(p.path, name)
            else:
                os.mkdir(p.join(name))
        except Exception as e:
            QMessageBox.critical(self, "Создание папки не удалось", str(e))
            return
        p.refresh()
        self.log_msg("Создана папка %s%s" % (p.path, name))

    def f8_delete(self):
        p = self.active_panel()
        entry = p.selected_entry()
        if not entry or entry.get("up"):
            QMessageBox.information(
                self, "Удалить", "Сначала выберите файл в активной панели."
            )
            return
        if p.mode == "img":
            QMessageBox.information(
                self,
                "Удалить",
                "Удаление из образа не поддерживается движком FAT32 "
                "(создание, перезапись и mkdir).",
            )
            return
        ret = QMessageBox.warning(
            self,
            "Удалить",
            "Удалить файл?\n\n  %s" % p.join(entry["name"]),
            MB_Yes | MB_No,
        )
        if ret != MB_Yes:
            return
        try:
            os.remove(p.join(entry["name"]))
        except OSError as e:
            QMessageBox.critical(self, "Удаление не удалось", str(e))
            return
        p.refresh()
        self.log_msg("Удалён %s" % entry["name"])

    def f9_menu(self):
        mb = self.menuBar()
        mb.setVisible(not mb.isVisible())

    def closeEvent(self, e):
        self._close_writer()
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
