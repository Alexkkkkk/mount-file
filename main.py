#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Mount File — file manager for raw disk images.

Usage:
    python main.py [disk_image.img]

Works with PyQt6 (Windows 10/11, Linux, macOS) or PyQt5 (Windows 7).
"""

import sys


def main():
    try:
        from PyQt6.QtWidgets import QApplication
        from mountfile.qt_compat import exec_app
    except ImportError:
        try:
            from PyQt5.QtWidgets import QApplication
            from mountfile.qt_compat import exec_app
        except ImportError:
            sys.exit(
                "No Qt bindings found.\n"
                "Windows 10+/Linux:  pip install -r requirements.txt\n"
                "Windows 7:          pip install -r requirements-win7.txt")

    from mountfile.mainwindow import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Mount File")
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.image_path = sys.argv[1]
        win.open_image()
    sys.exit(exec_app(app))


if __name__ == "__main__":
    main()
