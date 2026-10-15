# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Total Commander look & feel for the Mount File GUI.

The window reproduces the classic two-pane Total Commander layout:
menu bar (Файлы … Справка), icon toolbar, folder tabs, drive bar with the
free-space readout, a ``*.*`` path line, the file list with the columns
Имя / Тип / Размер / Дата / Атрибуты, the command line and the function key
bar (F3 Просмотр … Alt+F4 Выход).

Everything visual lives here (palette + Qt style sheet + the small
formatting helpers used by the panels) so the panels and the main window
cannot drift apart.
"""

from __future__ import annotations

import json
import os
import time

from .qt_compat import (
    CG_Disabled,
    P_ALTERNATEBASE,
    P_BASE,
    P_BUTTON,
    P_BUTTONTEXT,
    P_HIGHLIGHT,
    P_HIGHLIGHTEDTEXT,
    P_TEXT,
    P_TOOLTIPBASE,
    P_TOOLTIPTEXT,
    P_WINDOW,
    P_WINDOWTEXT,
    QColor,
    QFont,
    QFontDatabase,
    QPalette,
)

# Two skins: the dark one the interface is modelled on and the classic
# light grey one people know from Total Commander on Windows.
THEMES = {
    "dark": {
        "chrome": "#222222",
        "chrome_dark": "#161616",
        "panel": "#000000",
        "text": "#c8c8c8",
        "text_dim": "#6f6f6f",
        "text_dir": "#f0f0f0",
        "header": "#2b2b2b",
        "header_text": "#e6e6e6",
        "border": "#4a4a4a",
        "grid": "#2a2a2a",
        "select_bg": "#1f5aa8",
        "select_text": "#ffffff",
        "marked": "#14456f",
        "marked_text": "#ffffff",
        "button": "#2d2d2d",
        "button_hover": "#3b3b3b",
        "button_text": "#dcdcdc",
        "tab_active": "#101010",
        "tab_idle": "#2b2b2b",
        "toolbar": "#262626",
        "status": "#1c1c1c",
        "accent": "#3d7fd6",
        "prompt": "#9ad0ff",
    },
    "light": {
        "chrome": "#d4d0c8",
        "chrome_dark": "#bfbbb2",
        "panel": "#ffffff",
        "text": "#000000",
        "text_dim": "#8a8a8a",
        "text_dir": "#000060",
        "header": "#d4d0c8",
        "header_text": "#000000",
        "border": "#808080",
        "grid": "#e0e0e0",
        "select_bg": "#000080",
        "select_text": "#ffffff",
        "marked": "#cce4ff",
        "marked_text": "#000060",
        "button": "#d4d0c8",
        "button_hover": "#e5e2db",
        "button_text": "#000000",
        "tab_active": "#ffffff",
        "tab_idle": "#c8c4bc",
        "toolbar": "#d4d0c8",
        "status": "#d4d0c8",
        "accent": "#000080",
        "prompt": "#000060",
    },
}

# UI names <-> internal ids (used by the Конфигурация menu and dialogs).
THEME_BY_LABEL = {"Тёмная": "dark", "Светлая": "light"}
THEME_LABEL = {v: k for k, v in THEME_BY_LABEL.items()}

# Live user preferences.  Small enough to keep in memory: they are applied
# immediately and never change the behaviour of the file engine.
SETTINGS = {
    "theme": "dark",
    "font_size": 9,
    "show_log": False,
}

_FAMILY = []


def family():
    """First UI font of the classic Windows list that exists on this box."""
    if _FAMILY:
        return _FAMILY[0]
    try:
        installed = set(QFontDatabase.families())
    except Exception:  # pragma: no cover - very old Qt
        installed = set()
    for candidate in (
        "Segoe UI",
        "Tahoma",
        "Verdana",
        "Noto Sans",
        "Open Sans",
        "DejaVu Sans",
        "Liberation Sans",
        "Arial",
    ):
        if candidate in installed:
            _FAMILY.append(candidate)
            return candidate
    _FAMILY.append("Sans Serif")
    return _FAMILY[0]


def theme():
    return SETTINGS["theme"]


def color(key):
    """Current theme colour by key (panels colour their own labels)."""
    return THEMES[SETTINGS["theme"]].get(key, "#000000")


def stylesheet(name=None):
    t = THEMES.get(name or SETTINGS["theme"], THEMES["dark"])
    return """
