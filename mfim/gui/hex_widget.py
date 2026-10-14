"""Hex viewer widget.

Deliberately not a third-party component (QHexView is not a pip-installable
dependency): a ``QPlainTextEdit`` with a fixed font and word-wrap disabled
gives a correct, dependency-free hex view for ranges up to a few megabytes.
Rendering itself is done by :mod:`mfim.hexview`, which is pure Python and
covered by tests.
"""

from __future__ import annotations

from typing import Optional

try:  # pragma: no cover
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QPlainTextEdit
except ImportError:  # pragma: no cover
    from PySide6.QtCore import Qt  # type: ignore
    from PySide6.QtGui import QFont  # type: ignore
    from PySide6.QtWidgets import QPlainTextEdit  # type: ignore

from ..hexview import hexdump

MAX_RENDER = 4 << 20  # 4 MiB is plenty for a viewer; larger ranges get truncated


class HexViewWidget(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setPointSize(10)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setReadOnly(True)
        self.setPlaceholderText("Выберите файл или укажите смещение в образе")

    # -- public API --------------------------------------------------------- #
    def show_bytes(self, data: bytes, base: int = 0, caption: str = "") -> str:
        """Render a byte buffer. Returns the text actually displayed."""
        note = ""
        if len(data) > MAX_RENDER:
            data = data[:MAX_RENDER]
            note = f"\n# показаны первые {MAX_RENDER} байт из большего диапазона"
        text = hexdump(data, base=base)
        if caption:
            text = f"# {caption}\n" + text
        if note:
            text += note
        self.setPlainText(text)
        return text

    def show_raw_image(self, image_path: str, offset: int, length: int) -> str:
        with open(image_path, "rb") as fp:
            fp.seek(offset)
            data = fp.read(length)
        return self.show_bytes(data, base=offset, caption=f"{image_path} @ 0x{offset:x}")

    def show_vfs_file(self, vfs, path: str, offset: int = 0, length: int = 4096) -> str:
        data = vfs.read_range(path, offset, length)
        return self.show_bytes(
            data, base=offset, caption=f"{path} [{offset}:{offset + len(data)}] внутри образа"
        )

    def save_to_file(self, path: str) -> Optional[str]:
        text = self.toPlainText()
        if not text:
            return None
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(text + "\n")
        return path
