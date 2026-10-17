# MOUNTFILE_FIX — auto-patched + OLMA
# MOUNTFILE_FIX — auto-patched + OLMA
"""qt_compat.py — унифицированный слой PyQt5/PyQt6."""

from __future__ import annotations

IS_QT6 = False
QT_VERSION = "none"
_IMPORT_ERROR = None

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QAction, QIcon, QKeySequence

    IS_QT6 = True
    QT_VERSION = "PyQt6"
except Exception as e6:
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot
        from PyQt5.QtGui import QIcon, QKeySequence
        from PyQt5.QtWidgets import QAction

        IS_QT6 = False
        QT_VERSION = "PyQt5"
    except Exception as e5:
        _IMPORT_ERROR = f"PyQt6: {e6!r}; PyQt5: {e5!r}"


def is_qt_available() -> bool:
    return _IMPORT_ERROR is None


def require_qt() -> None:
    if not is_qt_available():
        raise RuntimeError(f"Qt not found. {_IMPORT_ERROR}")


__all__ = [
    "QtCore",
    "QtGui",
    "QtWidgets",
    "Qt",
    "pyqtSignal",
    "pyqtSlot",
    "QKeySequence",
    "QAction",
    "QIcon",
    "IS_QT6",
    "QT_VERSION",
    "is_qt_available",
    "require_qt",
]