QWidget { background: %(chrome)s; color: %(text)s; }
QMainWindow, QDialog { background: %(chrome)s; }
QMenuBar { background: %(chrome)s; color: %(text)s; border-bottom: 1px solid %(border)s; }
QMenuBar::item { padding: 2px 8px; background: transparent; }
QMenuBar::item:selected { background: %(select_bg)s; color: %(select_text)s; }
QMenu { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; }
QMenu::item { padding: 3px 24px 3px 22px; }
QMenu::item:selected { background: %(select_bg)s; color: %(select_text)s; }
QMenu::item:disabled { color: %(text_dim)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 3px 6px; }
QToolBar { background: %(toolbar)s; border-bottom: 1px solid %(border)s; spacing: 2px; padding: 2px; }
QToolBar::separator { width: 1px; background: %(border)s; margin: 2px 4px; }
QToolButton { background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s; padding: 2px 6px; }
QToolButton:hover { background: %(button_hover)s; }
QToolButton:pressed, QToolButton:checked { background: %(select_bg)s; color: %(select_text)s; }
QPushButton { background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s; padding: 2px 8px; }
QPushButton:hover { background: %(button_hover)s; }
QPushButton:pressed { border: 1px inset %(border)s; }
QPushButton:disabled { color: %(text_dim)s; }
QLineEdit, QPlainTextEdit, QTextEdit { background: %(panel)s; color: %(text)s;
    border: 1px solid %(border)s; padding: 1px 3px;
    selection-background-color: %(select_bg)s; selection-color: %(select_text)s; }
QComboBox { background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s; padding: 1px 4px; }
QComboBox:disabled { color: %(text_dim)s; }
QComboBox QAbstractItemView { background: %(header)s; color: %(text)s; border: 1px solid %(border)s;
    selection-background-color: %(select_bg)s; selection-color: %(select_text)s; }
QTableWidget { background: %(panel)s; color: %(text)s; gridline-color: %(grid)s;
    border: 1px solid %(border)s; outline: 0; }
QTableWidget::item { padding: 0px 3px; border: 0px; }
QTableWidget::item:selected { background: %(select_bg)s; color: %(select_text)s; }
QHeaderView { background: %(header)s; }
QHeaderView::section { background: %(header)s; color: %(header_text)s; border: 0px;
    border-right: 1px solid %(border)s; border-bottom: 1px solid %(border)s; padding: 1px 3px; }
QTableCornerButton::section { background: %(header)s; border: 1px solid %(border)s; }
QTabBar { background: %(chrome)s; }
QTabBar::tab { background: %(tab_idle)s; color: %(text)s; border: 1px solid %(border)s;
    border-bottom: 0px; padding: 1px 8px; margin-right: 1px; }
