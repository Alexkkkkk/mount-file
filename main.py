# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Mount File - file manager for raw disk images.

Usage:
    python main.py [disk_image.img]     start the GUI
    python main.py --diag               print environment diagnostics and exit
    python main.py --version            print the version and exit
    python main.py --check-updates      check GitHub Releases and exit

Works with PyQt6 (Windows 10/11, Linux, macOS) or PyQt5 (Windows 7).

Any startup error is printed with a full traceback and the console stays
open (double-click friendly) so the message can actually be read.

The GUI checks GitHub Releases for a newer build on every start; see
`mountfile/updater.py` and the README for the environment switches.
"""

import sys
import traceback


def _pause_on_windows():
    """Keep the console window open after a fatal error (double-click).

    Only pauses for an interactive console: in CI (or any piped stdin)
    waiting for Enter would hang the job forever. A windowed PyInstaller
    build has no stdin at all, so it must be checked for None first -
    calling .isatty() on None was the "AttributeError: 'NoneType' object
    has no attribute 'isatty'" crash dialog that hid the real error.
    """
    if not sys.platform.startswith("win"):
        return
    stdin = sys.stdin
    if stdin is None or not hasattr(stdin, "isatty"):
        return
    try:
        interactive = stdin.isatty()
    except (ValueError, OSError):
        interactive = False
    if not interactive:
        return
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


def diagnostics():
    """Step-by-step import checks; returns 0 when the GUI can start."""
    ok = True
    print("Python: %s (%s)" % (sys.version.split()[0], sys.executable))
    print("Platform: %s" % sys.platform)
    print("Frozen build: %s" % bool(getattr(sys, "frozen", False)))

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
        print(
            "pytsk3: not installed (%s) -- read-only browsing of "
            "FAT/NTFS/EXT will not work" % e
        )

    try:
        from mountfile import qt_compat

        print("qt_compat: OK (QT6=%s)" % qt_compat.QT6)
    except Exception as e:
        ok = False
        print("qt_compat: FAIL -- %s" % e)
        traceback.print_exc()

    try:
        from mountfile.hexview import HexView  # noqa: F401

        print("hexview: OK")
    except Exception as e:
        ok = False
        print("hexview: FAIL -- %s" % e)
        traceback.print_exc()

    try:
        from mountfile.tc_window import MainWindow  # noqa: F401

        print("tc_window: OK")
    except Exception as e:
        ok = False
        print("tc_window: FAIL -- %s" % e)
        traceback.print_exc()

    try:
        from mountfile import updater

        print(
            "updater: OK (repo=%s, check-at-start=%s, auto-install=%s)"
            % (
                updater.repo(),
                "off" if updater.disabled() else "on",
                "on" if updater.auto_install() else "ask",
            )
        )
    except Exception as e:
        print("updater: FAIL -- %s" % e)

    print("\nDiagnostics %s." % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def check_updates():
    """`--check-updates`: one synchronous update check, then exit.

    Never raises: a broken network/URL is reported and returns 0 so a
    launcher script does not treat "offline" as a program failure.
    """
    from mountfile import __version__, updater

    print("Installed version: v%s" % __version__)
    print("Update source: %s" % updater.update_url())
    if updater.disabled():
        print("Update checks are disabled (MOUNTFILE_NO_UPDATE).")
        return 0
    try:
        info = updater.check(__version__)
    except Exception as e:
        print("Update check failed: %s" % e)
        return 0
    if not info:
        print("No updates: v%s is the newest release." % __version__)
        return 0
    print("Update available: v%s (you have v%s)" % (info["version"], __version__))
    if info.get("asset"):
        print(
            "  asset: %s (%s bytes)"
            % (info["asset"].get("name"), info["asset"].get("size") or "?")
        )
        print("  sha256: %s" % (info.get("sha256") or "not published"))
    else:
        print("  no build for this platform in the release")
    print("  page: %s" % info.get("url"))
    return 10


def main():
    if "--version" in sys.argv:
        try:
            from mountfile import __version__
        except Exception as e:  # pragma: no cover - broken checkout
            print("mountfile is not importable here: %s" % e)
            sys.exit(1)
        print("Mount File v%s" % __version__)
        return

    if "--check-updates" in sys.argv:
        sys.exit(check_updates())

    if "--diag" in sys.argv:
        code = diagnostics()
        _pause_on_windows()
        sys.exit(code)

    try:
        from PyQt6.QtWidgets import QApplication

        from mountfile.qt_compat import exec_app
    except ImportError:
        try:
            from PyQt5.QtWidgets import QApplication

            from mountfile.qt_compat import exec_app
        except ImportError:
            print(
                "No Qt bindings found.\n"
                "Windows 10+/Linux:  pip install -r requirements.txt\n"
                "Windows 7:          pip install -r requirements-win7.txt"
            )
            _pause_on_windows()
            sys.exit(1)

    try:
        # Importing the GUI module up front keeps a broken install in the
        # traceback path instead of inside a half-built QApplication.
        from mountfile.tc_window import MainWindow
    except Exception:
        traceback.print_exc()
        _pause_on_windows()
        sys.exit(1)

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Mount File")
        win = MainWindow()
        win.show()
        if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
            win.open_image(sys.argv[1])
        sys.exit(exec_app(app))
    except Exception:
        traceback.print_exc()
        _pause_on_windows()
        sys.exit(1)


if __name__ == "__main__":
    main()
