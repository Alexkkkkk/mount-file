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

qt_compat = pytest.importorskip(
    "mountfile.qt_compat", exc_type=ImportError, reason="no Qt bindings installed"
)

from mountfile.mainwindow import ImageModel, MainWindow
from mountfile.qt_compat import QApplication, MB_Yes, QMessageBox, QModelIndex

TREE = {
    "/": [("boot", True), ("empty", True), ("README.TXT", False), ("data.bin", False)],
    "/boot": [("kernel.img", False)],
    "/empty": [],
}


class FakeProvider:
    """Minimal stand-in for the TSK/FAT32 provider (no sample images needed)."""

    root_name = "fake.img"

    def __init__(self):
        self.calls = []

    def list_dir(self, path):
        self.calls.append(path)
        return [
            {"name": n, "is_dir": d, "size": 0 if d else 42}
            for n, d in TREE.get(path, [])
        ]

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

    assert model.rowCount() == 4
    # Top-level rows must not be nested under a phantom parent item.
    top = model.index(0, 0)
    assert top.isValid()
    assert not model.parent(top).isValid()
    assert model.data(top) == "boot/"
    assert model.data(model.index(1, 0)) == "empty/"
    assert model.data(model.index(2, 0)) == "README.TXT"
    assert model.data(model.index(2, 1)) == "42 B"
    assert model.data(model.index(1, 1)) is None  # dirs have no size

    # Directories report children, files do not.
    assert model.hasChildren(top)
    assert not model.hasChildren(model.index(2, 0))

    # Children of "boot" are loaded on demand and have a valid parent.
    model.fetchMore(top)
    child = model.index(0, 0, top)
    assert model.data(child) == "kernel.img"
    assert model.parent(child) == top


def test_model_empty_dir_is_marked_loaded(app):
    model = ImageModel(FakeProvider())
    model.fetchMore(QModelIndex())
    empty = model.index(1, 0)

    assert model.data(empty) == "empty/"
    assert model.canFetchMore(empty)
    model.fetchMore(empty)  # legitimate empty directory
    assert model.rowCount(empty) == 0
    assert not model.canFetchMore(empty)  # loaded, just empty


def test_model_list_error_is_reported_and_user_can_retry(app, monkeypatch):
    class RetryProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.failed = False

        def list_dir(self, path):
            self.calls.append(path)
            if not self.failed:
                self.failed = True
                raise OSError("temporary read failure")
            return [{"name": "recovered.txt", "is_dir": False, "size": 7}]

    prompts = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: prompts.append(_args[2]) or MB_Yes,
    )
    model = ImageModel(RetryProvider())
    model.fetchMore(QModelIndex())

    assert len(prompts) == 1
    assert "temporary read failure" in prompts[0]
    assert model.last_error is None
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "recovered.txt"


def test_model_reload_refetches(app):
    model = ImageModel(FakeProvider())
    model.fetchMore(QModelIndex())

    boot = model.index(0, 0)
    model.fetchMore(boot)
    assert model.rowCount(boot) == 1
    model.reload(model.node_for(boot))  # must not raise
    assert model.rowCount(boot) == 0
    assert model.canFetchMore(boot)
    model.fetchMore(boot)
    assert model.rowCount(boot) == 1

    # Reloading the top level (an empty result right after) is a no-op, not a
    # beginRemoveRows(0, -1).
    model.reload(model.root)
    assert model.rowCount() == 0
    assert model.canFetchMore(QModelIndex())
    model.fetchMore(QModelIndex())
    assert model.rowCount() == 4


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

        win._refresh_dir("/")  # must not raise
        assert win.model.rowCount() == 0
        assert win.model.canFetchMore(QModelIndex())

        win.model.fetchMore(QModelIndex())
        win._refresh_dir("/boot")
        assert win.model.rowCount(model_idx(win)) == 4
        win._close_writer()
    finally:
        win.close()


def model_idx(win):
    return QModelIndex()
