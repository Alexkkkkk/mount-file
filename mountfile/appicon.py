# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Application icon lookup for the GUI.

The artwork lives in ``mountfile/data`` in the three flavours the
platforms need:

* ``icon.ico``  - Windows (16..256) and the PyInstaller ``--icon``
* ``icon.icns`` - macOS bundle icon
* ``icon.png``  - Linux window icon and the runtime ``QIcon``

``icon.png`` is shipped inside the frozen executable with
``--add-data "mountfile/data/icon.png:mountfile/data"``. ``icon_path``
looks in the source tree first and then in ``sys._MEIPASS``, so the same
call works from a checkout, from an installed wheel and from a one-file
PyInstaller build. Everything here is cosmetic: a missing icon returns
``None`` instead of raising.
"""

from __future__ import annotations

import os
import sys

ICON_NAME = "icon.png"
ICO_NAME = "icon.ico"
ICNS_NAME = "icon.icns"


def _data_roots():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "data")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, "mountfile", "data")
        yield os.path.join(meipass, "data")


def data_file(name):
    """Absolute path of ``name`` inside the package data dir, or None."""
    for root in _data_roots():
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


def icon_path():
    """Path to the runtime PNG icon, or None when it was not packaged."""
    return data_file(ICON_NAME)


def icon_file():
    """Path to the build icon of this platform (ico/icns/png)."""
    if sys.platform.startswith("win"):
        return data_file(ICO_NAME) or icon_path()
    if sys.platform == "darwin":
        return data_file(ICNS_NAME) or icon_path()
    return icon_path()


def _qt_app_running():
    """True when a live QGuiApplication exists in this process.

    ``QIcon(path)`` must not be constructed without one: Qt aborts the
    whole interpreter (``Fatal Python error: Aborted``, exit code 134)
    instead of raising, which used to kill pytest and the GUI smoke job
    as soon as a QApplication created by an earlier test had been
    garbage collected. Everything here is cosmetic, so the cheap check
    comes before the QIcon call.
    """
    try:
        from mountfile.qt_compat import QtCore
    except Exception:  # pragma: no cover - Qt missing
        return False
    try:
        return QtCore.QCoreApplication.instance() is not None
    except Exception:  # pragma: no cover - broken Qt build
        return False


def app_icon():
    """QIcon for QApplication / MainWindow / tray icon.

    Returns ``None`` when Qt, the artwork or a live QApplication is
    unavailable, so callers can simply skip setting an icon instead of
    crashing a headless build.
    """
    path = icon_path()
    if not path:
        return None
    if not _qt_app_running():
        return None
    try:
        from mountfile.qt_compat import QIcon
    except Exception:  # pragma: no cover - Qt missing
        return None
    try:
        return QIcon(path)
    except Exception:  # pragma: no cover - icons are cosmetic
        return None


if __name__ == "__main__":  # pragma: no cover - build helper
    # `python -m mountfile.appicon` prints the build icon of this platform;
    # the PyInstaller steps in CI and the Makefile use it as --icon value.
    print(icon_file() or "")
