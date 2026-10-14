"""PyQt6 front-end. Kept in a subpackage so the CLI works without Qt installed.

    python -m mfim.gui.app              # launch the window
    python -m mfim.gui.app image.img    # open an image immediately
"""

from __future__ import annotations

QT_BINDING = "none"
try:  # pragma: no cover - exercised only with Qt present
    from .qt_model import MfimTreeModel  # noqa: F401
    from .win_main import MainWindow  # noqa: F401
    from .hex_widget import HexViewWidget  # noqa: F401

    from .qt_model import QT_BINDING  # noqa: F401
except ImportError as exc:  # pragma: no cover
    _GUI_IMPORT_ERROR = exc
else:
    _GUI_IMPORT_ERROR = None

__all__ = ["MfimTreeModel", "MainWindow", "HexViewWidget", "QT_BINDING"]
