#!/usr/bin/env python3
"""Mount File — file manager for raw disk images.

Usage:
    python main.py [disk_image.img]
"""

import sys


def main():
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        sys.exit("PyQt6 is not installed. Run: pip install -r requirements.txt")

    from mountfile.mainwindow import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Mount File")
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.image_path = sys.argv[1]
        win.open_image()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
