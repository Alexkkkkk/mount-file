# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Main window of the Mount File utility (Qt5 / Qt6 via qt_compat)."""

from __future__ import annotations

import os

from .hexview import HexView
from .partition import list_partitions
from .qt_compat import (BottomDock, DisplayRole, Horizontal, MB_No, MB_Yes,
                        QAbstractItemModel, QAction, QComboBox, QDockWidget,
                        QFileDialog, QInputDialog, QLabel, QMainWindow,
                        QMessageBox, QModelIndex, QPlainTextEdit, QPushButton,
                        QSplitter, QTabWidget, QTreeView, SC_Open,
                        SingleSelection, Stretch)
from .vfs import TskError, TskImage


class Node:
    __slots__ = (
        "children",
        "is_dir",
        "loaded",
        "name",
        "parent",
        "path",
        "size",
    )

    def __init__(self, name, path, is_dir, size=0, parent=None):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.parent = parent
        self.children = []
        self.loaded = False


class ImageModel(QAbstractItemModel):
    """Lazy-loading tree model over a TskImage provider.

    `self.root` is the hidden root: its children are the top-level rows of
    the view, so the parent index of those rows is the invalid root index.
    """

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.root = Node(os.path.basename(provider.root_name), "/", True)
        self.last_error = None

    # -- structure -----------------------------------------------------
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = parent.internalPointer() if parent.isValid() else self.root
        if row < len(parent_node.children):
            return self.createIndex(row, column, parent_node.children[row])
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        # The hidden root itself, and its direct children (= the top-level
        # rows of the view), have no parent index. Returning a *valid*
        # index here would make the view nest every top-level entry under a
        # phantom item, so this must stay the invalid root index.
        if node.parent is None or node.parent is self.root:
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
        node.loaded = True
        # An empty directory must not announce a row insertion: calling
        # beginInsertRows(..., 0, 0) without appending anything leaves the
        # model inconsistent and makes Qt warn about an invalid index.
        if not items:
            return
        self.beginInsertRows(parent, 0, len(items) - 1)
        for it in items:
            p = (
                (node.path.rstrip("/") + "/" + it["name"])
                if node.path != "/"
                else "/" + it["name"]
            )
            node.children.append(
                Node(it["name"], p, it["is_dir"], it.get("size", 0), node)
            )
        self.endInsertRows()

    # -- data ------------------------------------------------------------
    def data(self, index, role=DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == DisplayRole:
            if index.column() == 0:
                return node.name + ("/" if node.is_dir else "")
            if index.column() == 1 and not node.is_dir:
                return self._fmt_size(node.size)
        return None

    def headerData(self, section, orientation, role=DisplayRole):
        if role == DisplayRole and orientation == Horizontal:
            return ("Name", "Size")[section]
        return None

    @staticmethod
    def _fmt_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
            n /= 1024
        return "%d B" % n

    # -- helpers ----------------------------------------------------------
    def node_for(self, index):
        return index.internalPointer() if index.isValid() else self.root

    def index_for(self, node):
        """Index pointing AT `node` (invalid for the hidden root).

        The internal pointer must be `node` itself, not `node.parent`: the
        returned index is used as the parent index for `node`'s children
        (e.g. in reload()), so it has to address `node`.
        """
        if node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.children.index(node), 0, node)

    def reload(self, node):
        """Drop cached children of `node` so they are re-fetched lazily.

        An empty `children` list must NOT reach beginRemoveRows():
        announcing the removal of row 0 when there is nothing to remove
        leaves the model inconsistent and makes Qt complain about an
        invalid index. Reset the flag and bail out instead.
        """
        if not node.loaded:
            return
        if not node.children:
            node.loaded = False
            return
        parent_idx = self.index_for(node)
        self.beginRemoveRows(parent_idx, 0, len(node.children) - 1)
        node.children = []
        node.loaded = False
        self.endRemoveRows()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mount File — image file manager")
        self.resize(1000, 640)

        self.image_path = None
        self.provider = None
        self.writer = None
        self.writer_fh = None
        self.model = None

        self._build_ui()
        self._set_loaded(False)

    def _build_ui(self):
        tb = self.addToolBar("Main")
        self.act_open = QAction("Open image…", self)
        self.act_open.setShortcut(SC_Open)
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

        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(SingleSelection)
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

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        dock = QDockWidget("Log", self)
        dock.setWidget(self.log)
        self.addDockWidget(BottomDock, dock)

        self.statusBar().showMessage("Open a disk image to start")

    def log_msg(self, msg):
        self.log.appendPlainText(msg)
        self.statusBar().showMessage(msg, 5000)

    def _set_loaded(self, loaded):
        for w in (self.cmb_parts, self.btn_extract, self.btn_import, self.btn_mkdir):
            w.setEnabled(loaded)
        if not loaded:
            self.btn_import.setEnabled(False)
            self.btn_mkdir.setEnabled(False)

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open disk image",
            "",
            "Disk images (*.img *.bin *.raw *.dump *.dd);;All files (*)",
        )
        if not path:
            return
        self.image_path = path
        self._close_writer()
        self.cmb_parts.blockSignals(True)
        self.cmb_parts.clear()
        self.cmb_parts.addItem("Whole image (offset 0)", 0)
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
            self.log_msg("Volume scan failed: %s" % e)
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
            QMessageBox.warning(
                self,
                "Cannot mount volume",
                "The Sleuth Kit could not open this volume:\n%s\n\n"
                "Tip: select a specific partition or install pytsk3." % e,
            )
            self.provider = None
            self.tree.setModel(None)
            return
        self.model = ImageModel(self.provider)
        self.tree.setModel(self.model)
        self.tree.selectionModel().currentChanged.connect(self.on_selection)
        self.tree.header().setSectionResizeMode(0, Stretch)

        self._close_writer()
        from .fat32 import Fat32, Fat32Error

        try:
            self.writer_fh = open(self.image_path, "r+b")
            self.writer = Fat32(self.writer_fh, offset=offset)
            self.btn_import.setEnabled(True)
            self.btn_mkdir.setEnabled(True)
            self.log_msg(
                "Volume mounted @ offset %d — FAT32 write support " "enabled" % offset
            )
        except (Fat32Error, OSError) as e:
            self.writer = None
            self.btn_import.setEnabled(False)
            self.btn_mkdir.setEnabled(False)
            self.log_msg("Volume mounted @ offset %d (read-only: %s)" % (offset, e))

    def _close_writer(self):
        if self.writer_fh:
            try:
                self.writer_fh.close()
            except OSError:
                pass
        self.writer_fh = None
        self.writer = None

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
                % (node.path, node.size, len(data))
            )
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

    def extract_selected(self):
        node = self._current_node()
        if node is None or node.is_dir:
            QMessageBox.information(self, "Extract", "Select a file in the tree first.")
            return
        dst, _ = QFileDialog.getSaveFileName(self, "Extract file as…", node.name)
        if not dst:
            return
        try:
            data = self.provider.read_file(node.path)
            with open(dst, "wb") as f:
                f.write(data)
            self.log_msg("Extracted %s (%d bytes) -> %s" % (node.path, len(data), dst))
        except Exception as e:
            QMessageBox.critical(self, "Extract failed", str(e))

    def import_file(self):
        node = self._current_node()
        if node is None:
            return
        target_dir = node.path if node.is_dir else os.path.dirname(node.path) or "/"
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
            self,
            "Confirm write",
            "This will WRITE into the image:\n\n"
            "  %s\n  -> %s%s\n\n"
            "Make sure you have a backup of the image. Continue?"
            % (src, target_dir, name),
            MB_Yes | MB_No,
        )
        if ret != MB_Yes:
            return
        try:
            self.writer.write_file(target_dir, name, data)
            self.log_msg(
                "Imported %s (%d bytes) -> %s%s" % (src, len(data), target_dir, name)
            )
            self._refresh_dir(target_dir)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))

    def make_dir(self):
        node = self._current_node()
        if node is None:
            return
        target_dir = node.path if node.is_dir else os.path.dirname(node.path) or "/"
        name, ok = QInputDialog.getText(
            self, "New folder", "Folder name in %s:" % target_dir
        )
        if not ok or not name.strip():
            return
        try:
            self.writer.mkdir(target_dir, name.strip())
            self.log_msg("Created folder %s%s" % (target_dir, name.strip()))
            self._refresh_dir(target_dir)
        except Exception as e:
            QMessageBox.critical(self, "mkdir failed", str(e))

    def _refresh_dir(self, path):
        if not self.model:
            return

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

    def closeEvent(self, e):
        self._close_writer()
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
