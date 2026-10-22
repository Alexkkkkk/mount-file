# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Qt compatibility layer: PyQt6 primary, PyQt5 fallback for legacy environments.

The supported application matrix uses PyQt6. The PyQt5 branch remains for
legacy/alternate environments, but the current package does not support
Windows 7. All GUI code imports Qt names from here and never from PyQt* directly.

IMPORTANT: both branches must export the SAME set of names. The PyQt5
branch used to import only a handful of widgets, so hexview.py died with
``ImportError: cannot import name 'QAbstractScrollArea' from 'mountfile'``
on every PyQt5 build (the "Unhandled exception in script" dialog).

NOTE: QKeySequence lives in QtGui in BOTH Qt5 and Qt6 - importing it
from QtCore raises ImportError and kills the whole application at
startup (this was the "won't launch on Windows 10" bug).
NOTE: QAction lives in QtWidgets on Qt5 but moved to QtGui in Qt6.
NOTE: scoped enums (``Qt.ItemDataRole.DisplayRole``) exist only in Qt6, so
every enum used by the GUI is flattened into a plain module-level name in
the ``if QT6:`` / ``else:`` block below - GUI modules never touch an enum
namespace themselves.
"""

from __future__ import annotations

QT6 = True
try:  # pragma: no cover - depends on environment
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import (
        QAbstractItemModel,
        QEvent,
        QEventLoop,
        QModelIndex,
        QSize,
        Qt,
        QThread,
        QTimer,
        pyqtSignal,
        pyqtSlot,
    )
    from PyQt6.QtGui import (
        QAction,
        QBrush,
        QColor,
        QFont,
        QFontDatabase,
        QFontMetrics,
        QIcon,
        QKeySequence,
        QPainter,
        QPalette,
        QPen,
    )
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QAbstractScrollArea,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDockWidget,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMenuBar,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStyle,
        QTabBar,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QToolBar,
        QToolButton,
        QTreeView,
        QVBoxLayout,
        QWidget,
    )
except ImportError as _pyqt6_error:  # pragma: no cover
    QT6 = False
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        from PyQt5.QtCore import (
            QAbstractItemModel,
            QEvent,
            QEventLoop,
            QModelIndex,
            QSize,
            Qt,
            QThread,
            QTimer,
            pyqtSignal,
            pyqtSlot,
        )
        from PyQt5.QtGui import (
            QBrush,
            QColor,
            QFont,
            QFontDatabase,
            QFontMetrics,
            QIcon,
            QKeySequence,
            QPainter,
            QPalette,
            QPen,
        )
        from PyQt5.QtWidgets import (
            QAbstractItemView,
            QAbstractScrollArea,
            QAction,
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QDockWidget,
            QFileDialog,
            QFormLayout,
            QGridLayout,
            QHBoxLayout,
            QHeaderView,
            QInputDialog,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMenu,
            QMenuBar,
            QMessageBox,
            QPlainTextEdit,
            QProgressBar,
            QPushButton,
            QSizePolicy,
            QSpinBox,
            QSplitter,
            QStyle,
            QTabBar,
            QTableWidget,
            QTableWidgetItem,
            QTabWidget,
            QTextEdit,
            QToolBar,
            QToolButton,
            QTreeView,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as _pyqt5_error:
        raise ImportError(
            "no usable Qt bindings: PyQt6 -> %s; PyQt5 -> %s"
            % (_pyqt6_error, _pyqt5_error)
        ) from _pyqt5_error


# ---------------------------------------------------------- enum shims
if QT6:  # pragma: no branch
    DisplayRole = Qt.ItemDataRole.DisplayRole
    Horizontal = Qt.Orientation.Horizontal
    MOUSE_LEFT = Qt.MouseButton.LeftButton
    MOUSE_RIGHT = Qt.MouseButton.RightButton
    CustomContextMenu = Qt.ContextMenuPolicy.CustomContextMenu
    Key_Up = Qt.Key.Key_Up
    Key_Down = Qt.Key.Key_Down
    Key_Left = Qt.Key.Key_Left
    Key_Right = Qt.Key.Key_Right
    Key_PageDown = Qt.Key.Key_PageDown
    Key_PageUp = Qt.Key.Key_PageUp
    Key_Backspace = Qt.Key.Key_Backspace
    Key_Return = Qt.Key.Key_Return
    Key_Enter = Qt.Key.Key_Enter
    Key_Tab = Qt.Key.Key_Tab
    Key_A = Qt.Key.Key_A
    Key_Delete = Qt.Key.Key_Delete
    Key_Insert = Qt.Key.Key_Insert
    Key_Space = Qt.Key.Key_Space
    CtrlModifier = Qt.KeyboardModifier.ControlModifier
    ShiftModifier = Qt.KeyboardModifier.ShiftModifier
    AltModifier = Qt.KeyboardModifier.AltModifier
    BottomDock = Qt.DockWidgetArea.BottomDockWidgetArea
    Stretch = QHeaderView.ResizeMode.Stretch
    HVC_Interactive = QHeaderView.ResizeMode.Interactive
    HVC_ResizeToContents = QHeaderView.ResizeMode.ResizeToContents
    SingleSelection = QTreeView.SelectionMode.SingleSelection
    TableSingleSelection = QAbstractItemView.SelectionMode.SingleSelection
    TableExtendedSelection = QAbstractItemView.SelectionMode.ExtendedSelection
    SelectRows = QAbstractItemView.SelectionBehavior.SelectRows
    NoEditTriggers = QAbstractItemView.EditTrigger.NoEditTriggers
    MB_Yes = QMessageBox.StandardButton.Yes
    MB_No = QMessageBox.StandardButton.No
    FONT_TYPEWRITER = QFont.StyleHint.TypeWriter
    SC_Open = QKeySequence.StandardKey.Open
    QEvent_KeyPress = QEvent.Type.KeyPress
    ItemIsEnabled = Qt.ItemFlag.ItemIsEnabled
    ItemIsSelectable = Qt.ItemFlag.ItemIsSelectable
    AlignLeft = Qt.AlignmentFlag.AlignLeft
    AlignRight = Qt.AlignmentFlag.AlignRight
    AlignVCenter = Qt.AlignmentFlag.AlignVCenter
    ElideMiddle = Qt.TextElideMode.ElideMiddle
    ElideRight = Qt.TextElideMode.ElideRight
    AscendingOrder = Qt.SortOrder.AscendingOrder
    DescendingOrder = Qt.SortOrder.DescendingOrder
    SizePolicyExpanding = QSizePolicy.Policy.Expanding
    SizePolicyFixed = QSizePolicy.Policy.Fixed
    SizePolicyIgnored = QSizePolicy.Policy.Ignored
    ComboAdjustToContents = QComboBox.SizeAdjustPolicy.AdjustToContents
    ComboAdjustToMinimumContentsLengthWithIcon = (
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    DlgOk = QDialogButtonBox.StandardButton.Ok
    DlgCancel = QDialogButtonBox.StandardButton.Cancel
    DlgClose = QDialogButtonBox.StandardButton.Close
    DlgActionRole = QDialogButtonBox.ButtonRole.ActionRole
    EchoPassword = QLineEdit.EchoMode.Password
    DialogAccepted = QDialog.DialogCode.Accepted
    TB_IconOnly = Qt.ToolButtonStyle.ToolButtonIconOnly
    TB_TextBesideIcon = Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    TB_InstantPopup = QToolButton.ToolButtonPopupMode.InstantPopup
    _ROLE = QPalette.ColorRole
    P_WINDOW = _ROLE.Window
    P_WINDOWTEXT = _ROLE.WindowText
    P_BASE = _ROLE.Base
    P_ALTERNATEBASE = _ROLE.AlternateBase
    P_TEXT = _ROLE.Text
    P_BUTTON = _ROLE.Button
    P_BUTTONTEXT = _ROLE.ButtonText
    P_HIGHLIGHT = _ROLE.Highlight
    P_HIGHLIGHTEDTEXT = _ROLE.HighlightedText
    P_TOOLTIPBASE = _ROLE.ToolTipBase
    P_TOOLTIPTEXT = _ROLE.ToolTipText
    CG_Disabled = QPalette.ColorGroup.Disabled
else:  # pragma: no cover - legacy PyQt5 fallback
    DisplayRole = Qt.DisplayRole
    Horizontal = Qt.Horizontal
    MOUSE_LEFT = Qt.LeftButton
    MOUSE_RIGHT = Qt.RightButton
    CustomContextMenu = Qt.CustomContextMenu
    Key_Up = Qt.Key_Up
    Key_Down = Qt.Key_Down
    Key_Left = Qt.Key_Left
    Key_Right = Qt.Key_Right
    Key_PageDown = Qt.Key_PageDown
    Key_PageUp = Qt.Key_PageUp
    Key_Backspace = Qt.Key_Backspace
    Key_Return = Qt.Key_Return
    Key_Enter = Qt.Key_Enter
    Key_Tab = Qt.Key_Tab
    Key_A = Qt.Key_A
    Key_Delete = Qt.Key_Delete
    Key_Insert = Qt.Key_Insert
    Key_Space = Qt.Key_Space
    CtrlModifier = Qt.ControlModifier
    ShiftModifier = Qt.ShiftModifier
    AltModifier = Qt.AltModifier
    BottomDock = Qt.BottomDockWidgetArea
    Stretch = QHeaderView.Stretch
    HVC_Interactive = QHeaderView.Interactive
    HVC_ResizeToContents = QHeaderView.ResizeToContents
    SingleSelection = QTreeView.SingleSelection
    TableSingleSelection = QAbstractItemView.SingleSelection
    TableExtendedSelection = QAbstractItemView.ExtendedSelection
    SelectRows = QAbstractItemView.SelectRows
    NoEditTriggers = QAbstractItemView.NoEditTriggers
    MB_Yes = QMessageBox.Yes
    MB_No = QMessageBox.No
    FONT_TYPEWRITER = QFont.TypeWriter
    SC_Open = QKeySequence.Open
    QEvent_KeyPress = QEvent.KeyPress
    ItemIsEnabled = Qt.ItemIsEnabled
    ItemIsSelectable = Qt.ItemIsSelectable
    AlignLeft = Qt.AlignLeft
    AlignRight = Qt.AlignRight
    AlignVCenter = Qt.AlignVCenter
    ElideMiddle = Qt.ElideMiddle
    ElideRight = Qt.ElideRight
    AscendingOrder = Qt.AscendingOrder
    DescendingOrder = Qt.DescendingOrder
    SizePolicyExpanding = QSizePolicy.Expanding
    SizePolicyFixed = QSizePolicy.Fixed
    SizePolicyIgnored = QSizePolicy.Ignored
    ComboAdjustToContents = QComboBox.AdjustToContents
    ComboAdjustToMinimumContentsLengthWithIcon = (
        QComboBox.AdjustToMinimumContentsLengthWithIcon
    )
    DlgOk = QDialogButtonBox.Ok
    DlgCancel = QDialogButtonBox.Cancel
    DlgClose = QDialogButtonBox.Close
    DlgActionRole = QDialogButtonBox.ActionRole
    EchoPassword = QLineEdit.Password
    DialogAccepted = QDialog.Accepted
    TB_IconOnly = Qt.ToolButtonIconOnly
    TB_TextBesideIcon = Qt.ToolButtonTextBesideIcon
    TB_InstantPopup = QToolButton.InstantPopup
    P_WINDOW = QPalette.Window
    P_WINDOWTEXT = QPalette.WindowText
    P_BASE = QPalette.Base
    P_ALTERNATEBASE = QPalette.AlternateBase
    P_TEXT = QPalette.Text
    P_BUTTON = QPalette.Button
    P_BUTTONTEXT = QPalette.ButtonText
    P_HIGHLIGHT = QPalette.Highlight
    P_HIGHLIGHTEDTEXT = QPalette.HighlightedText
    P_TOOLTIPBASE = QPalette.ToolTipBase
    P_TOOLTIPTEXT = QPalette.ToolTipText
    CG_Disabled = QPalette.Disabled


# -------------------------------------------------------------- helpers
def event_pos(e):
    """QMouseEvent position as QPoint (Qt6: position(), Qt5: pos())."""
    return e.position() if QT6 else e.pos()


def font_char_width(metrics):
    """Width of the '0' glyph (Qt5 < 5.11 only knows QFontMetrics.width)."""
    advance = getattr(metrics, "horizontalAdvance", None)
    if advance is not None:
        return advance("0")
    return metrics.width("0")


def standard_icon(widget, name):
    """QIcon from ``QStyle.StandardPixmap.<name>`` (empty when absent).

    `name` is the Qt6 enum member name, e.g. ``SP_DialogOpenButton``; Qt5
    exposes the same constants directly on QStyle. An unknown name yields an
    empty icon: a cosmetic toolbar must never break the window.
    """
    try:
        std = getattr(QStyle.StandardPixmap, name, None)
        if std is None:
            std = getattr(QStyle, name, None)
        return widget.style().standardIcon(std) if std is not None else QIcon()
    except Exception:  # pragma: no cover - icons are cosmetic
        return QIcon()


def exec_app(app):
    """QApplication.exec() (Qt6) or .exec_() (Qt5)."""
    return app.exec() if QT6 else app.exec_()


def exec_dialog(dlg):
    """QDialog.exec() (Qt6) or .exec_() (Qt5)."""
    return dlg.exec() if QT6 else dlg.exec_()


# ---------------------------------------------------------- compat aliases
# The OLMA-era rewrite of this module exposed ``IS_QT6`` / ``QT_VERSION``
# and ``require_qt()``; keep those names so both the old (``QT6``) and the
# new (``IS_QT6``) call sites keep working.
QT_VERSION = "PyQt6" if QT6 else "PyQt5"
IS_QT6 = QT6


def is_qt_available() -> bool:
    """True when a Qt binding was imported successfully."""
    return True


def require_qt() -> None:
    """No-op when Qt is ready.

    Importing this module already raised ``ImportError`` when neither
    PyQt6 nor PyQt5 could be imported, so reaching this line means the
    bindings are loaded.
    """
    return None


# -------------------------------------------------------------- re-exports
# Listed explicitly so linters treat every import above as part of the
# public API of this compatibility layer (see per-file-ignores in
# pyproject.toml): GUI modules import ALL Qt names from here and never
# from PyQt* directly.
__all__ = [
    "QT6",
    "IS_QT6",
    "QT_VERSION",
    "is_qt_available",
    "require_qt",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "Qt",
    "pyqtSignal",
    "pyqtSlot",
    "QAbstractItemModel",
    "QEvent",
    "QModelIndex",
    "QSize",
    "QThread",
    "QAction",
    "QBrush",
    "QColor",
    "QFont",
    "QFontDatabase",
    "QFontMetrics",
    "QIcon",
    "QKeySequence",
    "QPainter",
    "QPalette",
    "QPen",
    "QAbstractItemView",
    "QAbstractScrollArea",
    "QApplication",
    "QCheckBox",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QDockWidget",
    "QFileDialog",
    "QFormLayout",
    "QGridLayout",
    "QHBoxLayout",
    "QHeaderView",
    "QInputDialog",
    "QLabel",
    "QLineEdit",
    "QMainWindow",
    "QMenu",
    "QMenuBar",
    "QMessageBox",
    "QPlainTextEdit",
    "QProgressBar",
    "QPushButton",
    "QSizePolicy",
    "QSpinBox",
    "QSplitter",
    "QStyle",
    "QTabBar",
    "QTableWidget",
    "QTableWidgetItem",
    "QTabWidget",
    "QTextEdit",
    "QToolBar",
    "QToolButton",
    "QTreeView",
    "QVBoxLayout",
    "QWidget",
    "DisplayRole",
    "Horizontal",
    "MOUSE_LEFT",
    "MOUSE_RIGHT",
    "CustomContextMenu",
    "Key_Up",
    "Key_Down",
    "Key_Left",
    "Key_Right",
    "Key_PageDown",
    "Key_PageUp",
    "Key_Backspace",
    "Key_Return",
    "Key_Enter",
    "Key_Tab",
    "Key_A",
    "Key_Delete",
    "Key_Insert",
    "Key_Space",
    "CtrlModifier",
    "ShiftModifier",
    "AltModifier",
    "BottomDock",
    "Stretch",
    "HVC_Interactive",
    "HVC_ResizeToContents",
    "SingleSelection",
    "TableSingleSelection",
    "TableExtendedSelection",
    "SelectRows",
    "NoEditTriggers",
    "MB_Yes",
    "MB_No",
    "FONT_TYPEWRITER",
    "SC_Open",
    "QEvent_KeyPress",
    "ItemIsEnabled",
    "ItemIsSelectable",
    "AlignLeft",
    "AlignRight",
    "AlignVCenter",
    "ElideMiddle",
    "ElideRight",
    "AscendingOrder",
    "DescendingOrder",
    "SizePolicyExpanding",
    "SizePolicyFixed",
    "SizePolicyIgnored",
    "ComboAdjustToContents",
    "ComboAdjustToMinimumContentsLengthWithIcon",
    "DlgOk",
    "DlgCancel",
    "DlgClose",
    "DlgActionRole",
    "EchoPassword",
    "DialogAccepted",
    "TB_IconOnly",
    "TB_TextBesideIcon",
    "TB_InstantPopup",
    "P_WINDOW",
    "P_WINDOWTEXT",
    "P_BASE",
    "P_ALTERNATEBASE",
    "P_TEXT",
    "P_BUTTON",
    "P_BUTTONTEXT",
    "P_HIGHLIGHT",
    "P_HIGHLIGHTEDTEXT",
    "P_TOOLTIPBASE",
    "P_TOOLTIPTEXT",
    "CG_Disabled",
    "event_pos",
    "font_char_width",
    "standard_icon",
    "exec_app",
    "exec_dialog",
]
