"""Shared Qt application lifecycle for the test process."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session", autouse=True)
def _shared_qapplication():
    """Keep one QApplication alive; Qt cannot safely recreate it per module."""
    try:
        from mountfile.qt_compat import QApplication
    except ImportError:
        yield None
        return

    if QApplication is None:
        yield None
        return

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
