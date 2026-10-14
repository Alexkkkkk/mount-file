"""Qt-independent lazy tree data layer.

Why this exists: ``QFileSystemModel`` cannot be used here at all — it walks the
*host* filesystem. The GUI therefore needs its own model. To keep that model
testable without a display (CI, headless sandboxes), all the logic lives here
as plain Python and ``gui.py`` only wraps it in ``QAbstractItemModel``.

Children are loaded on first access (``children()``), which is what makes the
tree usable on images with hundreds of thousands of dirents.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

from .util import human_size, human_time, normalize_path


@dataclasses.dataclass
class FileNode:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    inode: int = 0
    type_name: str = "unknown"
    mtime: Optional[int] = None
    parent: Optional["FileNode"] = None
    _children: Optional[List["FileNode"]] = None
    error: Optional[str] = None

    @property
    def loaded(self) -> bool:
        return self._children is not None

    def child_count(self) -> int:
        return len(self._children or ())

    def invalidate(self) -> None:
        self._children = None


class ImageTreeModelData:
    """Data layer used by the Qt model and by the CLI ``tree`` command."""

    columns = ("Name", "Type", "Size", "Inode", "Modified")

    def __init__(self, vfs, name: str = "/"):
        self.vfs = vfs
        self._root = FileNode(name=name, path="/", is_dir=True)

    # -- tree --------------------------------------------------------------- #
    @property
    def root(self) -> FileNode:
        return self._root

    def children(self, node: FileNode) -> List[FileNode]:
        if not node.is_dir:
            return []
        if node._children is None:
            try:
                entries = self.vfs.list_dir(node.path)
            except Exception as exc:  # keep the tree alive on damaged dirs
                node.error = str(exc)
                node._children = []
            else:
                node._children = [
                    FileNode(
                        name=e.name,
                        path=e.path,
                        is_dir=e.is_dir,
                        size=e.size,
                        inode=e.inode,
                        type_name=e.type_name,
                        mtime=e.mtime,
                        parent=node,
                    )
                    for e in entries
                ]
        return node._children

    def find(self, path: str) -> Optional[FileNode]:
        """Resolve a filesystem path to a node, loading parents on the way."""
        want = normalize_path(path)
        if want == "/":
            return self._root
        node = self._root
        for segment in [s for s in want.split("/") if s]:
            match = None
            for child in self.children(node):
                if child.name == segment or child.name.lower() == segment.lower():
                    match = child
                    break
            if match is None:
                return None
            node = match
        return node

    def flatten(self, node: Optional[FileNode] = None, limit: int = 20000) -> List[FileNode]:
        node = node or self._root
        out: List[FileNode] = []
        stack = [node]
        while stack and len(out) < limit:
            current = stack.pop()
            for child in self.children(current):
                out.append(child)
                if child.is_dir:
                    stack.append(child)
                if len(out) >= limit:
                    break
        return out

    # -- table data --------------------------------------------------------- #
    def cell(self, node: FileNode, column: int) -> str:
        if column == 0:
            return node.name
        if column == 1:
            return "Каталог" if node.is_dir else (node.type_name or "Файл")
        if column == 2:
            return "-" if node.is_dir else human_size(node.size)
        if column == 3:
            return str(node.inode) if node.inode else "-"
        if column == 4:
            return human_time(node.mtime)
        return ""

    def row_of(self, node: FileNode) -> int:
        if node.parent is None:
            return 0
        try:
            return self.children(node.parent).index(node)
        except ValueError:
            return 0