QTabBar::tab:selected { background: %(tab_active)s; color: %(header_text)s; }
QLabel { background: transparent; }
QSplitter { background: %(chrome)s; }
QSplitter::handle { background: %(border)s; }
QSplitter::handle:horizontal { width: 3px; }
QStatusBar { background: %(status)s; color: %(text)s; }
QStatusBar::item { border: 0px; }
QDockWidget { color: %(text)s; }
QDockWidget::title { background: %(header)s; padding: 2px 6px; }
QScrollBar:vertical { background: %(chrome_dark)s; width: 13px; margin: 0px; }
QScrollBar::handle:vertical { background: %(button)s; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: %(button_hover)s; }
QScrollBar:add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: %(chrome_dark)s; height: 13px; margin: 0px; }
QScrollBar::handle:horizontal { background: %(button)s; min-width: 20px; }
QScrollBar::handle:horizontal:hover { background: %(button_hover)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QCheckBox, QRadioButton { background: transparent; }
QToolTip { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; }
""" % t


def apply_theme(app, name=None):
    """Apply palette + style sheet + UI font to the whole application."""
    if app is None:
        return
    if name:
        SETTINGS["theme"] = name
    t = THEMES.get(SETTINGS["theme"], THEMES["dark"])
    pal = QPalette()
    pal.setColor(P_WINDOW, QColor(t["chrome"]))
    pal.setColor(P_WINDOWTEXT, QColor(t["text"]))
    pal.setColor(P_BASE, QColor(t["panel"]))
    pal.setColor(P_ALTERNATEBASE, QColor(t["chrome_dark"]))
    pal.setColor(P_TEXT, QColor(t["text"]))
    pal.setColor(P_BUTTON, QColor(t["button"]))
    pal.setColor(P_BUTTONTEXT, QColor(t["button_text"]))
    pal.setColor(P_HIGHLIGHT, QColor(t["select_bg"]))
    pal.setColor(P_HIGHLIGHTEDTEXT, QColor(t["select_text"]))
    pal.setColor(P_TOOLTIPBASE, QColor(t["header"]))
    pal.setColor(P_TOOLTIPTEXT, QColor(t["text"]))
    for role in (P_TEXT, P_WINDOWTEXT, P_BUTTONTEXT):
        pal.setColor(CG_Disabled, role, QColor(t["text_dim"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet())
    app.setFont(app_font())


def app_font(size=None, mono=False):
    f = QFont("Courier New" if mono else family(), int(size or SETTINGS["font_size"]))
    if mono:
        f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


# ------------------------------------------------------------- formatting
def group(n):
    """1234567 -> '1 234 567' (Total Commander groups digits with spaces)."""
    return "{:,}".format(int(n)).replace(",", " ")


def fmt_size(n):
    """Plain byte count with digit grouping - as in the TC 'Размер' column."""
    return group(n)


def fmt_kb(nbytes):
    """Byte count as whole kilobytes, for the free-space / status readouts."""
    return "%s Кб" % group(round(float(nbytes) / 1024.0))


def fmt_date(ts):
    """16.08.2017 04:33 (the screenshot's date format, not ISO)."""
    try:
        if not ts or ts <= 0:
            return ""
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return ""


def attrs_str(name="", is_dir=False, writable=True, fat_attr=None):
    """One row of the Имя/Атрибуты columns, e.g. '-a--c' or '----c'.

    The five positions are the ones the reference Total Commander window
    prints (a plain file reads '-a--c', a directory '----c'), in this
    order: read-only, archive, system, hidden, and the trailing flag that
    the classic column always ends with.

    With a real FAT attribute byte (mountfile.fat32.DirEntry.attr) the four
    DOS flags are decoded from it; without one they are derived from the
    host stat and the name, so a local file and a file inside the image are
    formatted the same way.
    """
    if fat_attr is None:
        read_only = not writable
        archive = not is_dir
        system = False
        hidden = str(name).startswith(".")
    else:
        read_only = bool(fat_attr & 0x01)
        archive = bool(fat_attr & 0x20)
        system = bool(fat_attr & 0x04)
        hidden = bool(fat_attr & 0x02)
    return "".join(
        (
            "r" if read_only else "-",
            "a" if archive else "-",
            "s" if system else "-",
            "h" if hidden else "-",
            "c",
        )
    )


def file_type(name, is_dir, up=False):
    """The 'Тип' column: the extension for files, empty for directories.

    Directories do not carry a type in the reference window - their kind is
    shown by '<Папка>' in the size column instead.
    """
    if is_dir:
        return ""
    stem, dot, ext = str(name).rpartition(".")
    return ext.upper() if dot and stem else ""


def qcolor(key):
    """QColor of a theme key (imported lazily to keep this module importable
    without Qt when only the formatting helpers are needed)."""
    from .qt_compat import QColor

    return QColor(color(key))


# ------------------------------------------------------------ settings
#: Where the two switches of the Настройки dialog are remembered.
SETTINGS_PATH = os.path.join(
    os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
    ),
    "mount-file",
    "ui.json",
)


def load_settings():
    """Read theme/font/log switches; a missing or broken file is ignored."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(SETTINGS)
    for key in ("theme", "font_size", "show_log"):
        if key in data:
            SETTINGS[key] = data[key]
    if SETTINGS["theme"] not in THEMES:
        SETTINGS["theme"] = "dark"
    try:
        SETTINGS["font_size"] = max(7, min(16, int(SETTINGS["font_size"])))
    except (TypeError, ValueError):
        SETTINGS["font_size"] = 9
    SETTINGS["show_log"] = bool(SETTINGS["show_log"])
    return dict(SETTINGS)


def save_settings():
    """Persist the switches; failures are not worth interrupting the user."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2, sort_keys=True)
        return True
    except OSError:
        return False
