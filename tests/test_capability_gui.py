"""Behavioral checks for capability-driven GUI write controls."""

from types import SimpleNamespace

import pytest

from mountfile.capabilities import get_native_capability_status


class _Button:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


def _write_controls(profile_id, *, write_enabled):
    pytest.importorskip("PyQt6")
    from mountfile.mainwindow import MainWindow

    class Writer:
        @staticmethod
        def capability_status():
            return get_native_capability_status(profile_id, write_enabled=write_enabled)

    window = SimpleNamespace(
        writer=Writer(),
        btn_import=_Button(),
        btn_mkdir=_Button(),
        btn_rename=_Button(),
        btn_delete=_Button(),
    )
    MainWindow._set_write_enabled(window, True)
    return {
        "write-file": window.btn_import.enabled,
        "mkdir": window.btn_mkdir.enabled,
        "rename": window.btn_rename.enabled,
        "delete": window.btn_delete.enabled,
    }


def test_gui_keeps_native_ext3_write_controls_disabled():
    assert _write_controls("ext3-native-ro", write_enabled=False) == {
        "write-file": False,
        "mkdir": False,
        "rename": False,
        "delete": False,
    }


def test_gui_enables_only_operations_advertised_for_fat32_writer():
    assert _write_controls("fat32-native-rw", write_enabled=True) == {
        "write-file": True,
        "mkdir": True,
        "rename": True,
        "delete": True,
    }
