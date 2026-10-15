# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Smoke tests for the Qt layer: tree model and main window.

No display and no pytsk3 needed - a fake provider feeds the tree model, so
the tests also run on a headless CI runner (QT_QPA_PLATFORM=offscreen).
The whole module is skipped when neither PyQt6 nor PyQt5 is installed.

Run:  python -m pytest tests/test_gui_smoke.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

qt_compat = pytest.importorskip("mountfile.qt_compat",
                                reason="no Qt bindings installed")

from mountfile.qt_compat import QApplication, QModelIndex  # noqa: E402
from mountfile.mainwindow import ImageModel, MainWindow  # noqa: E402


TREE = {
    "/": [("boot", True), ("README.TXT", False), ("data.bin", False)],
    "/boot": [("kernel.img", False)],
    "/boot/empty": [],
}


class FakeProvider:
    """Minimal stand-in for the TSK provider (no sample images required)."""

    root_name = "fake.img"

    def __init__(self):
        self.calls = []

    def list_dir(self, path):
        self.calls.append(path)
        return [{"name": n, "is_dir": d, "size": 0 if d else 42}
                for n, d in TREE.get(path, [])]

    def read_file_range(self, path, offset=0, length=1 << 20):
        return b"\x00" * 16

    def read_file(self, path):
        return b"\x00" * 16


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_model_lazy_load(app):
    provider = FakeProvider()
    model = ImageModel(provider)

    assert model.rowCount() == 0
    assert model.canFetchMore(QModelIndex())
    model.fetchMore(QModelIndex())

    assert model.rowCount() == 3
    # Top-level rows must not be nested under a phantom parent item.
    top = model.index(0, 0)
    assert top.isValid()
    assert not model.parent(top).isValid()
    assert model.data(top) == "boot/"
    assert model.data(model.index(1, 0)) == "README.TXT"
    assert model.data(model.index(1, 1)) == "42 B"

    # Directories report children, files do not.
    assert model.hasChildren(top)
    assert not model.hasChildren(model.index(1, 0))

    # Children of "boot" are loaded on demand and have a valid parent.
    model.fetchMore(top)
    child = model.index(0, 0, top)
    assert model.data(child) == "kernel.img"
    assert model.parent(child) == top


def test_model_empty_dir_and_reload(app):
    model = ImageModel(FakeProvider())
    model.fetchMore(QModelIndex())
    boot = model.index(0, 0)
    model.fetchMore(model.index(2, 0))          # empty directory

    model.reload(boot)                          # must not raise
    assert model.rowCount(boot) == 0
    assert model.canFetchMore(boot)
    model.fetchMore(boot)
    assert model.rowCount(boot) == 1


def test_mainwindow_builds(app):
    win = MainWindow()
    try:
        assert not win.btn_import.isEnabled()
        assert not win.btn_mkdir.isEnabled()
        assert not win.btn_delete.isEnabled()
        win.log_msg("hello")
        assert "hello" in win.log.toPlainText()
    finally:
        win.close()


def test_mainwindow_refresh_dir(app):
    win = MainWindow()
    try:
        win.model = ImageModel(FakeProvider())
        win.provider = win.model.provider
        win.model.fetchMore(QModelIndex())
        win._refresh_dir("/")                    # must not raise
        assert win.model.rowCount() == 0
        win._refresh_dir("/boot")
        win._close_writer()
    finally:
        win.close()
