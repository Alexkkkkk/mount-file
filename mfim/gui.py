"""PyQt6 front-end: tree view + dir table + hex viewer + opt-in FAT writing.

The GUI holds *no* filesystem logic of its own — everything comes from
``mfim.partition`` / ``mfim.vfs`` / ``mfim.tree_model``, which is why the same
features are testable headlessly through ``mfim.cli``.

Qt specifics worth knowing before reading the code:

* ``QFileSystemModel`` is unusable here (it walks the host filesystem), so
  ``ImageTreeQtModel`` wraps the Qt-free :class:`mfim.tree_model.ImageTreeModelData`.
* Children are materialised through ``hasChildren()`` + ``rowCount()``, i.e. the
  tree is lazy: opening a 200k-entry directory does not read anything until the
  node is expanded.
* Write actions stay disabled unless the selected partition really is FAT and
  ``mtools`` is present **and** the user confirms a warning; a ``.bak`` copy of
  the image is made before the first write.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional

_QT_IMPORT_ERROR = None
try:  # pragma: no cover
    from PyQt6.QtCore import QAbstractItemModel, QAbstractTableModel, QModelIndex, Qt
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QFileDialog,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QSplitter,
        QStyle,
        QTabWidget,
        QTableView,
        QToolBar,
        QTreeView,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover
    # Never raise at import time. This module must stay importable where Qt is
    # absent (CLI, headless CI, `python -m mfim`, doctest sweeps); the error is
    # re-raised only when the GUI is actually launched.
    _QT_IMPORT_ERROR = exc

    def _qt_hint() -> str:
        return (
            "PyQt6 is required for the GUI: pip install PyQt6\n"
            f"(import error: {_QT_IMPORT_ERROR})\n"
            "Use the headless interface instead: python -m mfim.cli --help"
        )

    class _MissingQtMeta(type):
        def __getattr__(cls, name):
            raise ImportError(_qt_hint())

    class _MissingQt(metaclass=_MissingQtMeta):
        def __init__(self, *args, **kwargs):
            raise ImportError(_qt_hint())

    for _name in ("""QAbstractItemModel QAbstractTableModel QModelIndex Qt QAction
