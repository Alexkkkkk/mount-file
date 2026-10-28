# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Interactive hex editor widget compatible with Qt5 and Qt6."""

from __future__ import annotations

from .qt_compat import (
    FONT_TYPEWRITER,
    MOUSE_LEFT,
    CtrlModifier,
    FocusStrong,
    Key_Backspace,
    Key_Delete,
    Key_Down,
    Key_Insert,
    Key_Left,
    Key_PageDown,
    Key_PageUp,
    Key_Right,
    Key_Up,
    QAbstractScrollArea,
    QApplication,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    ShiftModifier,
    event_pos,
    font_char_width,
    pyqtSignal,
)


class HexView(QAbstractScrollArea):
    """Editable address/hex/ASCII byte editor.

    The widget keeps a complete byte buffer, supports selection, clipboard
    operations, nibble and ASCII editing, insertion/deletion and undo/redo.
    ``setData`` loads a new document and ``data`` returns the current bytes.
    """

    BYTES_PER_ROW = 16
    MAX_UNDO = 200
    dataChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Courier New", 10)
        font.setStyleHint(FONT_TYPEWRITER)
        self.setFont(font)
        fm = QFontMetrics(font)
        self._char_w = max(font_char_width(fm), 1)
        self._line_h = fm.height() + 4
        self._data = bytearray()
        self._original = b""
        self._base = 0
        self._undo = []
        self._redo = []
        self._sel_start = 0
        self._sel_end = 0
        self._anchor = 0
        self._cursor = 0
        self._dragging = False
        self._hex_nibble = 0
        self._edit_mode = "hex"
        self._c_addr = QColor("#888888")
        self._c_hex = QColor("#222222")
        self._c_ascii = QColor("#005a9e")
        self._c_sel = QColor(255, 200, 80, 150)
        self._c_cursor = QColor("#2b579a")
        self.setFocusPolicy(FocusStrong)
        self.setMinimumSize(self._row_width_px() + 40, self._line_h * 8 + 24)
        self._update_scroll()

    def setData(self, data: bytes, base: int = 0):
        self._data = bytearray(data)
        self._original = bytes(data)
        self._base = int(base)
        self._undo.clear()
        self._redo.clear()
        self._cursor = 0 if self._data else -1
        self._sel_start = self._sel_end = 0
        self._anchor = 0
        self._hex_nibble = 0
        self._edit_mode = "hex"
        self._update_scroll()
        self.viewport().update()
        self.dataChanged.emit()

    def clear(self):
        self.setData(b"", 0)

    def data(self) -> bytes:
        return bytes(self._data)

    def isModified(self) -> bool:
        return self.data() != self._original

    def selectedRange(self):
        return min(self._sel_start, self._sel_end), max(self._sel_start, self._sel_end)

    def _row_width_px(self):
        c = self._char_w
        return 10 * c + 12 + self.BYTES_PER_ROW * 3 * c + 12 + self.BYTES_PER_ROW * c

    def _total_rows(self):
        return (len(self._data) + self.BYTES_PER_ROW - 1) // self.BYTES_PER_ROW

    def _update_scroll(self):
        rows = self._total_rows()
        v = self.verticalScrollBar()
        v.setRange(0, max(0, rows - 1))
        v.setSingleStep(1)
        v.setPageStep(max(1, self.viewport().height() // self._line_h - 1))
        h = self.horizontalScrollBar()
        h.setRange(0, max(0, self._row_width_px() - self.viewport().width() + 12))
        h.setPageStep(max(1, self.viewport().width()))

    def resizeEvent(self, e):
        self._update_scroll()
        super().resizeEvent(e)

    def _columns(self):
        c = self._char_w
        x_addr = 4
        x_hex = x_addr + 10 * c + 12
        return x_addr, x_hex, x_hex + self.BYTES_PER_ROW * 3 * c + 12

    def paintEvent(self, e):
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.fillRect(self.viewport().rect(), QColor("#ffffff"))
        scroll_x = self.horizontalScrollBar().value()
        painter.translate(-scroll_x, 0)
        top = self.verticalScrollBar().value()
        x_addr, x_hex, x_ascii = self._columns()
        rows_visible = self.viewport().height() // self._line_h + 1
        c = self._char_w
        if not self._data:
            painter.setPen(self._c_addr)
            painter.drawText(10, self._line_h, "No data")
            painter.end()
            return
        lo, hi = self.selectedRange()
        for vr in range(rows_visible):
            row = top + vr
            row_off = row * self.BYTES_PER_ROW
            if row_off >= len(self._data):
                break
            y = (vr + 1) * self._line_h - 6
            chunk = self._data[row_off : row_off + self.BYTES_PER_ROW]
            painter.setPen(self._c_addr)
            painter.drawText(x_addr, y, "%08X" % (self._base + row_off))
            for i, b in enumerate(chunk):
                off = row_off + i
                if lo <= off < hi:
                    painter.fillRect(
                        x_hex + i * 3 * c,
                        y - self._line_h + 6,
                        2 * c + 2,
                        self._line_h - 2,
                        self._c_sel,
                    )
                    painter.fillRect(
                        x_ascii + i * c,
                        y - self._line_h + 6,
                        c + 1,
                        self._line_h - 2,
                        self._c_sel,
                    )
            painter.setPen(self._c_hex)
            for i, b in enumerate(chunk):
                painter.drawText(x_hex + i * 3 * c, y, "%02X" % b)
            painter.setPen(self._c_ascii)
            painter.drawText(
                x_ascii, y, "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            )
        if (
            (self.hasFocus() or self.viewport().hasFocus())
            and 0 <= self._cursor < len(self._data)
        ):
            cursor_row, cursor_col = divmod(self._cursor, self.BYTES_PER_ROW)
            if top <= cursor_row < top + rows_visible:
                visible_row = cursor_row - top
                cursor_y = (visible_row + 1) * self._line_h - self._line_h
                if self._edit_mode == "hex":
                    cursor_x = x_hex + cursor_col * 3 * c + self._hex_nibble * c
                else:
                    cursor_x = x_ascii + cursor_col * c
                painter.setPen(QPen(self._c_cursor, 1))
                painter.drawRect(cursor_x - 1, cursor_y + 1, c + 1, self._line_h - 2)
        painter.setPen(QPen(QColor("#dddddd")))
        painter.drawLine(x_hex - 8, 0, x_hex - 8, self.viewport().height())
        painter.drawLine(x_ascii - 8, 0, x_ascii - 8, self.viewport().height())
        painter.end()

    def _hit_test(self, pos):
        """Return ``(offset, mode, nibble)`` for an address/hex/ASCII point."""
        c = self._char_w
        x = pos.x() + self.horizontalScrollBar().value()
        row = self.verticalScrollBar().value() + pos.y() // self._line_h
        _x_addr, x_hex, x_ascii = self._columns()
        if x >= x_ascii:
            col = (x - x_ascii) // c
            mode = "ascii"
            nibble = 0
        elif x >= x_hex:
            in_cell = x - x_hex
            col = in_cell // (3 * c)
            mode = "hex"
            nibble = 1 if in_cell % (3 * c) >= c else 0
        else:
            return None
        if not 0 <= col < self.BYTES_PER_ROW:
            return None
        off = row * self.BYTES_PER_ROW + col
        return (off, mode, nibble) if 0 <= off < len(self._data) else None

    def _pos_from_point(self, pos):
        hit = self._hit_test(pos)
        return hit[0] if hit is not None else None

    def mousePressEvent(self, e):
        hit = self._hit_test(event_pos(e))
        if hit is not None:
            off, mode, nibble = hit
            self.setFocus()
            self._edit_mode = mode
            self._hex_nibble = nibble if mode == "hex" else 0
            if e.modifiers() & CtrlModifier:
                self._sel_start, self._sel_end = 0, len(self._data)
            else:
                self._anchor = off
                self._sel_start = self._sel_end = off
                self._cursor = off
                self._hex_nibble = 0
            self._dragging = True
            self.viewport().update()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._dragging and e.buttons() & MOUSE_LEFT:
            hit = self._hit_test(event_pos(e))
            if hit is not None:
                off, mode, nibble = hit
                self._cursor = off
                self._edit_mode = mode
                self._hex_nibble = nibble if mode == "hex" else 0
                self._sel_start, self._sel_end = sorted((self._anchor, off))
                self._sel_end += 1
                self.viewport().update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        super().mouseReleaseEvent(e)

    def _snapshot(self):
        return (
            bytes(self._data),
            self._cursor,
            self._sel_start,
            self._sel_end,
            self._anchor,
            self._hex_nibble,
            self._edit_mode,
        )

    def _restore(self, state):
        self._data = bytearray(state[0])
        (
            self._cursor,
            self._sel_start,
            self._sel_end,
            self._anchor,
            self._hex_nibble,
            self._edit_mode,
        ) = state[1:]
        self._update_scroll()
        self.viewport().update()
        self.dataChanged.emit()

    def _edit(self, operation):
        self._undo.append(self._snapshot())
        if len(self._undo) > self.MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()
        operation()
        self._update_scroll()
        self.viewport().update()
        self.dataChanged.emit()

    def undo(self):
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())

    def selectAll(self):
        self._sel_start, self._sel_end = 0, len(self._data)
        self._cursor = max(0, len(self._data) - 1)
        self._anchor = 0
        self.viewport().update()

    def copy(self):
        lo, hi = self.selectedRange()
        if lo != hi:
            QApplication.clipboard().setText(bytes(self._data[lo:hi]).hex(" ").upper())

    def cut(self):
        lo, hi = self.selectedRange()
        if lo != hi:
            self.copy()
            self.deleteSelection()

    def paste(self):
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        compact = "".join(text.split())
        try:
            payload = bytes.fromhex(compact)
        except ValueError:
            payload = text.encode("utf-8")
        self.insertBytes(payload)

    def insertBytes(self, payload: bytes):
        payload = bytes(payload)
        if not payload:
            return

        def op():
            lo, hi = self.selectedRange()
            self._data[lo:hi] = payload
            self._cursor = lo + len(payload) - 1
            self._sel_start = self._sel_end = self._cursor
            self._anchor = self._cursor
            self._hex_nibble = 0

        self._edit(op)

    def deleteSelection(self):
        lo, hi = self.selectedRange()
        if lo == hi:
            if not self._data:
                return
            hi = min(len(self._data), lo + 1)

        def op():
            del self._data[lo:hi]
            self._cursor = min(lo, len(self._data) - 1)
            self._sel_start = self._sel_end = max(0, self._cursor)
            self._anchor = max(0, self._cursor)
            self._hex_nibble = 0

        self._edit(op)

    def find(self, needle: bytes, start=None):
        needle = bytes(needle)
        if not needle:
            return -1
        start = self._cursor + 1 if start is None else max(0, int(start))
        pos = bytes(self._data).find(needle, start)
        if pos < 0 and start:
            pos = bytes(self._data).find(needle, 0)
        if pos >= 0:
            self._cursor = pos
            self._sel_start, self._sel_end = pos, pos + len(needle)
            self._anchor = pos
            self._hex_nibble = 0
            self._ensure_visible(pos)
            self.viewport().update()
        return pos

    def goto(self, offset: int):
        if not self._data:
            return False
        offset = max(0, min(int(offset), len(self._data) - 1))
        self._cursor = offset
        self._sel_start = self._sel_end = offset
        self._anchor = offset
        self._hex_nibble = 0
        self._ensure_visible(offset)
        self.viewport().update()
        return True

    def _ensure_visible(self, offset):
        row = offset // self.BYTES_PER_ROW
        page = max(1, self.viewport().height() // self._line_h - 1)
        if row < self.verticalScrollBar().value():
            self.verticalScrollBar().setValue(row)
        elif row >= self.verticalScrollBar().value() + page:
            self.verticalScrollBar().setValue(row - page + 1)

    def _move(self, delta, extend=False):
        if not self._data:
            return
        self._cursor = max(0, min(len(self._data) - 1, self._cursor + delta))
        if extend:
            self._sel_start, self._sel_end = sorted((self._anchor, self._cursor))
            self._sel_end += 1
        else:
            self._sel_start = self._sel_end = self._cursor
            self._anchor = self._cursor
        self._hex_nibble = 0
        self._ensure_visible(self._cursor)
        self.viewport().update()

    def keyPressEvent(self, e):
        key = e.key()
        ctrl = bool(e.modifiers() & CtrlModifier)
        text = e.text()
        if ctrl:
            # Qt key events may provide a control character (or no text) for
            # Ctrl+key, so use the key code rather than QKeyEvent.text().
            shortcut = chr(key).lower() if 0 <= key < 128 else ""
            if shortcut == "z":
                self.undo()
                return
            if shortcut == "y":
                self.redo()
                return
            if shortcut == "c":
                self.copy()
                return
            if shortcut == "x":
                self.cut()
                return
            if shortcut == "v":
                self.paste()
                return
            if shortcut == "a":
                self.selectAll()
                return
        if key == Key_Delete or key == Key_Backspace:
            self.deleteSelection()
            return
        if key == Key_Insert:
            self.insertBytes(b"\x00")
            return
        extend = bool(e.modifiers() & ShiftModifier)
        if key == Key_Left:
            self._move(-1, extend)
            return
        if key == Key_Right:
            self._move(1, extend)
            return
        if key == Key_Up:
            self._move(-self.BYTES_PER_ROW, extend)
            return
        if key == Key_Down:
            self._move(self.BYTES_PER_ROW, extend)
            return
        if key == Key_PageUp:
            self._move(
                -self.BYTES_PER_ROW
                * max(1, self.viewport().height() // self._line_h - 1),
                extend,
            )
            return
        if key == Key_PageDown:
            self._move(
                self.BYTES_PER_ROW
                * max(1, self.viewport().height() // self._line_h - 1),
                extend,
            )
            return
        if not self._data or not text:
            super().keyPressEvent(e)
            return
        ch = text[0]
        if self._edit_mode == "hex" and ch in "0123456789abcdefABCDEF":
            value = int(ch, 16)
            pos = self._cursor
            nibble = self._hex_nibble

            def op():
                old = self._data[pos]
                self._data[pos] = (
                    ((value << 4) | (old & 0x0F))
                    if nibble == 0
                    else ((old & 0xF0) | value)
                )
                self._cursor = min(len(self._data) - 1, pos + (1 if nibble else 0))
                self._sel_start = self._sel_end = self._cursor
                self._anchor = self._cursor

            self._edit(op)
            self._hex_nibble = 1 - nibble
            self.viewport().update()
            return
        if 32 <= ord(ch) < 127:
            pos = self._cursor
            if self._edit_mode != "ascii":
                super().keyPressEvent(e)
                return

            self._edit(lambda: self._data.__setitem__(pos, ord(ch)))
            self._cursor = min(len(self._data) - 1, pos + 1)
            self._sel_start = self._sel_end = self._cursor
            self._anchor = self._cursor
            self._hex_nibble = 0
            self.viewport().update()
            return
        super().keyPressEvent(e)
