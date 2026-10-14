"""Qt item model adapter around the Qt-independent tree data layer.

``QFileSystemModel`` is unusable for this project: it walks the *host*
filesystem, not the image. So the tree is fed by :class:`ImageTreeModelData`
and wrapped here in ``QAbstractItemModel``. All listing logic (lazy children,
error tolerance) stays in ``mfim.tree_model`` so it can be unit-tested with no
display; this module is a thin translation layer and nothing more.
"""

from __future__ import annotations

from typing import Optional

try:  # pragma: no cover
    from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt

    QT_BINDING = "PyQt6"
except ImportError:  # pragma: no cover
    try:
        from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt  # type: ignore

        QT_BINDING = "PySide6"
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Для графического интерфейса нужен PyQt6 (или PySide6): pip install PyQt6"
        ) from exc

from ..tree_model import FileNode, ImageTreeModelData


class MfimTreeModel(QAbstractItemModel):
    """Read model for one filesystem inside an image."""

    def __init__(self, data: Optional[ImageTreeModelData] = None, parent=None):
        super().__init__(parent)
        self._data = data or ImageTreeModelData(_NullVfs())
        self._headers = ImageTreeModelData.columns

    # -- data layer --------------------------------------------------------- #
    def setDataLayer(self, data: ImageTreeModelData) -> None:
        self.beginResetModel()
        self._data = data
        self.endResetModel()

    @property
    def data_layer(self) -> ImageTreeModelData:
        return self._data

    # -- QAbstractItemModel -------------------------------------------------- #
    def index(self, row: int, column: int, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = parent.internalPointer() if parent.isValid() else self._data.root
        children = self._data.children(node)
        if row < 0 or row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index):  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        node: FileNode = index.internalPointer()
        if node is None or node.parent is None or node is self._data.root:
            return QModelIndex()
        parent = node.parent
        if parent.parent is None:  # parent is the invisible root
            return QModelIndex()
        siblings = self._data.children(parent.parent)
        try:
            row = siblings.index(parent)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, parent)

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.column() > 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self._data.root
        if node is None or not node.is_dir:
            return 0
        return len(self._data.children(node))

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: FileNode = index.internalPointer()
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._data.cell(node, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            info = f"{node.path}\nтип: {node.type_name}\ninode: {node.inode}"
            if node.error:
                info += f"\nошибка чтения: {node.error}"
            return info
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headers[section] if section < len(self._headers) else None
        return section + 1

    # -- helpers for the window --------------------------------------------- #
    def node_for_index(self, index) -> Optional[FileNode]:
        return index.internalPointer() if index.isValid() else self._data.root

    def index_for_node(self, node: FileNode):
        """Build a QModelIndex for a node already present in the loaded tree."""
        if node is self._data.root or node.parent is None:
            return QModelIndex()
        siblings = self._data.children(node.parent)
        try:
            row = siblings.index(node)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, node)


class _NullVfs:
    """Placeholder so the model can exist before a file is opened."""

    def list_dir(self, path: str = "/"):  # pragma: no cover - trivial
        return []
