"""Read-only hex viewer widget for PyQt6."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QWidget


class HexView(QAbstractScrollArea):
    """Minimal read-only hex viewer (address / hex / ASCII columns)."""

    BYTES_PER_ROW = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Courier New", 10)
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(font)
        fm = QFontMetrics(font)
        self._char_w = max(fm.horizontalAdvance("0"), 1)
        self._line_h = fm.height() + 4

        self._data = b""
        self._base = 0  # address shown for the first byte

        self._c_addr = QColor("#888888")
        self._c_hex = QColor("#222222")
        self._c_ascii = QColor("#005a9e")
        self._c_sel = QColor(255, 200, 80, 120)

        self._sel_start = -1
        self._sel_end = -1

        self.setMinimumSize(self._row_width_px() + 40, self._line_h * 8 + 24)
        self._update_scroll()

    # ------------------------------------------------------------ API
    def setData(self, data: bytes, base: int = 0):
        self._data = bytes(data)
        self._base = base
        self._sel_start = self._sel_end = -1
        self._update_scroll()
        self.viewport().update()

    def clear(self):
        self.setData(b"", 0)

    def _row_width_px(self):
        c = self._char_w
        hex_part = self.BYTES_PER_ROW * 3 * c
        return 10 * c + 12 + hex_part + 12 + self.BYTES_PER_ROW * c

    def _total_rows(self):
        if not self._data:
            return 0
        return (len(self._data) + self.BYTES_PER_ROW - 1) // self.BYTES_PER_ROW

    def _update_scroll(self):
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, self._total_rows() - 1))
        sb.setSingleStep(1)
        sb.setPageStep(max(1, self.viewport().height() // self._line_h - 1))

    def resizeEvent(self, e):
        self._update_scroll()
        super().resizeEvent(e)

    # ---------------------------------------------------------- painting
    def paintEvent(self, e):
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        top = self.verticalScrollBar().value()
        rows_visible = self.viewport().height() // self._line_h + 1
        c = self._char_w
        x_addr = 4
        x_hex = x_addr + 10 * c + 12
        x_ascii = x_hex + self.BYTES_PER_ROW * 3 * c + 12

        painter.fillRect(self.viewport().rect(), QColor("#ffffff"))
        if not self._data:
            painter.setPen(self._c_addr)
            painter.drawText(10, self._line_h, "No data")
            painter.end()
            return

        for vr in range(rows_visible):
            r = top + vr
            row_off = r * self.BYTES_PER_ROW
            if row_off >= len(self._data):
                break
            y = (vr + 1) * self._line_h - 6
            row = self._data[row_off: row_off + self.BYTES_PER_ROW]

            painter.setPen(self._c_addr)
            painter.drawText(x_addr, y, "%08X" % (self._base + row_off))

            painter.setPen(self._c_hex)
            for i, b in enumerate(row):
                abs_off = row_off + i
                if self._sel_start <= abs_off < self._sel_end:
                    painter.fillRect(x_hex + i * 3 * c, y - self._line_h + 6,
                                     2 * c + 2, self._line_h - 2,
                                     self._c_sel)
                painter.drawText(x_hex + i * 3 * c, y, "%02X" % b)

            painter.setPen(self._c_ascii)
            ascii_chars = "".join(chr(b) if 32 <= b < 127 else "."
                                  for b in row)
            painter.drawText(x_ascii, y, ascii_chars)

        painter.setPen(QPen(QColor("#dddddd")))
        painter.drawLine(x_hex - 8, 0, x_hex - 8,
                         self.viewport().height())
        painter.drawLine(x_ascii - 8, 0, x_ascii - 8,
                         self.viewport().height())
        painter.end()

    # -------------------------------------------------------- selection
    def _pos_from_point(self, pos):
        c = self._char_w
        top = self.verticalScrollBar().value()
        x = pos.x()
        y = pos.y()
        row = top + y // self._line_h
        x_hex = 4 + 10 * c + 12
        x_ascii = x_hex + self.BYTES_PER_ROW * 3 * c + 12
        col = 0
        if x >= x_ascii:
            col = min(self.BYTES_PER_ROW - 1, (x - x_ascii) // c)
        elif x >= x_hex:
            col = min(self.BYTES_PER_ROW - 1, (x - x_hex) // (3 * c))
        else:
            return None
        off = row * self.BYTES_PER_ROW + col
        return off if off < len(self._data) else None

    def mousePressEvent(self, e):
        off = self._pos_from_point(e.position())
        if off is not None:
            self._sel_start = off
            self._sel_end = off + 1
            self.viewport().update()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton \
                and self._sel_start >= 0:
            off = self._pos_from_point(e.position())
            if off is not None:
                self._sel_end = off + 1
                self.viewport().update()
        super().mouseMoveEvent(e)

    def keyPressEvent(self, e):
        step = self.BYTES_PER_ROW
        cur = self._sel_start if self._sel_start >= 0 else 0
        key = e.key()
        if key == Qt.Key.Key_Down:
            cur = min(len(self._data) - 1, cur + step)
        elif key == Qt.Key.Key_Up:
            cur = max(0, cur - step)
        elif key == Qt.Key.Key_Right:
            cur = min(len(self._data) - 1, cur + 1)
        elif key == Qt.Key.Key_Left:
            cur = max(0, cur - 1)
        elif key == Qt.Key.Key_PageDown:
            cur = min(len(self._data) - 1,
                      cur + step * max(1, self.viewport().height()
                                       // self._line_h - 1))
        elif key == Qt.Key.Key_PageUp:
            cur = max(0, cur - step * max(1, self.viewport().height()
                                          // self._line_h - 1))
        else:
            super().keyPressEvent(e)
            return
        self._sel_start = cur
        self._sel_end = cur + 1
        row = cur // step
        sb = self.verticalScrollBar()
        if row < sb.value():
            sb.setValue(row)
        elif row > sb.value() + self.viewport().height() // self._line_h - 2:
            sb.setValue(row - self.viewport().height() // self._line_h + 2)
        self.viewport().update()
