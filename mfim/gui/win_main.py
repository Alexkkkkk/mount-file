"""Main window: partition selector + lazy file tree + hex viewer.

Deliberately plain PyQt6 — no designer files, no extra widgets, so the UI can be
read top-to-bottom and modified without tooling.
"""

from __future__ import annotations

import os
from typing import List, Optional

try:  # pragma: no cover
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import (
        QComboBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStatusBar,
        QTreeView,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover
    from PySide6.QtCore import Qt  # type: ignore
    from PySide6.QtGui import QAction  # type: ignore
    from PySide6.QtWidgets import (  # type: ignore
        QComboBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStatusBar,
        QTreeView,
        QVBoxLayout,
        QWidget,
    )

from ..partition import PartitionTable, scan_filesystems
from ..tree_model import FileNode, ImageTreeModelData
from ..util import human_size
from ..vfs import ImageVFS, VfsError
from .hex_widget import HexViewWidget
from .qt_model import MfimTreeModel


class MainWindow(QMainWindow):
    def __init__(self, image_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Mount File — файловый менеджер поверх образов")
        self.resize(1280, 800)

        self.image_path: Optional[str] = None
        self.table: Optional[PartitionTable] = None
        self.vfs: Optional[ImageVFS] = None
        self._build_ui()
        self._build_menu()

        if image_path:
            self.open_image(image_path)

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        row = QHBoxLayout()
        self.btn_open = QPushButton("Открыть образ…")
        self.btn_open.clicked.connect(self.choose_image)
        row.addWidget(self.btn_open)

        row.addWidget(QLabel("Раздел:"))
        self.cmb_parts = QComboBox()
        self.cmb_parts.setMinimumWidth(420)
        self.cmb_parts.currentIndexChanged.connect(self._partition_changed)
        row.addWidget(self.cmb_parts, 1)

        self.lbl_fs = QLabel("—")
        row.addWidget(self.lbl_fs)
        outer.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tree = QTreeView()
        self.model = MfimTreeModel()
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.selectionModel().currentChanged.connect(self._selection_changed)
        for i, width in enumerate((360, 110, 110, 90, 150)):
            self.tree.setColumnWidth(i, width)
        splitter.addWidget(self.tree)

        hex_panel = QWidget()
        hex_layout = QVBoxLayout(hex_panel)
        hex_row = QHBoxLayout()
        self.btn_extract = QPushButton("Извлечь выбранное…")
        self.btn_extract.clicked.connect(self.extract_selected)
        hex_row.addWidget(self.btn_extract)
        self.btn_raw = QPushButton("Показать сектор образа")
        self.btn_raw.clicked.connect(self.show_raw_at_selection)
        hex_row.addWidget(self.btn_raw)
        hex_row.addWidget(QLabel("offset:"))
        self.ed_offset = QLineEdit("0")
        self.ed_offset.setMaximumWidth(110)
        hex_row.addWidget(self.ed_offset)
        hex_row.addWidget(QLabel("длина:"))
        self.ed_len = QLineEdit("4096")
        self.ed_len.setMaximumWidth(90)
        hex_row.addWidget(self.ed_len)
        self.btn_hex = QPushButton("Читать")
        self.btn_hex.clicked.connect(self.refresh_hex)
        hex_row.addWidget(self.btn_hex)
        hex_row.addStretch(1)
        hex_layout.addLayout(hex_row)

        self.hex = HexViewWidget()
        hex_layout.addWidget(self.hex)
        splitter.addWidget(hex_panel)
        splitter.setSizes([480, 320])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Образ не открыт")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Файл")
        act_open = QAction("Открыть образ…", self)
        act_open.triggered.connect(self.choose_image)
        file_menu.addAction(act_open)

        act_extract = QAction("Извлечь выбранное…", self)
        act_extract.triggered.connect(self.extract_selected)
        file_menu.addAction(act_extract)

        file_menu.addSeparator()
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        tools_menu = self.menuBar().addMenu("Инструменты")
        act_sniff = QAction("Определить ФС раздела", self)
        act_sniff.triggered.connect(self._report_sniff)
        tools_menu.addAction(act_sniff)

    # -- image / partition handling ----------------------------------------- #
    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите образ диска", "",
            "Образы (*.img *.bin *.raw *.dd *.iso);;Все файлы (*)",
        )
        if path:
            self.open_image(path)

    def open_image(self, path: str) -> None:
        if not os.path.exists(path):
            QMessageBox.warning(self, "Ошибка", f"Файл не найден:\n{path}")
            return
        self.image_path = path
        try:
            self.table = scan_filesystems(path)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка разбора", str(exc))
            return

        self.cmb_parts.blockSignals(True)
        self.cmb_parts.clear()
        parts = self.table.partitions
        if not parts:
            self.cmb_parts.addItem("нет разделов — образ целиком (offset 0)", userData=0)
        else:
            for p in parts:
                label = (f"#{p.index}  offset 0x{p.offset:x}  {human_size(p.size)}  "
                         f"{p.type_name or p.name}"
                         + (f"  [{p.fs_name}]" if p.fs_name else ""))
                self.cmb_parts.addItem(label, userData=p.offset)
        self.cmb_parts.blockSignals(False)

        total = os.path.getsize(path)
        self.statusBar().showMessage(
            f"{os.path.basename(path)} — {human_size(total)}, таблица: {self.table.scheme}, "
            f"разделов: {len(parts)}"
            + (f" | внимание: {len(self.table.warnings)} предупреждений"
               if self.table.warnings else "")
        )
        if self.table.warnings:
            self.hex.show_bytes(b"", 0,
                                caption="Предупреждения разбора:\n  "
                                        + "\n  ".join(self.table.warnings))
        self._partition_changed()

    def _current_offset(self) -> int:
        data = self.cmb_parts.currentData()
        return int(data) if data is not None else 0

    def _partition_changed(self) -> None:
        offset = self._current_offset()
        self.model.setDataLayer(ImageTreeModelData(_NullVfs()))
        self.vfs = None
        self.lbl_fs.setText("—")
        if not self.image_path:
            return
        try:
            self.vfs = ImageVFS(self.image_path, offset=offset)
        except VfsError as exc:
            self.lbl_fs.setText("TSK не открыл ФС")
            self.hex.show_bytes(b"", 0, caption=str(exc))
            return
        self.model.setDataLayer(ImageTreeModelData(self.vfs))
        part = self.table.by_index(self.cmb_parts.currentIndex() + 1) if self.table else None
        self.lbl_fs.setText(f"{part.fs_name if part else '?'} | {self.vfs.describe()}")

    def _report_sniff(self) -> None:
        if not self.image_path:
            return
        from ..partition import sniff_filesystem

        lines = [f"offset 0x0 : {sniff_filesystem(self.image_path, 0)}"]
        for p in (self.table.partitions if self.table else []):
            lines.append(f"offset 0x{p.offset:<10x} (#{p.index}) : "
                         f"{sniff_filesystem(self.image_path, p.offset)}")
        QMessageBox.information(self, "Определение файловых систем", "\n".join(lines))

    # -- tree / hex --------------------------------------------------------- #
    def _current_node(self) -> Optional[FileNode]:
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return None
        return self.model.node_for_index(idx)

    def _selection_changed(self, current, _previous) -> None:
        node = self.model.node_for_index(current) if current.isValid() else None
        if node is None or self.vfs is None:
            return
        if node.is_dir:
            self.hex.show_bytes(b"", 0, caption=f"{node.path} — каталог")
            return
        self.refresh_hex()

    def refresh_hex(self) -> None:
        node = self._current_node()
        if node is None or self.vfs is None:
            return
        try:
            offset = int(self.ed_offset.text() or 0, 0)
            length = int(self.ed_len.text() or 4096, 0)
        except ValueError:
            self.statusBar().showMessage("offset/длина должны быть числами (можно 0x…)")
            return
        if node.is_dir:
            return
        if offset >= max(node.size, 1):
            self.statusBar().showMessage(
                f"{node.name}: смещение {offset} за пределами файла ({node.size} байт)")
            return
        try:
            self.hex.show_vfs_file(self.vfs, node.path, offset, length)
        except VfsError as exc:
            self.hex.show_bytes(b"", 0, caption=str(exc))

    def show_raw_at_selection(self) -> None:
        node = self._current_node()
        if node is None or self.image_path is None:
            return
        try:
            offset = int(self.ed_offset.text() or 0, 0)
            length = int(self.ed_len.text() or 4096, 0)
        except ValueError:
            return
        self.hex.show_raw_image(self.image_path, offset, length)

    def extract_selected(self) -> None:
        node = self._current_node()
        if node is None or self.vfs is None:
            QMessageBox.information(self, "Извлечение", "Выберите файл в дереве")
            return
        if node.is_dir:
            QMessageBox.information(self, "Извлечение",
                                    "Извлечение каталога в GUI пока не реализовано — "
                                    "используйте CLI: mfim extract --all")
            return
        target, _ = QFileDialog.getSaveFileName(self, "Куда сохранить", node.name)
        if not target:
            return
        try:
            res = self.vfs.extract(node.path, target, verify=True)
        except VfsError as exc:
            QMessageBox.critical(self, "Ошибка извлечения", str(exc))
            return
        self.statusBar().showMessage(
            f"{node.path} → {target}: {res['bytes']} байт, "
            f"совпадение размера: {'да' if res['match'] else 'НЕТ'}"
        )
        QMessageBox.information(
            self, "Извлечение",
            f"Файл: {node.path}\nКуда: {target}\n"
            f"Байт записано: {res['bytes']} (ожидалось {res['expected']})\n"
            f"sha256: {res.get('sha256', '-')}",
        )


class _NullVfs:
    def list_dir(self, path: str = "/"):  # pragma: no cover - trivial
        return []
