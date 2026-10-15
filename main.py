# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Mount File - file manager for raw disk images.

Usage:
    python main.py [disk_image.img]     start the GUI
    python main.py --diag               print environment diagnostics and exit
    python -m mountfile --help          command line interface

Works with PyQt6 (Windows 10/11, Linux, macOS) or PyQt5 (Windows 7).

Any startup error is printed with a full traceback and the console stays
open (double-click friendly) so the message can actually be read.
"""

import sys
import traceback


def _pause_on_windows():
    """Keep the console window open after a fatal error (double-click)."""
    if sys.platform.startswith("win"):
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


def diagnostics():
    """Step-by-step import checks; returns 0 when the GUI can start."""
    ok = True
    print("Python: %s (%s)" % (sys.version.split()[0], sys.executable))
    print("Platform: %s" % sys.platform)

    try:
        from mountfile import __version__
        print("mountfile package: OK (v%s)" % __version__)
    except Exception as e:
        ok = False
        print("mountfile package: FAIL -- %s" % e)

    for name in ("PyQt6", "PyQt5"):
        try:
            mod = __import__(name)
            print("%s: OK (%s)" % (name, getattr(mod, "__file__", "?")))
        except ImportError as e:
            print("%s: not installed (%s)" % (name, e))

    try:
        import pytsk3
        print("pytsk3: OK (%s)" % getattr(pytsk3, "__file__", "?"))
    except ImportError as e:
        print("pytsk3: not installed (%s) -- FAT32 images are still browsable "
              "with the built-in engine, other filesystems need libtsk" % e)

    for module in ("mountfile.qt_compat", "mountfile.firmware",
                   "mountfile.fat32", "mountfile.mainwindow"):
        try:
            __import__(module)
            print("%s: OK" % module)
        except Exception as e:
            ok = False
            print("%s: FAIL -- %s" % (module, e))
            traceback.print_exc()

    print("\nDiagnostics %s." % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    if "--diag" in sys.argv:
        code = diagnostics()
        _pause_on_windows()
        sys.exit(code)

    try:
        from mountfile.qt_compat import QApplication, exec_app
        from mountfile.mainwindow import MainWindow
    except Exception as e:
        print("Cannot start the GUI: %s\n"
              "Windows 10+/Linux:  pip install -r requirements.txt\n"
              "Windows 7:          pip install -r requirements-win7.txt\n\n"
              "The command line interface has no Qt dependency:\n"
              "  python -m mountfile --help" % e)
        _pause_on_windows()
        sys.exit(1)

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Mount File")
        win = MainWindow()
        win.show()
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            win.open_image(sys.argv[1])
        sys.exit(exec_app(app))
    except Exception:
        traceback.print_exc()
        _pause_on_windows()
        sys.exit(1)


if __name__ == "__main__":
    main()
