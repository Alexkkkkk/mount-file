# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Mount File - file manager for raw disk images.

Usage:
    python main.py [disk_image.img]     start the GUI
    python main.py --diag               print environment diagnostics and exit
    python main.py --version            print the version and exit
    python main.py --check-updates      check GitHub Releases and exit
    python main.py <command> ...        CLI (volumes/info/ls/get/put/mkdir/mv/rm)
    python -m mountfile --help          full command line help

Works with PyQt6 (Windows 10/11, Linux, macOS) or PyQt5 (Windows 7).

Any startup error is printed with a full traceback and the console stays
open (double-click friendly) so the message can actually be read.

The GUI checks GitHub Releases for a newer build on every start; see
`mountfile/updater.py` and the README for the environment switches.
"""

import sys
import traceback

# --- MOUNTFILE_FIX: OLMA CLI -----------------------------------------------


# --- MOUNTFILE_FIX: startup headroom ---------------------------------------
# Headroom for the frozen exe: PyQt6 + the OLMA hook chain in a windowed
# PyInstaller build can hit ~250 frames during bootstrap. Anything below 2000
# is risky on Python 3.12 frozen, so we bump the limit once at import time.
try:
    import sys as _sys

    if int(_sys.getrecursionlimit()) < 2000:
        _sys.setrecursionlimit(2000)
except Exception:
    pass


# --- MOUNTFILE_FIX: OLMA integration ---------------------------------------
# IMPORTANT: do not install the open() hook at import time. PyInstaller's
# bootloader and Qt's .pyd init open files for their own plumbing; running
# a hook that calls traceback.extract_stack() while they are mid-bootstrap
# is what produces the "Failed to execute script 'main': maximum recursion
# depth exceeded" splash crash on Windows. The hook is installed in main()
# after QApplication exists, on a fresh branch.
_olma = None


def _install_olma_runtime() -> None:
    global _olma
    if _olma is not None:
        return
    try:
        from mountfile.olma import olma

        olma.install(trace_files=True, trace_memory=False)
        _olma = olma
    except Exception as _olma_err:  # noqa: BLE001
        import sys as _sys

        print(f"[OLMA] init warning: {_olma_err}", file=_sys.stderr)


# --- /MOUNTFILE_FIX --------------------------------------------------------
def _olma_main(argv=None) -> int:
    from mountfile.olma.cli import olma_main

    return olma_main(argv)


def _mf_diag() -> int:
    import platform
    import sys as _sys

    try:
        from mountfile import __version__ as _v
    except Exception:
        _v = "unknown"
    print("MountFile diag (OLMA)")
    print(f"  python:   {_sys.version.split()[0]}")
    print(f"  platform: {platform.platform()}")
    print(f"  version:  {_v}")
    if _v != "unknown":
        print(f"  mountfile package: OK (v{_v})")
    else:
        print("  mountfile package: FAIL")
    try:
        from mountfile import qt_compat

        print(f"  qt:       {qt_compat.QT_VERSION}")
        print(f"  qt_compat: OK (QT6={qt_compat.QT6})")
    except Exception as e:
        print(f"  qt:       NOT AVAILABLE ({e!r})")
    try:
        print("  olma:     OK")
    except Exception as e:
        print(f"  olma:     FAIL ({e!r})")
    try:
        import pytsk3

        print(f"  pytsk3:   {getattr(pytsk3, '__version__', 'ok')}")
    except Exception:
        print("  pytsk3:   NOT AVAILABLE")
    return 0


# --- /MOUNTFILE_FIX --------------------------------------------------------
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
    argv = [a for a in sys.argv[1:] if a not in ("--pause", "--no-pause")]
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

    # Keep disk commands independent of Qt, including in frozen builds.
    from mountfile.cli import COMMANDS
    from mountfile.cli import main as cli_main

    if argv and (argv[0] in COMMANDS or argv[0].startswith("-")):
        sys.exit(cli_main(argv))

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
        _install_olma_runtime()
        win = MainWindow()
        win.show()
        # OLMA Watch: слежение за ошибками (opt-in) - после QApplication
        # и до первого взаимодействия пользователя с окном.
        try:
            from mountfile import olma_watch
            olma_watch.install(qt_app=app, log=getattr(win, "log_msg", None))
        except Exception:
            traceback.print_exc()
        if argv and not argv[0].startswith("-"):
            win.open_image(argv[0])
        sys.exit(exec_app(app))
    except Exception:
        traceback.print_exc()
        _pause_on_windows()
        sys.exit(1)


if __name__ == "__main__":
    import sys as _sys

    if "--diag" in _sys.argv:
        raise SystemExit(_mf_diag())
    if "--olma" in _sys.argv:
        idx = _sys.argv.index("--olma")
        raise SystemExit(_olma_main(_sys.argv[idx + 1 :]))
    main()
