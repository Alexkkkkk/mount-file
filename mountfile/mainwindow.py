# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Main window of the Mount File utility (Qt5 / Qt6 via qt_compat)."""

from __future__ import annotations

import os

from .qt_compat import (
    QAction, QAbstractItemModel, BottomDock, DisplayRole, QComboBox,
    QDockWidget, QFileDialog, Horizontal, QInputDialog,
    QLabel, QMainWindow, MB_No, MB_Yes, QMessageBox, QModelIndex,
    QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTreeView,
    SC_Open, SingleSelection, Stretch,
)

from .extract import extract_path
from .fat32 import Fat32, Fat32Error, Fat32Provider
from .firmware import detect_volumes
from .hexview import HexView
from .vfs import TskImage, TskError


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
    """Lazy-loading tree model over a filesystem provider.

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
        parent_node = parent.internalPointer() if parent.isValid() \
            else self.root
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
            p = (node.path.rstrip("/") + "/" + it["name"]) \
                if node.path != "/" else "/" + it["name"]
            node.children.append(Node(it["name"], p, it["is_dir"],
                                      it.get("size", 0), node))
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
        self.backend = None
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
        self.btn_rename = QPushButton("Rename (FAT32)")
        self.btn_rename.clicked.connect(self.rename_selected)
        tb.addWidget(self.btn_rename)
        self.btn_delete = QPushButton("Delete (FAT32)")
        self.btn_delete.clicked.connect(self.delete_selected)
        tb.addWidget(self.btn_delete)

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

    def _set_write_enabled(self, on):
        for w in (self.btn_import, self.btn_mkdir, self.btn_rename,
                  self.btn_delete):
            w.setEnabled(bool(on))

    def _set_loaded(self, loaded):
        for w in (self.cmb_parts, self.btn_extract):
            w.setEnabled(loaded)
        if not loaded:
            self._set_write_enabled(False)

    # ------------------------------------------------------------- open
    def open_image(self, path=None):
        """Open an image; `path` may come from argv or the File menu."""
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open disk image", "",
                "Disk images (*.img *.bin *.raw *.dump *.dd);;All files (*)")
        if not path:
            return
        self.image_path = path
        self._unmount()
        self.cmb_parts.blockSignals(True)
        self.cmb_parts.clear()
        self.cmb_parts.addItem("Whole image (offset 0)", 0)
        try:
            for i, v in enumerate(detect_volumes(path), 1):
                self.cmb_parts.addItem("%d. %s" % (i, v.desc), v.offset)
        except Exception as e:
            self.log_msg("Volume scan failed: %s" % e)
        self.cmb_parts.blockSignals(False)
        # Auto-select the first real volume when the image has a partition
        # table; otherwise stay on offset 0.
        idx = 1 if self.cmb_parts.count() > 1 else 0
        self.cmb_parts.setCurrentIndex(idx)
        self._set_loaded(True)
        self._mount_volume(self.cmb_parts.itemData(idx))
        self.log_msg("Opened image: %s" % path)

    def volume_changed(self, idx):
        if self.image_path:
            self._mount_volume(self.cmb_parts.itemData(idx))

    def _mount_volume(self, offset):
        if offset is None:
            offset = 0
        self._unmount()
        # Nothing is writable until a volume is actually mounted and its FAT32
        # header parsed - a failed mount must not leave live buttons behind.
        self._set_write_enabled(False)
        try:
            self.provider = TskImage(self.image_path, offset=offset)
            self.backend = "The Sleuth Kit"
        except TskError as tsk_err:
            try:
                self.provider = Fat32Provider(self.image_path, offset=offset)
                self.backend = "built-in FAT32 engine"
            except (Fat32Error, OSError, ValueError) as fat_err:
                self.provider = None
                QMessageBox.warning(
                    self, "Cannot mount volume",
                    "No filesystem could be mounted at offset %d.\n\n"
                    "The Sleuth Kit: %s\nBuilt-in FAT32 engine: %s\n\n"
                    "Tip: pick another entry in the Volume list."
                    % (offset, tsk_err, fat_err))
                return
        self.model = ImageModel(self.provider)
        self.tree.setModel(self.model)
        self.tree.selectionModel().currentChanged.connect(self.on_selection)
        self.tree.header().setSectionResizeMode(0, Stretch)
        self.log_msg("Volume mounted @ offset %d via %s"
                     % (offset, self.backend))
        self._open_writer(offset)

    def _open_writer(self, offset):
        try:
            fh = open(self.image_path, "r+b")
            try:
                self.writer = Fat32(fh, offset)
            except Exception:
                fh.close()
                raise
        except (Fat32Error, OSError) as e:
            self.writer = None
            self.writer_fh = None
            self._set_write_enabled(False)
            self.log_msg("Volume is read-only here (%s)" % e)
            return
        self.writer_fh = fh
        self._set_write_enabled(True)
        self.log_msg("FAT32 write support enabled (offset %d)" % offset)

    def _close_writer(self):
        if self.writer_fh:
            try:
                self.writer_fh.close()
            except OSError:
                pass
        self.writer_fh = None
        self.writer = None

    def _unmount(self):
        """Release the writer and the current provider."""
        self._close_writer()
        provider, self.provider = self.provider, None
        self.backend = None
        self.model = None
        if provider is not None and hasattr(provider, "close"):
            try:
                provider.close()
            except OSError:
                pass
        self.tree.setModel(None)
        self.hex_view.clear()
        self.info.setPlainText("")

    # -------------------------------------------------------- selection
    def on_selection(self, current, previous):
        node = self.model.node_for(current) if self.model else None
        if node is None or node.is_dir or self.provider is None:
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

    # ---------------------------------------------------------- actions
    def extract_selected(self):
        node = self._current_node()
        if node is None or self.provider is None:
            QMessageBox.information(self, "Extract",
                                    "Select a file or folder in the tree first.")
            return
        if node.is_dir:
            dst = QFileDialog.getExistingDirectory(
                self, "Extract folder into…")
            if not dst:
                return
            try:
                count = extract_path(self.provider, node.path, dst)
                self.log_msg("Extracted %d file(s) from %s -> %s"
                             % (count, node.path, dst))
            except Exception as e:
                QMessageBox.critical(self, "Extract failed", str(e))
            return
        dst, _ = QFileDialog.getSaveFileName(self, "Extract file as…",
                                             node.name)
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

    def import_file(self):
        node = self._current_node()
        if node is None or self.writer is None:
            return
        target_dir = node.path if node.is_dir else \
            (os.path.dirname(node.path) or "/")
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
            MB_Yes | MB_No)
        if ret != MB_Yes:
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
        if node is None or self.writer is None:
            return
        target_dir = node.path if node.is_dir else \
            (os.path.dirname(node.path) or "/")
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

    def rename_selected(self):
        node = self._current_node()
        if node is None or node.path == "/" or self.writer is None:
            return
        parent = os.path.dirname(node.path) or "/"
        new_name, ok = QInputDialog.getText(self, "Rename",
                                            "New name for %s:" % node.name,
                                            text=node.name)
        if not ok or not new_name.strip():
            return
        try:
            self.writer.rename(node.path, new_name.strip())
            self.log_msg("Renamed %s -> %s" % (node.path, new_name.strip()))
            self._refresh_dir(parent)
        except Exception as e:
            QMessageBox.critical(self, "Rename failed", str(e))

    def delete_selected(self):
        node = self._current_node()
        if node is None or node.path == "/" or self.writer is None:
            return
        parent = os.path.dirname(node.path) or "/"
        what = "folder" if node.is_dir else "file"
        extra = "\n(and everything inside it)" if node.is_dir else ""
        ret = QMessageBox.warning(
            self, "Confirm delete",
            "Delete this %s from the image?\n\n  %s%s\n\n"
            "This rewrites the image in place. Continue?" % (what, node.path,
                                                             extra),
            MB_Yes | MB_No)
        if ret != MB_Yes:
            return
        try:
            self.writer.delete(node.path, recursive=node.is_dir)
            self.log_msg("Deleted %s" % node.path)
            self._refresh_dir(parent)
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))

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
        self._unmount()
        super().closeEvent(e)
