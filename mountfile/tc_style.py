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
    CG_Disabled,
    QColor,
    QFont,
    QFontDatabase,
    QPalette,
)

# Two accessible, utility-first skins.  ``text_dim`` intentionally remains
# bright enough for body copy on both the panel and the surrounding chrome.
THEMES = {
    "dark": {
        "chrome": "#111827",
        "chrome_dark": "#0b1220",
        "panel": "#0f172a",
        "text": "#f8fafc",
        "text_dim": "#aab8cc",
        "text_dir": "#bfdbfe",
        "header": "#1e293b",
        "header_text": "#f8fafc",
        "border": "#334155",
        "grid": "#263449",
        "select_bg": "#2563eb",
        "select_text": "#ffffff",
        "marked": "#1d4ed8",
        "marked_text": "#ffffff",
        "button": "#243247",
        "button_hover": "#334155",
        "button_text": "#f8fafc",
        "tab_active": "#0f172a",
        "tab_idle": "#1e293b",
        "toolbar": "#172136",
        "status": "#172136",
        "accent": "#2563eb",
        "accent_soft": "#193b6d",
        "success": "#15803d",
        "warning": "#b45309",
        "danger": "#dc2626",
        "prompt": "#93c5fd",
    },
    "light": {
        "chrome": "#f1f5f9",
        "chrome_dark": "#e2e8f0",
        "panel": "#ffffff",
        "text": "#0f172a",
        "text_dim": "#475569",
        "text_dir": "#1d4ed8",
        "header": "#f8fafc",
        "header_text": "#0f172a",
        "border": "#cbd5e1",
        "grid": "#e2e8f0",
        "select_bg": "#2563eb",
        "select_text": "#ffffff",
        "marked": "#dbeafe",
        "marked_text": "#1e3a8a",
        "button": "#ffffff",
        "button_hover": "#eaf2ff",
        "button_text": "#0f172a",
        "tab_active": "#ffffff",
        "tab_idle": "#e2e8f0",
        "toolbar": "#f8fafc",
        "status": "#f1f5f9",
        "accent": "#2563eb",
        "accent_soft": "#dbeafe",
        "success": "#15803d",
        "warning": "#b45309",
        "danger": "#dc2626",
        "prompt": "#1d4ed8",
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
    # AI analysis (the «AI» toolbar button). The API key lives here but is
    # never written to the log, the report or the repository.
    "ai_backend": "none",
    "ai_base_url": "http://127.0.0.1:11434",
    "ai_model": "",
    "ai_api_key": "",
    "ai_max_bytes": 262144,
    # Отдельный адрес OpenAI-совместимого бэкенда: локальный адрес Ollama
    # не должен протекать в облачный эндпоинт (пусто = https://api.openai.com).
    "ai_base_url_openai": "",
    # Format Advisor: локальные рекомендации только по метаданным. Старые
    # GitHub reporter/consensus settings сохранены для совместимости, но GUI
    # больше их не использует и не отправляет советник в сеть.
    "ai_format_autohook": False,
    "ai_github_repo": "",
    "ai_github_token": "",
    # Консенсус советника: при наличии второго доступного бэкенда — голосование.
    "ai_consensus": False,
    # OLMA Watch: opt-in crash monitoring (see mountfile/olma_watch.py).
    # olma_token is read from the MOUNTFILE_OLMA_TOKEN environment variable
    # or this key, and never appears in the log or in any report.
    "olma_watch": False,
    "olma_token": "",
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
    return (
        """
QWidget { background: %(chrome)s; color: %(text)s; }
QMainWindow, QDialog { background: %(chrome)s; }

QMenuBar { background: %(chrome)s; color: %(text)s; border-bottom: 1px solid %(border)s; }
QMenuBar::item { background: transparent; border-radius: 4px; margin: 2px; padding: 5px 9px; }
QMenuBar::item:selected { background: %(accent_soft)s; color: %(header_text)s; }
QMenu { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px; }
QMenu::item { border-radius: 4px; padding: 6px 28px 6px 24px; }
QMenu::item:selected { background: %(select_bg)s; color: %(select_text)s; }
QMenu::item:disabled { color: %(text_dim)s; }
QMenu::separator { background: %(border)s; height: 1px; margin: 4px 7px; }

QToolBar, QToolBar#mainToolbar {
    background: %(toolbar)s; border: 0px; border-bottom: 1px solid %(border)s;
    spacing: 4px; padding: 5px 8px;
}
QToolBar::separator { background: %(border)s; margin: 3px 5px; width: 1px; }
QToolButton {
    background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 6px; min-width: 20px;
}
QToolButton:hover { background: %(button_hover)s; border-color: %(accent)s; }
QToolButton:pressed, QToolButton:checked { background: %(select_bg)s; border-color: %(select_bg)s; color: %(select_text)s; }
QToolButton:disabled { background: %(chrome_dark)s; border-color: %(border)s; color: %(text_dim)s; }
QToolButton:focus { border: 2px solid %(accent)s; }

QPushButton {
    background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 6px 10px;
}
QPushButton:hover { background: %(button_hover)s; border-color: %(accent)s; }
QPushButton:pressed { background: %(select_bg)s; border-color: %(select_bg)s; color: %(select_text)s; }
QPushButton:disabled { background: %(chrome_dark)s; border-color: %(border)s; color: %(text_dim)s; }
QPushButton:focus { border: 2px solid %(accent)s; }
QPushButton#primaryButton, QToolButton#primaryButton { background: %(accent)s; border-color: %(accent)s; color: %(select_text)s; font-weight: 600; }
QPushButton#primaryButton:hover, QToolButton#primaryButton:hover { background: %(select_bg)s; }
QPushButton#dangerButton, QToolButton#dangerButton { background: %(danger)s; border-color: %(danger)s; color: #ffffff; }
QPushButton#dangerButton:hover, QToolButton#dangerButton:hover { background: %(danger)s; border-color: %(header_text)s; }
QPushButton#functionButton, QToolButton#functionButton { background: %(header)s; }
QPushButton#functionButton:disabled, QPushButton#dangerButton:disabled { background: %(chrome_dark)s; border-color: %(border)s; color: %(text_dim)s; }
QPushButton#dangerButton { background: %(button)s; color: %(text)s; border-color: %(border)s; }
QPushButton#dangerButton:hover { background: %(danger)s; color: #ffffff; }

QLineEdit, QPlainTextEdit, QTextEdit {
    background: %(panel)s; color: %(text)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 5px 7px;
    selection-background-color: %(select_bg)s; selection-color: %(select_text)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus { border: 2px solid %(accent)s; }
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled { background: %(chrome_dark)s; color: %(text_dim)s; }
QLineEdit#commandLine { background: %(chrome_dark)s; border-color: %(border)s; color: %(prompt)s; }
QLineEdit#panelSearch { background: %(panel)s; }
QComboBox {
    background: %(button)s; color: %(button_text)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 5px 8px;
}
QComboBox:hover { border-color: %(accent)s; }
QComboBox:focus { border: 2px solid %(accent)s; }
QComboBox:disabled { background: %(chrome_dark)s; color: %(text_dim)s; }
QComboBox QAbstractItemView {
    background: %(header)s; color: %(text)s; border: 1px solid %(border)s;
    selection-background-color: %(select_bg)s; selection-color: %(select_text)s;
}

QWidget#workspaceHeader {
    background: %(toolbar)s; border: 1px solid %(border)s; border-radius: 8px;
    padding: 7px 9px;
}
QWidget#filePanel { background: %(panel)s; border: 1px solid %(border)s; border-radius: 8px; }
QWidget#filePanel[active="true"] { border: 2px solid %(accent)s; }
QWidget#filePanel[active="true"] QWidget#workspaceHeader { border-color: %(accent)s; }
QLabel#panelTitle { color: %(header_text)s; font-weight: 600; }
QLabel#sourceBadge {
    background: %(accent_soft)s; border: 1px solid %(accent)s; border-radius: 9px;
    color: %(text_dir)s; font-weight: 600; padding: 3px 7px;
}
QLabel#panelSpace, QLabel#panelStatus, QLabel#emptyHint { color: %(text_dim)s; }
QLabel#panelPath { color: %(text_dir)s; font-weight: 600; }
QWidget#panelStatus { background: %(status)s; border-top: 1px solid %(border)s; padding: 5px 7px; }
QWidget#emptyState { background: %(panel)s; }
QLabel#emptyTitle { color: %(header_text)s; font-size: 12pt; font-weight: 600; }

QTableView, QTableWidget {
    alternate-background-color: %(chrome_dark)s; background: %(panel)s; color: %(text)s;
    border: 1px solid %(border)s; gridline-color: %(grid)s;
    selection-background-color: %(select_bg)s; selection-color: %(select_text)s;
}
QTableView:focus, QTableWidget:focus { border: 2px solid %(accent)s; }
QTableView::item, QTableWidget::item { border: 0px; padding: 4px 6px; }
QTableView::item:hover, QTableWidget::item:hover { background: %(accent_soft)s; }
QTableView::item:selected, QTableWidget::item:selected { background: %(select_bg)s; color: %(select_text)s; }
QHeaderView { background: %(header)s; }
QHeaderView::section {
    background: %(header)s; color: %(header_text)s; border: 0px;
    border-right: 1px solid %(border)s; border-bottom: 1px solid %(border)s;
    padding: 6px 8px;
}
QHeaderView::section:hover { background: %(button_hover)s; }
QTableCornerButton::section { background: %(header)s; border: 0px; border-right: 1px solid %(border)s; border-bottom: 1px solid %(border)s; }

QTabWidget::pane { background: %(panel)s; border: 1px solid %(border)s; border-radius: 0px 0px 8px 8px; }
QTabBar { background: %(chrome)s; }
QTabBar::tab {
    background: %(tab_idle)s; color: %(text_dim)s; border: 1px solid %(border)s;
    border-bottom: 0px; border-top-left-radius: 6px; border-top-right-radius: 6px;
    margin-right: 3px; padding: 6px 10px;
}
QTabBar::tab:hover { background: %(button_hover)s; color: %(text)s; }
QTabBar::tab:selected { background: %(tab_active)s; border-color: %(accent)s; color: %(header_text)s; font-weight: 600; }
QTabBar::tab:focus { border: 2px solid %(accent)s; }

QLabel { background: transparent; }
QSplitter { background: %(chrome)s; }
QSplitter::handle { background: %(border)s; }
QSplitter::handle:hover { background: %(accent)s; }
QSplitter::handle:horizontal { margin: 8px 0px; width: 4px; }
QStatusBar { background: %(status)s; color: %(text_dim)s; border-top: 1px solid %(border)s; }
QStatusBar::item { border: 0px; }
QDockWidget { color: %(text)s; }
QDockWidget::title { background: %(header)s; border-bottom: 1px solid %(border)s; padding: 6px 8px; }

QScrollBar:vertical { background: %(chrome_dark)s; border: 0px; margin: 2px; width: 10px; }
QScrollBar::handle:vertical { background: %(button)s; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: %(button_hover)s; }
QScrollBar::handle:vertical:pressed { background: %(accent)s; }
QScrollBar:add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: %(chrome_dark)s; border: 0px; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: %(button)s; border-radius: 5px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: %(button_hover)s; }
QScrollBar::handle:horizontal:pressed { background: %(accent)s; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QCheckBox, QRadioButton { background: transparent; color: %(text)s; spacing: 6px; }
QCheckBox:focus, QRadioButton:focus { color: %(text_dir)s; }
QProgressBar { background: %(chrome_dark)s; border: 1px solid %(border)s; border-radius: 5px; color: %(text)s; text-align: center; }
QProgressBar::chunk { background: %(accent)s; border-radius: 4px; }
QToolTip { background: %(header)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 4px; padding: 4px 6px; }
"""
        % t
    )


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
    return f"{int(n):,}".replace(",", " ")


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


#: What the Тип column prints for a directory (the size column reads
#: '<Папка>').
DIR_TYPE_TEXT = "Папка"


def file_type(name, is_dir, up=False):
    """The 'Тип' column: the extension for files, 'Папка' for directories.

    The reference Total Commander window prints the kind in this column as
    well, so a directory row reads 'Папка' here and '<Папка>' in the size
    column. Leaving the whole column blank for a folder listing (every row
    a directory) made the pane look broken, which is what it used to do.
    """
    if is_dir:
        return DIR_TYPE_TEXT
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
    os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")),
    "mount-file",
    "ui.json",
)


def load_settings():
    """Read theme/font/log switches; a missing or broken file is ignored."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(SETTINGS)
    for key in ("theme", "font_size", "show_log"):
        if key in data:
            SETTINGS[key] = data[key]
    # Булевы переключатели: без этой ветки сохранённые значения (включая
    # olma_watch) после перезапуска всегда откатывались к дефолту.
    for key in ("show_log", "olma_watch", "ai_format_autohook", "ai_consensus"):
        if key in data:
            SETTINGS[key] = bool(data[key])
    for key in (
        "ai_backend",
        "ai_base_url",
        "ai_base_url_openai",
        "ai_model",
        "ai_api_key",
        "ai_github_repo",
        "ai_github_token",
        "olma_token",
    ):
        if key in data:
            SETTINGS[key] = str(data[key] or "")
    try:
        SETTINGS["ai_max_bytes"] = int(SETTINGS["ai_max_bytes"])
    except (TypeError, ValueError):
        SETTINGS["ai_max_bytes"] = 262144
    if SETTINGS["theme"] not in THEMES:
        SETTINGS["theme"] = "dark"
    try:
        SETTINGS["font_size"] = max(7, min(16, int(SETTINGS["font_size"])))
    except (TypeError, ValueError):
        SETTINGS["font_size"] = 9
    SETTINGS["show_log"] = bool(SETTINGS["show_log"])
    SETTINGS["olma_watch"] = bool(SETTINGS["olma_watch"])
    SETTINGS["ai_format_autohook"] = bool(SETTINGS.get("ai_format_autohook"))
    SETTINGS["ai_consensus"] = bool(SETTINGS.get("ai_consensus"))
    SETTINGS["ai_max_bytes"] = max(4096, min(SETTINGS["ai_max_bytes"], 64 << 20))
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