QApplication QComboBox QFileDialog QHBoxLayout QInputDialog QLabel QLineEdit
QMainWindow QMessageBox QPlainTextEdit QPushButton QSpinBox QSplitter QStyle
QTabWidget QTableView QToolBar QTreeView QVBoxLayout QWidget""".split()):
        globals()[_name] = _MissingQt
    del _name

from .partition import parse_partition_table, scan_filesystems
from .tree_model import FileNode, ImageTreeModelData
from .util import human_size
from .vfs import ImageVFS, VfsError


# --------------------------------------------------------------------------- #
# Qt models
# --------------------------------------------------------------------------- #
class ImageTreeQtModel(QAbstractItemModel):
    """Lazy tree over :class:`ImageTreeModelData`."""

    def __init__(self, data: ImageTreeModelData, parent=None):
        super().__init__(parent)
        self.data_layer = data

    # -- structure ---------------------------------------------------------- #
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = parent.internalPointer() if parent.isValid() else self.data_layer.root
        children = self.data_layer.children(node)
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index):  # noqa: A003 - Qt naming
        if not index.isValid():
            return QModelIndex()
        node: FileNode = index.internalPointer()
        parent_node = node.parent
        if parent_node is None or parent_node is self.data_layer.root:
            return QModelIndex()
        return self.createIndex(self.data_layer.row_of(parent_node), 0, parent_node)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self.data_layer.root
        if not node.is_dir:
            return 0
        return len(self.data_layer.children(node))

    def columnCount(self, parent=QModelIndex()):
        return len(self.data_layer.columns)

    def hasChildren(self, parent=QModelIndex()):
        if parent.column() > 0:
            return False
        node = parent.internalPointer() if parent.isValid() else self.data_layer.root
        return bool(node.is_dir)

    # -- payload ------------------------------------------------------------ #
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: FileNode = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.data_layer.cell(node, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"{node.path}\n{node.type_name}"
                    + (f"\nerror: {node.error}" if node.error else ""))
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            style = QApplication.style()
            pixmap = (QStyle.StandardPixmap.SP_DirIcon if node.is_dir
                      else QStyle.StandardPixmap.SP_FileIcon)
            return style.standardIcon(pixmap)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() >= 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.data_layer.columns[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def node_of(self, index) -> Optional[FileNode]:
        return index.internalPointer() if index and index.isValid() else None


class DirTableModel(QAbstractTableModel):
    """Flat table with the contents of one directory."""

    def __init__(self, rows: Optional[List[FileNode]] = None, parent=None):
        super().__init__(parent)
        self.rows: List[FileNode] = rows or []
        self.columns = ("Имя", "Тип", "Размер", "Inode", "Изменён")

    def set_rows(self, rows: List[FileNode]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        node = self.rows[index.row()]
        col = index.column()
        if col == 0:
            return node.name
        if col == 1:
            return "Каталог" if node.is_dir else (node.type_name or "Файл")
        if col == 2:
            return "-" if node.is_dir else human_size(node.size)
        if col == 3:
            return str(node.inode) if node.inode else "-"
        if col == 4:
            from .util import human_time

            return human_time(node.mtime)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, image_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Mount File — файловый менеджер поверх образов")
        self.resize(1200, 760)

        self.image_path: Optional[str] = None
        self.table = None
        self.vfs: Optional[ImageVFS] = None
        self.tree_data: Optional[ImageTreeModelData] = None
        self.tree_model: Optional[ImageTreeQtModel] = None
        self.backend = None            # FatWriteBackend when writing is allowed
        self._backup_done = False

        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage("Откройте образ: Файл → Открыть образ…")

        if image_path:
            self.open_image(image_path)

    # -- layout ------------------------------------------------------------- #
    def _build_toolbar(self) -> None:
        bar = QToolBar("Основные")
        self.addToolBar(bar)

        act_open = QAction("Открыть образ…", self)
        act_open.triggered.connect(self.choose_image)
        bar.addAction(act_open)

        act_extract = QAction("Извлечь файл…", self)
        act_extract.triggered.connect(self.extract_selected)
        bar.addAction(act_extract)

        bar.addSeparator()
        bar.addWidget(QLabel(" Раздел: "))
        self.part_combo = QComboBox()
        self.part_combo.setMinimumWidth(320)
        self.part_combo.currentIndexChanged.connect(self._partition_changed)
        bar.addWidget(self.part_combo)

        bar.addSeparator()
        self.write_button = QPushButton("Запись в FAT")
        self.write_button.setEnabled(False)
        self.write_button.setToolTip(
            "Доступно только для FAT-раздела и при установленных mtools"
        )
        self.write_button.clicked.connect(self.open_write_panel)
        bar.addWidget(self.write_button)

    def _build_central(self) -> None:
        self.tree_view = QTreeView()
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.doubleClicked.connect(self._tree_activated)

        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._tree_context_menu)

        self.dir_model = DirTableModel()
        self.dir_view = QTableView()
        self.dir_view.setModel(self.dir_model)
        self.dir_view.horizontalHeader().setStretchLastSection(True)

        self.hex_view = QPlainTextEdit()
        self.hex_view.setReadOnly(True)
        font = self.hex_view.font()
        font.setFamily("Consolas")
        font.setStyleHint(font.StyleHint.Monospace)
        font.setPointSize(max(font.pointSize(), 10))
        self.hex_view.setFont(font)
        self.hex_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        hex_panel = QWidget()
        hv = QVBoxLayout(hex_panel)
        row = QHBoxLayout()
        row.addWidget(QLabel("Смещение:"))
        self.hex_offset = QSpinBox()
        self.hex_offset.setRange(0, 2**31 - 1)
        self.hex_offset.setSingleStep(256)
        row.addWidget(self.hex_offset)
        row.addWidget(QLabel("Строк:"))
        self.hex_lines = QSpinBox()
        self.hex_lines.setRange(1, 4096)
        self.hex_lines.setValue(32)
        row.addWidget(self.hex_lines)
        btn_load = QPushButton("Обновить")
        btn_load.clicked.connect(self.refresh_hex)
        row.addWidget(btn_load)
        self.hex_source = QLineEdit()
        self.hex_source.setReadOnly(True)
        self.hex_source.setPlaceholderText("файл внутри образа не выбран")
        row.addWidget(self.hex_source, 1)
        hv.addLayout(row)
        hv.addWidget(self.hex_view, 1)

        tabs = QTabWidget()
        tabs.addTab(hex_panel, "HEX")
        self.info_view = QPlainTextEdit()
        self.info_view.setReadOnly(True)
        tabs.addTab(self.info_view, "Разделы")

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.dir_view)
        right.addWidget(tabs)
        right.setSizes([260, 500])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree_view)
        splitter.addWidget(right)
        splitter.setSizes([420, 780])
        self.setCentralWidget(splitter)

        self.tree_view.clicked.connect(self._tree_clicked)

    # -- opening ------------------------------------------------------------ #
    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть образ", "", "Образы (*.img *.bin *.dd *.raw *.iso);;Все файлы (*)"
        )
        if path:
            self.open_image(path)

    def open_image(self, path: str) -> None:
        try:
            self.table = scan_filesystems(path)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось разобрать образ:\n{exc}")
            return

        self.image_path = path
        self.setWindowTitle(f"Mount File — {os.path.basename(path)} "
                            f"({human_size(os.path.getsize(path))})")
        self.info_view.setPlainText(self.table.describe())
        self._close_backend()
        self._backup_done = False

        self.part_combo.blockSignals(True)
        self.part_combo.clear()
        if self.table.partitions:
            for p in self.table.partitions:
                self.part_combo.addItem(
                    f"#{p.index} {p.type_name or p.name} — {human_size(p.size)} "
                    f"[{p.fs_name or '?'}] @ 0x{p.offset:x}",
                    p.offset,
                )
        else:
            self.part_combo.addItem("образ без таблицы разделов — смещение 0", 0)
        self.part_combo.blockSignals(False)
        self._partition_changed(0)

    def _partition_changed(self, _index: int) -> None:
        if not self.image_path:
            return
        offset = int(self.part_combo.currentData() or 0)
        try:
            self.vfs = ImageVFS(self.image_path, offset=offset)
        except VfsError as exc:
            self.vfs = None
            self.tree_data = None
            self.tree_view.setModel(None)
            self.dir_model.set_rows([])
            self.hex_view.setPlainText("")
            self.statusBar().showMessage(f"ФС недоступна на этом смещении: {exc}")
            self.write_button.setEnabled(False)
            return

        self.tree_data = ImageTreeModelData(self.vfs, name=os.path.basename(self.image_path))
        self.tree_model = ImageTreeQtModel(self.tree_data, self)
        self.tree_view.setModel(self.tree_model)
        self.tree_view.expandToDepth(0)
        self.statusBar().showMessage(self.vfs.describe())
        self._update_write_availability(offset)

    def _update_write_availability(self, offset: int) -> None:
        enabled = False
        if self.table is not None:
            part = self.table.containing(offset) if offset else (
                self.table.partitions[0] if self.table.partitions else None
            )
            fs = (part.fs_name if part else "")
            from .fatwrite import mtools_available

            enabled = fs.startswith("fat") and mtools_available()
        self.write_button.setEnabled(enabled)

    # -- navigation --------------------------------------------------------- #
    def _tree_clicked(self, index) -> None:
        node = self.tree_model.node_of(index) if self.tree_model else None
        if node is None:
            return
        if node.is_dir:
            self.dir_model.set_rows(self.tree_data.children(node))
        else:
            self.dir_model.set_rows([])
            self.show_hex_for(node)

    def _tree_activated(self, index) -> None:
        node = self.tree_model.node_of(index) if self.tree_model else None
        if node is not None and not node.is_dir:
            self.show_hex_for(node)

    def _tree_context_menu(self, pos) -> None:
        index = self.tree_view.indexAt(pos)
        node = self.tree_model.node_of(index) if self.tree_model else None
        if node is None or node.is_dir:
            return
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        act = menu.addAction("Извлечь…")
        act.triggered.connect(lambda: self.extract_node(node))
        menu.exec(self.tree_view.viewport().mapToGlobal(pos))

    # -- hex ---------------------------------------------------------------- #
    def show_hex_for(self, node: FileNode) -> None:
        self.hex_source.setText(node.path)
        self.hex_offset.setRange(0, max(node.size - 1, 0))
        self.hex_offset.setValue(0)
        self.refresh_hex(node)

    def refresh_hex(self, node: Optional[FileNode] = None) -> None:
        if self.vfs is None:
            return
        if node is None:
            path = self.hex_source.text()
            if not path:
                return
        else:
            path = node.path
        try:
            self.hex_view.setPlainText(self.vfs.hexdump(
                path, offset=self.hex_offset.value(), length=self.hex_lines.value() * 16
            ))
        except Exception as exc:
            self.hex_view.setPlainText(f"<ошибка чтения: {exc}>")

    # -- extraction --------------------------------------------------------- #
    def extract_selected(self) -> None:
        node = None
        if self.dir_view.selectionModel() and self.dir_view.selectionModel().hasSelection():
            rows = self.dir_view.selectionModel().selectedRows()
            if rows:
                node = self.dir_model.rows[rows[0].row()]
        if node is None:
            index = self.tree_view.currentIndex()
            node = self.tree_model.node_of(index) if self.tree_model else None
        if node is None or node.is_dir:
            QMessageBox.information(self, "Извлечение", "Выберите файл в дереве или таблице.")
            return
        self.extract_node(node)

    def extract_node(self, node: FileNode) -> None:
        if self.vfs is None:
            return
        target, _ = QFileDialog.getSaveFileName(self, "Сохранить как", node.name)
        if not target:
            return
        try:
            result = self.vfs.extract(node.path, target, verify=True)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка извлечения", str(exc))
            return
        ok = "✓ размер совпал" if result["match"] else "⚠ размер не совпал"
        QMessageBox.information(
            self, "Готово",
            f"{target}\n{result['bytes']} байт, ожидалось {result['expected']} — {ok}\n"
            f"sha256: {result.get('sha256', '')[:32]}…",
        )

    # -- writing (opt-in) --------------------------------------------------- #
    def _ensure_backup(self) -> bool:
        if self._backup_done or self.image_path is None:
            return True
        answer = QMessageBox.warning(
            self, "Запись в образ",
            "Запись изменяет файл образа на диске.\n\n"
            "Перед первой операцией будет создана полная резервная копия "
            f"«{os.path.basename(self.image_path)}.bak».\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        shutil.copyfile(self.image_path, self.image_path + ".bak")
        self._backup_done = True
        return True

    def _close_backend(self) -> None:
        if self.backend is not None:
            try:
                self.backend.close()
            finally:
                self.backend = None

    def open_write_panel(self) -> None:
        if not self._ensure_backup():
            return
        from .fatwrite import FatWriteBackend, FatWriteError

        offset = int(self.part_combo.currentData() or 0)
        try:
            self._close_backend()
            self.backend = FatWriteBackend(self.image_path, offset=offset)
        except FatWriteError as exc:
            QMessageBox.critical(self, "FAT-запись недоступна", str(exc))
            return
        self.statusBar().showMessage(self.backend.describe())

        menu_text, ok = QInputDialog.getItem(
            self, "Запись в FAT-раздел",
            "Операция:", ["Добавить файл", "Создать каталог", "Удалить", "Показать листинг"],
            0, False,
        )
        if not ok:
            return
        try:
            if menu_text == "Добавить файл":
                local, _ = QFileDialog.getOpenFileName(self, "Файл для копирования в образ")
                if not local:
                    return
                self.backend.copy_in(local, "/" + os.path.basename(local))
                self.statusBar().showMessage(f"скопировано: {local} → /{os.path.basename(local)}")
            elif menu_text == "Создать каталог":
                name, ok2 = QInputDialog.getText(self, "Новый каталог", "Путь внутри образа:")
                if ok2 and name:
                    self.backend.mkdir(name)
            elif menu_text == "Удалить":
                name, ok2 = QInputDialog.getText(self, "Удалить", "Путь внутри образа:")
                if ok2 and name and QMessageBox.question(
                    self, "Подтверждение", f"Удалить {name}?"
                ) == QMessageBox.StandardButton.Yes:
                    self.backend.remove(name)
            else:
                self.hex_view.setPlainText(self.backend.listdir("/"))
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка записи", str(exc))
        finally:
            self._close_backend()
        self._partition_changed(self.part_combo.currentIndex())

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        self._close_backend()
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    if _QT_IMPORT_ERROR is not None:
        raise SystemExit(_qt_hint())
    argv = list(sys.argv if argv is None else argv)
    image = next((a for a in argv[1:] if not a.startswith("-")), None)
    app = QApplication(argv)
    win = MainWindow(image)
    win.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
