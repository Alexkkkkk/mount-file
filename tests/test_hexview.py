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
from mountfile.qt_compat import QApplication


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
