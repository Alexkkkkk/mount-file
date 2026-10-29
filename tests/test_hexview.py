# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Behavior tests for the interactive hex editor."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

pytest.importorskip("mountfile.qt_compat", exc_type=ImportError)

from mountfile.hexview import HexView
from mountfile.qt_compat import QT6, CtrlModifier, QApplication, Qt, QtCore, QtGui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_edit_insert_delete_undo_redo(app):
    view = HexView()
    view.setData(b"abcdef")
    view.goto(3)
    view.insertBytes(b"XYZ")
    assert view.data() == b"abcXYZdef"
    assert view.isModified()
    view.undo()
    assert view.data() == b"abcdef"
    view.redo()
    assert view.data() == b"abcXYZdef"
    view.goto(3)
    view.deleteSelection()
    assert view.data() == b"abcYZdef"


def test_find_selection_and_reset(app):
    view = HexView()
    view.setData(b"header\x00payload\x00payload")
    assert view.find(b"payload") == 7
    assert view.selectedRange() == (7, 14)
    assert view.goto(0)
    assert view.selectedRange() == (0, 0)
    view.setData(b"new")
    assert not view.isModified()
    assert view.find(b"missing") == -1


class _Point:
    def __init__(self, x, y=0):
        self._x = x
        self._y = y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _KeyEvent:
    def __init__(self, key, modifiers=0, text=""):
        self._key = key
        self._modifiers = modifiers
        self._text = text

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def text(self):
        return self._text


def test_hit_test_distinguishes_hex_ascii_and_nibbles(app):
    view = HexView()
    view.setData(b"\x00" * 16)
    _, x_hex, x_ascii = view._columns()
    c = view._char_w

    assert view._hit_test(_Point(x_hex)) == (0, "hex", 0)
    assert view._hit_test(_Point(x_hex + c)) == (0, "hex", 1)
    assert view._hit_test(_Point(x_ascii)) == (0, "ascii", 0)


def test_mouse_click_preserves_low_hex_nibble(app):
    view = HexView()
    view.setData(b"\xAB")
    _, x_hex, _ = view._columns()
    point = QtCore.QPointF(x_hex + view._char_w, view._line_h // 2)
    event_type = (
        QtCore.QEvent.Type.MouseButtonPress if QT6 else QtCore.QEvent.MouseButtonPress
    )
    left_button = Qt.MouseButton.LeftButton if QT6 else Qt.LeftButton
    no_modifier = Qt.KeyboardModifier.NoModifier if QT6 else Qt.NoModifier
    event = QtGui.QMouseEvent(event_type, point, left_button, left_button, no_modifier)

    view.mousePressEvent(event)
    assert view._hex_nibble == 1
    view.keyPressEvent(_KeyEvent(ord("4"), text="4"))

    assert view.data() == b"\xA4"


def test_ascii_and_hex_modes_edit_bytes(app):
    view = HexView()
    view.setData(b"\x00\x00")
    view._edit_mode = "ascii"
    view.keyPressEvent(_KeyEvent(ord("A"), text="A"))
    assert view.data() == b"A\x00"

    view._edit_mode = "hex"
    view.goto(1)
    view.keyPressEvent(_KeyEvent(ord("B"), text="B"))
    view.keyPressEvent(_KeyEvent(ord("C"), text="C"))
    assert view.data() == b"A\xBC"


def test_control_shortcuts_use_key_codes_and_cut_requires_selection(app):
    view = HexView()
    view.setData(b"A")
    view._edit_mode = "ascii"
    view.goto(0)
    view.keyPressEvent(_KeyEvent(ord("B"), text="B"))
    assert view.data() == b"B"

    # Qt commonly reports Ctrl+Z as a control character rather than "z".
    view.keyPressEvent(_KeyEvent(ord("Z"), CtrlModifier, "\x1a"))
    assert view.data() == b"A"
    view.keyPressEvent(_KeyEvent(ord("Y"), CtrlModifier, "\x19"))
    assert view.data() == b"B"

    view.keyPressEvent(_KeyEvent(ord("X"), CtrlModifier, "\x18"))
    assert view.data() == b"B"
