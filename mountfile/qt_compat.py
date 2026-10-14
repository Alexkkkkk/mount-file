"""Qt compatibility layer: PyQt6 (modern OS) or PyQt5 (Windows 7).

PyQt6/Qt6 requires Windows 10+, so on Windows 7 this project falls back
to PyQt5 (Qt 5.15 LTS, the last Qt branch supporting Windows 7).
All GUI code imports Qt names from here and never from PyQt* directly.
"""

from __future__ import annotations

QT6 = True
try:  # pragma: no cover - depends on environment
    from PyQt6.QtCore import (
        QAbstractItemModel, QKeySequence, QModelIndex, QSize, Qt,
    )
    from PyQt6.QtGui import (
        QAction, QColor, QFont, QFontMetrics, QPainter, QPen,
    )
    from PyQt6.QtWidgets import (
        QAbstractScrollArea, QApplication, QComboBox, QDockWidget,
        QFileDialog, QHeaderView, QInputDialog, QLabel, QMainWindow,
        QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
        QTreeView, QWidget,
    )
except ImportError:  # pragma: no cover
    QT6 = False
    from PyQt5.QtCore import (
        QAbstractItemModel, QKeySequence, QModelIndex, QSize, Qt,
    )
    from PyQt5.QtGui import (
        QColor, QFont, QFontMetrics, QPainter, QPen,
    )
    from PyQt5.QtWidgets import (
        QAbstractScrollArea, QAction, QApplication, QComboBox, QDockWidget,
        QFileDialog, QHeaderView, QInputDialog, QLabel, QMainWindow,
        QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
        QTreeView, QWidget,
    )


# ---------------------------------------------------------- enum shims
if QT6:  # pragma: no branch
    DisplayRole = Qt.ItemDataRole.DisplayRole
    Horizontal = Qt.Orientation.Horizontal
    MOUSE_LEFT = Qt.MouseButton.LeftButton
    Key_Down = Qt.Key.Key_Down
    Key_Up = Qt.Key.Key_Up
    Key_Left = Qt.Key.Key_Left
    Key_Right = Qt.Key.Key_Right
    Key_PageDown = Qt.Key.Key_PageDown
    Key_PageUp = Qt.Key.Key_PageUp
    BottomDock = Qt.DockWidgetArea.BottomDockWidgetArea
    Stretch = QHeaderView.ResizeMode.Stretch
    SingleSelection = QTreeView.SelectionMode.SingleSelection
    MB_Yes = QMessageBox.StandardButton.Yes
    MB_No = QMessageBox.StandardButton.No
    FONT_TYPEWRITER = QFont.StyleHint.TypeWriter
    SC_Open = QKeySequence.StandardKey.Open
else:  # pragma: no cover
    DisplayRole = Qt.DisplayRole
    Horizontal = Qt.Horizontal
    MOUSE_LEFT = Qt.LeftButton
    Key_Down = Qt.Key_Down
    Key_Up = Qt.Key_Up
    Key_Left = Qt.Key_Left
    Key_Right = Qt.Key_Right
    Key_PageDown = Qt.Key_PageDown
    Key_PageUp = Qt.Key_PageUp
    BottomDock = Qt.BottomDockWidgetArea
    Stretch = QHeaderView.Stretch
    SingleSelection = QTreeView.SingleSelection
    MB_Yes = QMessageBox.Yes
    MB_No = QMessageBox.No
    FONT_TYPEWRITER = QFont.TypeWriter
    SC_Open = QKeySequence.Open


def event_pos(e):
    """QMouseEvent position as QPoint (Qt6: position(), Qt5: pos())."""
    return e.position() if QT6 else e.pos()


def exec_app(app):
    """QApplication.exec() (Qt6) or .exec_() (Qt5)."""
    return app.exec() if QT6 else app.exec_()
