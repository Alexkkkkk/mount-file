"""Main window of the Mount File utility (PyQt6)."""

from __future__ import annotations

import os

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QComboBox, QDockWidget, QFileDialog, QHeaderView, QInputDialog,
    QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTreeView,
)

from .hexview import HexView
from .partition import list_partitions
from .vfs import TskImage, TskError


# ============================================================ tree model
class Node:
    __slots__ = ("name", "path", "is_dir", "size", "parent", "children",
                 "loaded")

    def __init__(self, name, path, is_dir, size=0, parent=None):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.parent = parent
        self.children = []
        self.loaded = False


class ImageModel(QAbstractItemModel):
    """Lazy-loading tree model over a TskImage provider."""

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.root = Node(os.path.basename(provider.root_name), "/", True)
        self.last_error = None

    # -- structure -----------------------------------------------------
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = parent.internalPointer() if parent.isValid() \
            else self.root
        if row < len(parent_node.children):
            return self.createIndex(row, column,
                                    parent_node.children[row])
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if node.parent is None:
            return QModelIndex()
        row = node.parent.children.index(node)
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent=QModelIndex()):
        node = parent.internalPointer() if parent.isValid() else self.root
        return len(node.children)

    def columnCount(self, parent=QModelIndex()):
        return 2

    def hasChildren(self, parent=QModelIndex()):
        node = parent.internalPointer() if parent.isValid() else self.root
        return node.is_dir

    # -- lazy loading ----------------------------------------------------
    def canFetchMore(self, parent=QModelIndex()):
        node = parent.internalPointer() if parent.isValid() else self.root
        return node.is_dir and not node.loaded

    def fetchMore(self, parent=QModelIndex()):
        node = parent.internalPointer() if parent.isValid() else self.root
        if node.loaded:
            return
        try:
            items = self.provider.list_dir(node.path)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            node.loaded = True
            return
        self.beginInsertRows(parent, 0, max(0, len(items) - 1))
        for it in items:
            p = (node.path.rstrip("/") + "/" + it["name"]) \
                if node.path != "/" else "/" + it["name"]
            node.children.append(Node(it["name"], p, it["is_dir"],
                                      it.get("size", 0), node))
        node.loaded = True
        self.endInsertRows()

    # -- data ------------------------------------------------------------
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return node.name + ("/" if node.is_dir else "")
            if index.column() == 1 and not node.is_dir:
                return self._fmt_size(node.size)
        return None

    def headerData(self, section, orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole \
                and orientation == Qt.Orientation.Horizontal:
            return ("Name", "Size")[section]
        return None

    @staticmethod
    def _fmt_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return "%.1f %s" % (n, unit) if unit != "B" \
                    else "%d B" % n
            n /= 1024
        return "%d B" % n

    # -- helpers ----------------------------------------------------------
    def node_for(self, index):
        return index.internalPointer() if index.isValid() else self.root

    def reload(self, node):
        """Drop cached children of `node` so they are re-fetched lazily."""
        if not node.loaded:
            return
        idx = self.index_for(node)
        parent_idx = self.parent(idx)
        self.beginRemoveRows(parent_idx, 0, max(0, len(node.children) - 1))
        node.children = []
        node.loaded = False
        self.endRemoveRows()

    def index_for(self, node):
        if node.parent is None:
            return QModelIndex()
        row = node.parent.children.index(node)
        return self.createIndex(row, 0, node)


# ============================================================ main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mount File — image file manager")
        self.resize(1000, 640)

        self.image_path = None
        self.provider = None       # TskImage (read)
        self.writer = None         # Fat32 (write) or None
        self.writer_fh = None
        self.model = None

        self._build_ui()
        self._set_loaded(False)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        # toolbar
        tb = self.addToolBar("Main")
        self.act_open = QAction("Open image…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_image)
        tb.addAction(self.act_open)

        tb.addWidget(QLabel("  Volume: "))
        self.cmb_parts = QComboBox()
        self.cmb_parts.setMinimumWidth(320)
        self.cmb_parts.currentIndexChanged.connect(self.volume_changed)
        tb.addWidget(self.cmb_parts)

        tb.addSeparator()
        self.btn_extract = QPushButton("Extract…")
        self.btn_extract.clicked.connect(self.extract_selected)
        tb.addWidget(self.btn_extract)
        self.btn_import = QPushButton("Import file… (FAT32)")
        self.btn_import.clicked.connect(self.import_file)
        tb.addWidget(self.btn_import)
        self.btn_mkdir = QPushButton("New folder (FAT32)")
        self.btn_mkdir.clicked.connect(self.make_dir)
        tb.addWidget(self.btn_mkdir)

        # central: tree + tabs
        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(
            QTreeView.SelectionMode.SingleSelection)
        self.tree.doubleClicked.connect(self.on_double_click)

        self.hex_view = HexView()
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        tabs = QTabWidget()
        tabs.addTab(self.hex_view, "Hex view")
        tabs.addTab(self.info, "Info")

        splitter = QSplitter()
        splitter.addWidget(self.tree)
        splitter.addWidget(tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        # log dock
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

        self.statusBar().showMessage("Open a disk image to start")

    def log_msg(self, msg):
        self.log.appendPlainText(msg)
        self.statusBar().showMessage(msg, 5000)

    def _set_loaded(self, loaded):
        for w in (self.cmb_parts, self.btn_extract, self.btn_import,
                  self.btn_mkdir):
            w.setEnabled(loaded)
        if not loaded:
            self.btn_import.setEnabled(False)
            self.btn_mkdir.setEnabled(False)

    # ------------------------------------------------------------ open
    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open disk image", "",
            "Disk images (*.img *.bin *.raw *.dump *.dd);;All files (*)")
        if not path:
            return
        self.image_path = path
        self._close_writer()
        self.cmb_parts.blockSignals(True)
        self.cmb_parts.clear()
        self.cmb_parts.addItem("Whole image (offset 0)", 0)
        try:
            parts = list_partitions(path)
            for p in parts:
                self.cmb_parts.addItem(
                    "%d  %s  %s  %.1f MB @ LBA %d" % (
                        p.index, p.name, p.type_name, p.size_mb,
                        p.start_lba),
                    p.offset)
        except Exception as e:
            self.log_msg("Partition scan failed: %s" % e)
        self.cmb_parts.blockSignals(False)
        self.cmb_parts.setCurrentIndex(0)
        self._mount_volume(0)
        self._set_loaded(True)
        self.log_msg("Opened image: %s" % path)

    def volume_changed(self, idx):
        if self.image_path:
            self._mount_volume(self.cmb_parts.itemData(idx))

    def _mount_volume(self, offset):
        try:
            self.provider = TskImage(self.image_path, offset=offset)
        except TskError as e:
            QMessageBox.warning(self, "Cannot mount volume",
                                "The Sleuth Kit could not open this volume:\n%s\n\n"
                                "Tip: select a specific partition or install pytsk3."
                                % e)
            self.provider = None
            self.tree.setModel(None)
            return
        self.model = ImageModel(self.provider)
        self.tree.setModel(self.model)
        self.tree.selectionModel().currentChanged.connect(
            self.on_selection)
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)

        # try to attach the built-in FAT32 writer
        self._close_writer()
        from .fat32 import Fat32, Fat32Error
        try:
            self.writer_fh = open(self.image_path, "r+b")
            self.writer = Fat32(self.writer_fh, offset=offset)
            self.btn_import.setEnabled(True)
            self.btn_mkdir.setEnabled(True)
            self.log_msg("Volume mounted @ offset %d — FAT32 write support "
                         "enabled" % offset)
        except (Fat32Error, OSError) as e:
            self.writer = None
            self.btn_import.setEnabled(False)
            self.btn_mkdir.setEnabled(False)
            self.log_msg("Volume mounted @ offset %d (read-only: %s)"
                         % (offset, e))

    def _close_writer(self):
        if self.writer_fh:
            try:
                self.writer_fh.close()
            except OSError:
                pass
        self.writer_fh = None
        self.writer = None

    # ------------------------------------------------------------ tree
    def on_selection(self, current, previous):
        node = self.model.node_for(current) if self.model else None
        if node is None or node.is_dir:
            self.hex_view.clear()
            self.info.setPlainText("")
            return
        try:
            data = self.provider.read_file_range(node.path, 0, 1 << 20)
            self.hex_view.setData(data)
            self.info.setPlainText(
                "Path: %s\nSize: %d bytes\nPreview: first %d bytes"
                % (node.path, node.size, len(data)))
        except Exception as e:
            self.hex_view.clear()
            self.info.setPlainText("Error: %s" % e)

    def on_double_click(self, index):
        node = self.model.node_for(index)
        if node and not node.is_dir:
            self.extract_selected()

    def _current_node(self):
        idx = self.tree.currentIndex()
        return self.model.node_for(idx) if self.model else None

    # ------------------------------------------------------------ extract
    def extract_selected(self):
        node = self._current_node()
        if node is None or node.is_dir:
            QMessageBox.information(self, "Extract",
                                    "Select a file in the tree first.")
            return
        dst, _ = QFileDialog.getSaveFileName(
            self, "Extract file as…", node.name)
        if not dst:
            return
        try:
            data = self.provider.read_file(node.path)
            with open(dst, "wb") as f:
                f.write(data)
            self.log_msg("Extracted %s (%d bytes) -> %s"
                         % (node.path, len(data), dst))
        except Exception as e:
            QMessageBox.critical(self, "Extract failed", str(e))

    # ------------------------------------------------------------ import
    def import_file(self):
        node = self._current_node()
        if node is None:
            return
        target_dir = node.path if node.is_dir else os.path.dirname(
            node.path) or "/"
        src, _ = QFileDialog.getOpenFileName(self, "Import file", "")
        if not src:
            return
        name = os.path.basename(src)
        try:
            with open(src, "rb") as f:
                data = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return
        ret = QMessageBox.warning(
            self, "Confirm write",
            "This will WRITE into the image:\n\n"
            "  %s\n  -> %s%s\n\n"
            "Make sure you have a backup of the image. Continue?"
            % (src, target_dir, name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self.writer.write_file(target_dir, name, data)
            self.log_msg("Imported %s (%d bytes) -> %s%s"
                         % (src, len(data), target_dir, name))
            self._refresh_dir(target_dir)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))

    def make_dir(self):
        node = self._current_node()
        if node is None:
            return
        target_dir = node.path if node.is_dir else os.path.dirname(
            node.path) or "/"
        name, ok = QInputDialog.getText(self, "New folder",
                                        "Folder name in %s:" % target_dir)
        if not ok or not name.strip():
            return
        try:
            self.writer.mkdir(target_dir, name.strip())
            self.log_msg("Created folder %s%s" % (target_dir, name.strip()))
            self._refresh_dir(target_dir)
        except Exception as e:
            QMessageBox.critical(self, "mkdir failed", str(e))

    def _refresh_dir(self, path):
        """Force re-read of the given image directory in the tree."""
        if not self.model:
            return
        # find the node matching path
        def walk(node, parts):
            if not parts:
                return node
            for ch in node.children:
                if ch.name == parts[0]:
                    return walk(ch, parts[1:])
            return None

        parts = [p for p in path.split("/") if p]
        node = walk(self.model.root, parts)
        if node is not None:
            self.model.reload(node)

    # ------------------------------------------------------------ close
    def closeEvent(self, e):
        self._close_writer()
        super().closeEvent(e)
