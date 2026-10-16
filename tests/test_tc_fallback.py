"""Regression: the real GUI keeps working when optional pytsk3 is absent."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip(
    "mountfile.qt_compat", exc_type=ImportError, reason="Qt not installed"
)

from test_gui_end_to_end import build_disk

from mountfile.qt_compat import QApplication
from mountfile.tc_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_tc_mounts_fat32_without_tsk_and_closes_provider(tmp_path, monkeypatch, app):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr("mountfile.vfs.pytsk3", None)
    win = MainWindow()
    try:
        assert win.load_image(build_disk(tmp_path))
        panel = win.panelL
        assert panel.provider.read_file("/HELLO.TXT") == b"hello world"
        assert panel.writer is not None
        provider = panel.provider
        win.release_volume(panel)
        assert provider.f is None
        assert panel.provider is None
        assert panel.writer is None
        assert panel.writer_fh is None
    finally:
        win.close()
