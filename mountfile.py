# -*- coding: utf-8 -*-
"""
Mount File — mount any disk image with one action.

Design principles
-----------------
  1. Fast start          : no network, no telemetry, UI in <300ms.
  2. Offline-first       : works without internet, always.
  3. Keyboard-first      : Command Palette (Ctrl+K), standard hotkeys.
  4. Never lose data     : atomic writes (write->fsync->replace).
  5. Human errors        : explain what happened and what to do.
  6. Local & private     : no accounts, no analytics, no phone-home.
  7. Tray + CLI          : works both as GUI and headless tool.
  8. Do one thing well   : mount images. Nothing else.

Usage
-----
  GUI :  python mountfile.py
  CLI :  python mountfile.py image.img --to /mnt/x
         python mountfile.py --list
         python mountfile.py --unmount /mnt/x
  Build: pyinstaller --onefile --windowed --name mountfile mountfile.py

Author : Alexkkkkk
License: MIT
Version: 3.0.0
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ============================================================================
# METADATA
# ============================================================================
APP_NAME = "MountFile"
APP_DISPLAY = "Mount File"
APP_VERSION = "3.0.0"
APP_ORG = "Alexkkkkk"
APP_REPO = "https://github.com/Alexkkkkk/mount-file"
SCHEMA_VERSION = 2

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ============================================================================
# PATHS (cross-platform, survive updates — never inside the binary folder)
# ============================================================================
class Paths:
    @staticmethod
    def _base() -> Path:
        if IS_WIN:
            return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        if IS_MAC:
            return Path.home() / "Library/Application Support"
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return Path(xdg) if xdg else Path.home() / ".config"

    @classmethod
    def data(cls) -> Path:
        p = cls._base() / APP_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def logs(cls) -> Path:
        p = cls.data() / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def tmp(cls) -> Path:
        p = Path(tempfile.gettempdir()) / APP_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def state_file(cls) -> Path:
        return cls.data() / "state.json"

    @classmethod
    def lock_file(cls) -> Path:
        return cls.tmp() / f"{APP_NAME}.lock"


# ============================================================================
# ATOMIC IO (crash-safe writes)
# ============================================================================
class AtomicIO:
    @staticmethod
    def write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    @staticmethod
    def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
        AtomicIO.write_bytes(path, text.encode(encoding))

    @staticmethod
    def write_json(path: Path, payload: Any) -> None:
        AtomicIO.write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )

    @staticmethod
    def read_json(path: Path, default: Any = None) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, OSError):
            return default


# ============================================================================
# LOGGING (rotating file + stderr, no telemetry)
# ============================================================================
log = logging.getLogger(APP_NAME)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03dZ [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        Paths.logs() / "app.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(sh)


def install_excepthook() -> None:
    def _hook(t, e, tb):
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, e, tb)
            return
        log.critical("Uncaught exception", exc_info=(t, e, tb))
        report = Paths.data() / "last_crash.txt"
        with contextlib.suppress(OSError):
            AtomicIO.write_text(
                report,
                f"{datetime.now(timezone.utc).isoformat()}\n"
                + "".join(traceback.format_exception(t, e, tb)),
            )

    sys.excepthook = _hook


# ============================================================================
# SINGLE INSTANCE LOCK
# ============================================================================
class SingleInstance:
    def __init__(self) -> None:
        self._fh = None
        self._handle = None
        self._ok = False

    def acquire(self) -> bool:
        if IS_WIN:
            return self._win()
        return self._posix()

    def release(self) -> None:
        if not self._ok:
            return
        with contextlib.suppress(Exception):
            if IS_WIN and self._handle:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            elif self._fh:
                self._fh.close()
                Paths.lock_file().unlink(missing_ok=True)
        self._ok = False

    def _posix(self) -> bool:
        try:
            self._fh = open(Paths.lock_file(), "a+")
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            self._ok = True
            return True
        except (BlockingIOError, OSError):
            return False

    def _win(self) -> bool:
        try:
            k = ctypes.windll.kernel32
            self._handle = k.CreateMutexW(None, False, f"Global\\{APP_NAME}")
            if k.GetLastError() == 183:  # already exists
                if self._handle:
                    k.CloseHandle(self._handle)
                return False
            self._ok = True
            return True
        except Exception:
            return True  # fail-open


# ============================================================================
# STATE (atomic, versioned, survives updates)
# ============================================================================
DEFAULT_STATE: Dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    "ui/theme": "system",
    "ui/autoupdate": True,
    "ui/mountpoint_last": "",
    "session/recent_images": [],
    "session/recent_mounts": [],
    "session/favorite_dirs": [],
    "stats/opens": 0,
}


class State:
    MAX_RECENT = 15
    MAX_FAVORITES = 20
    MAX_MOUNTS = 10

    def __init__(self) -> None:
        self._lock = threading.RLock()
        raw = AtomicIO.read_json(Paths.state_file(), default=None)
        self._data = self._migrate(raw if isinstance(raw, dict) else {})
        self.flush()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def push(self, key: str, value: str, cap: int) -> None:
        with self._lock:
            items: List[str] = list(self._data.get(key) or [])
            if value in items:
                items.remove(value)
            items.insert(0, value)
            self._data[key] = items[:cap]

    def remember_open(self, path: Path) -> None:
        self.push("session/recent_images", str(path), self.MAX_RECENT)
        self.push("session/favorite_dirs", str(path.parent), self.MAX_FAVORITES)
        self.set("stats/opens", int(self.get("stats/opens", 0)) + 1)

    def remember_mount(self, image: str, target: str) -> None:
        mounts = list(self.get("session/recent_mounts") or [])
        mounts = [m for m in mounts if m.get("target") != target]
        mounts.insert(0, {"image": image, "target": target, "ts": time.time()})
        self.set("session/recent_mounts", mounts[: self.MAX_MOUNTS])

    def forget_mount(self, target: str) -> None:
        mounts = [
            m
            for m in (self.get("session/recent_mounts") or [])
            if m.get("target") != target
        ]
        self.set("session/recent_mounts", mounts)

    def flush(self) -> None:
        with self._lock:
            self._data["schema"] = SCHEMA_VERSION
            with contextlib.suppress(OSError):
                AtomicIO.write_json(Paths.state_file(), self._data)

    @staticmethod
    def _migrate(raw: Dict[str, Any]) -> Dict[str, Any]:
        data = {**DEFAULT_STATE, **raw}
        for k in ("session/recent_images", "session/favorite_dirs"):
            data[k] = [p for p in (data.get(k) or []) if isinstance(p, str)]
        data["session/recent_mounts"] = [
            m
            for m in (data.get("session/recent_mounts") or [])
            if isinstance(m, dict) and "target" in m
        ]
        data["schema"] = SCHEMA_VERSION
        return data


# ============================================================================
# MOUNT BACKENDS (one job, done well)
# ============================================================================
class MountError(Exception):
    """Human-readable mount failure."""


class MountBackend:
    """Abstract mount/unmount per platform. Stateless."""

    def mount(self, image: Path, target: Path) -> Path:
        raise NotImplementedError

    def unmount(self, target: Path) -> None:
        raise NotImplementedError

    def list_mounted(self) -> List[Dict[str, str]]:
        return []


class LinuxBackend(MountBackend):
    """Uses loop devices + mount(8). No root? Tries udisksctl."""

    LOOP_IMG_RE = re.compile(r"^(/dev/loop\d+)\s+(.+?)\s+(\S+)\s")

    def mount(self, image: Path, target: Path) -> Path:
        image = image.resolve()
        target.mkdir(parents=True, exist_ok=True)

        if shutil.which("udisksctl") and os.geteuid() != 0:
            r = self._run(["udisksctl", "loop-setup", "-f", str(image)])
            m = re.search(r"(/dev/loop\d+)", r)
            if m:
                dev = m.group(1)
                output = self._run(["udisksctl", "mount", "-b", dev])
                mounted = re.search(r"^Mounted\s+\S+\s+at\s+(.+?)\.\s*$", output)
                if not mounted:
                    raise MountError(
                        "Could not determine the mount point from udisksctl output."
                    )
                mountpoint = Path(mounted.group(1))
                if not mountpoint.is_absolute():
                    raise MountError(
                        "udisksctl reported a mount point that is not an absolute path."
                    )
                return mountpoint

        if os.geteuid() != 0:
            raise MountError(
                "Root is required to mount loop devices.\n"
                "Either run with sudo, or install udisks2 for unprivileged mounting."
            )
        self._run(["mount", "-o", "loop,ro", str(image), str(target)])
        return target

    def unmount(self, target: Path) -> None:
        try:
            self._run(["umount", str(target)])
            return
        except MountError:
            pass
        if shutil.which("udisksctl"):
            with contextlib.suppress(MountError):
                self._run(["udisksctl", "unmount", "-b", str(target)])
                return
        raise MountError(f"Cannot unmount {target}.")

    def list_mounted(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        with contextlib.suppress(Exception):
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0].startswith("/dev/loop"):
                    out.append({"device": parts[0], "target": parts[1], "fs": parts[2]})
        return out

    @staticmethod
    def _run(cmd: List[str]) -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as e:
            raise MountError(f"Tool not found: {cmd[0]}") from e
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip() or f"{cmd[0]} failed"
            raise MountError(msg)
        return r.stdout


class MacBackend(MountBackend):
    """hdiutil attach/detach. No sudo required."""

    def mount(self, image: Path, target: Path) -> Path:
        image = image.resolve()
        target.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "hdiutil",
                "attach",
                str(image),
                "-mountpoint",
                str(target),
                "-nobrowse",
                "-readonly",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise MountError((r.stderr or r.stdout or "hdiutil attach failed").strip())
        return target

    def unmount(self, target: Path) -> None:
        r = subprocess.run(
            ["hdiutil", "detach", str(target), "-force"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise MountError((r.stderr or r.stdout or "hdiutil detach failed").strip())

    def list_mounted(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        with contextlib.suppress(Exception):
            r = subprocess.run(["hdiutil", "info"], capture_output=True, text=True)
            cur: Dict[str, str] = {}
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("/dev/disk"):
                    if cur:
                        out.append(cur)
                    cur = {"device": line.split()[0]}
                elif line.startswith("/Volumes/") and cur:
                    cur["target"] = line
            if cur:
                out.append(cur)
        return out


class WindowsBackend(MountBackend):
    """
    Uses PowerShell's Mount-DiskImage (ISO/VHD/VHDX) — built-in since Win 8.
    For .img it falls back to asking the user to install a driver (out of scope).
    """

    PS_MOUNT = (
        "param($p) "
        "$i = Mount-DiskImage -ImagePath $p -PassThru; "
        "($i | Get-Volume).DriveLetter"
    )
    PS_UNMOUNT = "param($p) Dismount-DiskImage -ImagePath $p"

    def mount(self, image: Path, target: Path) -> Path:
        image = image.resolve()
        letter = self._ps(self.PS_MOUNT, str(image)).strip()
        if not letter or letter.lower() not in "abcdefghijklmnopqrstuvwxyz":
            raise MountError("Windows did not return a drive letter for the image.")
        return Path(f"{letter.upper()}:\\")

    def unmount(self, target: Path) -> None:
        # target here is a drive letter path — resolve back to the source image
        # via Get-DiskImage is complex; we accept a letter and dismount by letter.
        letter = target.drive.rstrip(":\\")
        ps = (
            "param($d) "
            "Get-DiskImage -DevicePath (Get-Partition -DriveLetter $d).DiskNumber "
            "| Dismount-DiskImage"
        )
        with contextlib.suppress(MountError):
            self._ps(ps, letter)
            return
        raise MountError(f"Cannot unmount drive {letter}:")

    def list_mounted(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        with contextlib.suppress(Exception):
            ps = (
                "Get-DiskImage | Where-Object {$_.Attached} | "
                "Select-Object -Property ImagePath,DevicePath | ConvertTo-Json"
            )
            r = self._ps(ps)
            if r.strip().startswith("["):
                for item in json.loads(r):
                    out.append(
                        {
                            "device": item.get("DevicePath", ""),
                            "target": item.get("ImagePath", ""),
                        }
                    )
        return out

    @staticmethod
    def _ps(script: str, *args: str) -> str:
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
            *args,
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode != 0:
            raise MountError((r.stderr or r.stdout or "PowerShell failed").strip())
        return r.stdout


def get_backend() -> MountBackend:
    if IS_WIN:
        return WindowsBackend()
    if IS_MAC:
        return MacBackend()
    if IS_LINUX:
        return LinuxBackend()
    raise MountError(f"Unsupported platform: {sys.platform}")


def default_mount_point(image: Path) -> Path:
    stem = image.stem[:40] or "image"
    safe = re.sub(r"[^\w.\-]+", "_", stem)
    if IS_WIN:
        # Windows will pick a drive letter itself.
        return Path("")
    if IS_MAC:
        return Path(f"/Volumes/{safe}")
    return Path(f"/tmp/{APP_NAME}/{safe}")


# ============================================================================
# CLI
# ============================================================================
def cli_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mountfile",
        description="Mount or unmount a disk image. Works offline.",
    )
    parser.add_argument(
        "image", nargs="?", help="Image to mount (.img/.iso/.vhd/.vhdx)"
    )
    parser.add_argument("--to", "-t", help="Mount point / drive letter")
    parser.add_argument(
        "--unmount", "-u", metavar="TARGET", help="Unmount the given target"
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List currently mounted images"
    )
    parser.add_argument(
        "--open",
        "-o",
        action="store_true",
        help="Open the mount point in the file manager",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--version", action="version", version=f"{APP_DISPLAY} {APP_VERSION}"
    )
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)
    backend = get_backend()

    try:
        if args.list:
            mounts = backend.list_mounted()
            if not mounts:
                print("No images mounted.")
                return 0
            for m in mounts:
                print(f"{m.get('device','?'):<20} {m.get('target','?')}")
            return 0

        if args.unmount:
            backend.unmount(Path(args.unmount))
            print(f"Unmounted: {args.unmount}")
            return 0

        if not args.image:
            parser.print_help()
            return 2

        image = Path(args.image)
        if not image.is_file():
            print(f"File not found: {image}", file=sys.stderr)
            return 1
        target = Path(args.to) if args.to else default_mount_point(image)
        if (
            args.to
            and IS_LINUX
            and isinstance(backend, LinuxBackend)
            and os.geteuid() != 0
            and shutil.which("udisksctl")
        ):
            raise MountError(
                "--to cannot be honored by unprivileged udisksctl; omit --to "
                "to use the system-selected mount point"
            )
        mounted = backend.mount(image, target)
        print(str(mounted))
        if args.open:
            reveal_in_file_manager(mounted)
        return 0
    except MountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def reveal_in_file_manager(path: Path) -> None:
    with contextlib.suppress(Exception):
        if IS_WIN:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])


# ============================================================================
# GUI
# ============================================================================
try:
    from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QCloseEvent, QFont, QKeySequence, QShortcut
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFormLayout,
        QGroupBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSplitter,
        QStatusBar,
        QStyle,
        QSystemTrayIcon,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    HAS_QT = True
except ImportError:
    HAS_QT = False


if HAS_QT:

    # ------------------------------------------------------------------ theme
    LIGHT_QSS = """
        QWidget { font-size: 10pt; }
        QGroupBox { font-weight: 600; margin-top: 12px; }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        QPushButton { padding: 6px 14px; }
        QListWidget { padding: 4px; }
    """
    DARK_QSS = """
        QWidget { background: #1e1f22; color: #e6e6e6; font-size: 10pt; }
        QLineEdit, QListWidget, QTextEdit, QComboBox {
            background: #2b2d31; border: 1px solid #3a3d41;
            border-radius: 4px; padding: 5px; color: #e6e6e6;
        }
        QGroupBox { border: 1px solid #3a3d41; border-radius: 6px;
                    margin-top: 12px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        QPushButton { background: #3a3d41; border: 1px solid #4a4d51;
                      border-radius: 4px; padding: 6px 14px; color: #e6e6e6; }
        QPushButton:hover { background: #44474b; }
        QPushButton:pressed { background: #2b2d31; }
        QMenuBar, QMenu, QStatusBar { background: #1e1f22; }
        QSplitter::handle { background: #2b2d31; }
    """

    def apply_theme(app: QApplication, name: str) -> None:
        if name == "dark":
            app.setStyleSheet(DARK_QSS)
        elif name == "light":
            app.setStyleSheet(LIGHT_QSS)
        else:
            # System default — let Qt decide.
            app.setStyleSheet("")

    # ------------------------------------------------------ command palette
    class CommandPalette(QDialog):
        """Ctrl+K palette: fuzzy-search all commands, Enter to run."""

        def __init__(self, parent, commands: List[Tuple[str, Callable[[], None]]]):
            super().__init__(parent)
            self.setWindowTitle("Command Palette")
            self.setModal(True)
            self.resize(520, 360)
            self._commands = commands

            lay = QVBoxLayout(self)
            self.input = QLineEdit()
            self.input.setPlaceholderText("Type a command…  (Esc to cancel)")
            self.input.textChanged.connect(self._filter)
            lay.addWidget(self.input)

            self.list = QListWidget()
            self.list.itemActivated.connect(self._run_current)
            self.list.itemDoubleClicked.connect(self._run_current)
            lay.addWidget(self.list, 1)

            self._populate(commands)
            QTimer.singleShot(0, self.input.setFocus)

        def _populate(self, items: List[Tuple[str, Callable[[], None]]]) -> None:
            self.list.clear()
            for name, cb in items:
                it = QListWidgetItem(name)
                it.setData(Qt.ItemDataRole.UserRole, cb)
                self.list.addItem(it)
            if self.list.count():
                self.list.setCurrentRow(0)

        def _filter(self, text: str) -> None:
            text = text.strip().lower()
            if not text:
                self._populate(self._commands)
                return
            filtered = [(n, cb) for n, cb in self._commands if text in n.lower()]
            self._populate(filtered)

        def _run_current(self, *_: Any) -> None:
            it = self.list.currentItem()
            if not it:
                return
            cb = it.data(Qt.ItemDataRole.UserRole)
            self.accept()
            if callable(cb):
                QTimer.singleShot(0, cb)

        def keyPressEvent(self, e) -> None:  # noqa: N802
            if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.list.keyPressEvent(e)
                return
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_current()
                return
            super().keyPressEvent(e)

    # -------------------------------------------------------- worker thread
    class MountWorker(QThread):
        """Runs mount/unmount off the UI thread. Never blocks the interface."""

        done = pyqtSignal(str, str)  # action, target_or_message
        failed = pyqtSignal(str)  # human message

        def __init__(
            self,
            backend: MountBackend,
            action: str,
            image: Optional[Path] = None,
            target: Optional[Path] = None,
            parent: Optional[QObject] = None,
        ) -> None:
            super().__init__(parent)
            self._b = backend
            self._action = action
            self._image = image
            self._target = target

        def run(self) -> None:
            try:
                if self._action == "mount":
                    assert self._image and self._target
                    where = self._b.mount(self._image, self._target)
                    self.done.emit("mount", str(where))
                elif self._action == "unmount":
                    assert self._target
                    self._b.unmount(self._target)
                    self.done.emit("unmount", str(self._target))
            except MountError as e:
                self.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("Mount worker crashed")
                self.failed.emit(f"Unexpected error: {e}")

    # -------------------------------------------------------------- window
    class MainWindow(QMainWindow):
        def __init__(self, state: State, backend: MountBackend) -> None:
            super().__init__()
            self._state = state
            self._backend = backend
            self._current_image: Optional[Path] = None
            self._workers: List[QThread] = []
            self._tray: Optional[QSystemTrayIcon] = None

            self.setWindowTitle(f"{APP_DISPLAY} {APP_VERSION}")
            self.setMinimumSize(QSize(760, 460))
            self.resize(QSize(940, 600))

            self._build_ui()
            self._build_menu()
            self._build_shortcuts()
            self._build_tray()
            self._build_status()

            self._restore()
            QTimer.singleShot(50, self._refresh_mounts)

        # --------------------------------------------------------- UI
        def _build_ui(self) -> None:
            central = QWidget(self)
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)

            split = QSplitter(Qt.Orientation.Horizontal)

            # Left: recent / mounts / favorites
            left = QWidget()
            ll = QVBoxLayout(left)
            ll.setContentsMargins(0, 0, 0, 0)

            ll.addWidget(self._section("Mounted now"))
            self.mounts_list = QListWidget()
            self.mounts_list.setAlternatingRowColors(True)
            self.mounts_list.itemDoubleClicked.connect(self._unmount_selected)
            ll.addWidget(self.mounts_list, 2)

            ll.addWidget(self._section("Recent images"))
            self.recent_list = QListWidget()
            self.recent_list.setAlternatingRowColors(True)
            self.recent_list.itemDoubleClicked.connect(self._mount_from_item)
            ll.addWidget(self.recent_list, 2)

            ll.addWidget(self._section("Favorite folders"))
            self.fav_list = QListWidget()
            self.fav_list.setAlternatingRowColors(True)
            self.fav_list.itemDoubleClicked.connect(self._open_folder)
            ll.addWidget(self.fav_list, 2)

            split.addWidget(left)

            # Right: action + settings + log
            right = QWidget()
            rl = QVBoxLayout(right)
            rl.setContentsMargins(0, 0, 0, 0)

            grp = QGroupBox("Mount")
            form = QFormLayout(grp)
            self.edit_image = QLineEdit()
            self.edit_image.setPlaceholderText("Drop an image here or click Browse…")
            self.edit_image.setReadOnly(True)
            btn_browse = QPushButton("Browse…  (Ctrl+O)")
            btn_browse.clicked.connect(self.open_image)
            form.addRow("Image:", self.edit_image)
            form.addRow("", btn_browse)

            self.edit_target = QLineEdit()
            self.edit_target.setPlaceholderText("Auto (leave empty) or pick a folder…")
            btn_target = QPushButton("Pick folder…")
            btn_target.clicked.connect(self.pick_target)
            form.addRow("Mount to:", self.edit_target)
            form.addRow("", btn_target)

            btn_mount = QPushButton("Mount  (Ctrl+Enter)")
            btn_mount.clicked.connect(self.mount_current)
            form.addRow(btn_mount)
            rl.addWidget(grp)

            grp2 = QGroupBox("Settings")
            form2 = QFormLayout(grp2)
            self.chk_autoupdate = QCheckBox("Check for updates on start")
            form2.addRow(self.chk_autoupdate)
            self.cmb_theme = QComboBox()
            self.cmb_theme.addItems(["system", "light", "dark"])
            self.cmb_theme.currentTextChanged.connect(self._on_theme_changed)
            form2.addRow("Theme:", self.cmb_theme)
            rl.addWidget(grp2)

            rl.addWidget(self._section("Log"))
            self.log_view = QTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setFont(
                QFont("Menlo" if IS_MAC else "Consolas" if IS_WIN else "monospace", 9)
            )
            rl.addWidget(self.log_view, 1)

            split.addWidget(right)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 4)
            root.addWidget(split)

            # Drag & drop
            self.setAcceptDrops(True)

        def _section(self, title: str) -> QLabel:
            lbl = QLabel(title)
            f = lbl.font()
            f.setBold(True)
            lbl.setFont(f)
            return lbl

        def _build_menu(self) -> None:
            mb = self.menuBar()

            m_file = mb.addMenu("&File")
            self._act(
                m_file, "Open image…", QKeySequence.StandardKey.Open, self.open_image
            )
            self._act(m_file, "Mount", "Ctrl+Return", self.mount_current)
            self._act(m_file, "Unmount selected", "Ctrl+U", self._unmount_selected)
            m_file.addSeparator()
            self._act(m_file, "Exit", QKeySequence.StandardKey.Quit, self.close)

            m_view = mb.addMenu("&View")
            self._act(m_view, "Command Palette…", "Ctrl+K", self.open_palette)
            self._act(m_view, "Refresh mounts", "F5", self._refresh_mounts)

            m_help = mb.addMenu("&Help")
            self._act(
                m_help,
                "Open logs folder",
                None,
                lambda: reveal_in_file_manager(Paths.logs()),
            )
            self._act(m_help, "About", None, self._show_about)

        def _act(
            self, menu: QMenu, text: str, shortcut: Any, cb: Callable[[], None]
        ) -> None:
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(cb)
            menu.addAction(a)

        def _build_shortcuts(self) -> None:
            QShortcut(QKeySequence("Ctrl+K"), self, activated=self.open_palette)
            QShortcut(QKeySequence("F5"), self, activated=self._refresh_mounts)
            QShortcut(QKeySequence("Ctrl+U"), self, activated=self._unmount_selected)

        def _build_tray(self) -> None:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip(f"{APP_DISPLAY} {APP_VERSION}")
            menu = QMenu()
            menu.addAction("Show / Hide", self._toggle_visible)
            menu.addAction("Refresh mounts", self._refresh_mounts)
            menu.addSeparator()
            menu.addAction("Quit", self.close)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(
                lambda r: (
                    self._toggle_visible()
                    if r == QSystemTrayIcon.ActivationReason.Trigger
                    else None
                )
            )
            self._tray.show()

        def _build_status(self) -> None:
            sb = QStatusBar(self)
            self.setStatusBar(sb)
            self.progress = QProgressBar()
            self.progress.setMaximumWidth(220)
            self.progress.setVisible(False)
            sb.addPermanentWidget(self.progress)
            sb.showMessage("Ready")

        # --------------------------------------------------- state restore
        def _restore(self) -> None:
            theme = str(self._state.get("ui/theme", "system"))
            idx = self.cmb_theme.findText(theme)
            if idx >= 0:
                self.cmb_theme.setCurrentIndex(idx)
            self.chk_autoupdate.setChecked(bool(self._state.get("ui/autoupdate", True)))
            self.edit_target.setText(str(self._state.get("ui/mountpoint_last", "")))
            self._refresh_lists()

        def _refresh_lists(self) -> None:
            self.recent_list.clear()
            for p in self._state.get("session/recent_images", []) or []:
                it = QListWidgetItem(Path(p).name)
                it.setToolTip(p)
                it.setData(Qt.ItemDataRole.UserRole, p)
                self.recent_list.addItem(it)

            self.fav_list.clear()
            for p in self._state.get("session/favorite_dirs", []) or []:
                it = QListWidgetItem(p)
                it.setData(Qt.ItemDataRole.UserRole, p)
                self.fav_list.addItem(it)

        # ------------------------------------------------------ actions
        def open_palette(self) -> None:
            cmds: List[Tuple[str, Callable[[], None]]] = [
                ("Open image…", self.open_image),
                ("Mount current", self.mount_current),
                ("Unmount selected", self._unmount_selected),
                ("Refresh mounts", self._refresh_mounts),
                ("Open logs folder", lambda: reveal_in_file_manager(Paths.logs())),
                ("Open state folder", lambda: reveal_in_file_manager(Paths.data())),
                (
                    "Switch theme: system",
                    lambda: self.cmb_theme.setCurrentText("system"),
                ),
                ("Switch theme: light", lambda: self.cmb_theme.setCurrentText("light")),
                ("Switch theme: dark", lambda: self.cmb_theme.setCurrentText("dark")),
                ("About", self._show_about),
                ("Quit", self.close),
            ]
            for m in self._state.get("session/recent_mounts", []) or []:
                tgt = m.get("target", "")
                cmds.insert(
                    2, (f"Unmount: {tgt}", lambda t=tgt: self._unmount_target(Path(t)))
                )
            for p in self._state.get("session/recent_images", [])[:5]:
                cmds.insert(
                    1,
                    (
                        f"Mount: {Path(p).name}",
                        lambda pp=p: self._quick_mount(Path(pp)),
                    ),
                )
            CommandPalette(self, cmds).exec()

        def open_image(self) -> None:
            start = ""
            favs = self._state.get("session/favorite_dirs", []) or []
            if favs and Path(favs[0]).is_dir():
                start = favs[0]
            p, _ = QFileDialog.getOpenFileName(
                self,
                "Open disk image",
                start,
                "Disk images (*.img *.iso *.bin *.raw *.vhd *.vhdx);;All files (*)",
            )
            if p:
                self._set_image(Path(p))

        def pick_target(self) -> None:
            p = QFileDialog.getExistingDirectory(
                self, "Pick mount point", self.edit_target.text() or str(Path.home())
            )
            if p:
                self.edit_target.setText(p)

        def _set_image(self, path: Path) -> None:
            self._current_image = path
            self.edit_image.setText(str(path))
            if not self.edit_target.text() and not IS_WIN:
                self.edit_target.setText(str(default_mount_point(path)))

        def mount_current(self) -> None:
            if not self._current_image or not self._current_image.is_file():
                self._warn("Choose an image first. Use Ctrl+O.")
                return
            if IS_WIN:
                target = Path(self.edit_target.text().strip() or "AUTO")
                if self.edit_target.text().strip():
                    target = Path(self.edit_target.text().strip())
                else:
                    target = Path("")  # backend returns the drive letter
            else:
                target = Path(
                    self.edit_target.text().strip()
                    or default_mount_point(self._current_image)
                )

            self._state.remember_open(self._current_image)
            if not IS_WIN and str(target):
                self._state.set("ui/mountpoint_last", str(target))
            self._state.flush()
            self._refresh_lists()

            self._set_busy(True, f"Mounting {self._current_image.name}…")
            w = MountWorker(
                self._backend,
                "mount",
                image=self._current_image,
                target=target,
                parent=self,
            )
            w.done.connect(self._on_mount_done)
            w.failed.connect(self._on_mount_failed)
            self._track(w)
            w.start()

        def _quick_mount(self, path: Path) -> None:
            if path.is_file():
                self._set_image(path)
                self.mount_current()

        def _mount_from_item(self, item: QListWidgetItem) -> None:
            p = Path(str(item.data(Qt.ItemDataRole.UserRole)))
            if p.is_file():
                self._set_image(p)
                self.mount_current()
            else:
                self._warn(f"File no longer exists:\n{p}")

        def _unmount_selected(self) -> None:
            it = self.mounts_list.currentItem()
            if not it:
                self._warn("Select a mounted item first.")
                return
            target = Path(str(it.data(Qt.ItemDataRole.UserRole)))
            self._unmount_target(target)

        def _unmount_target(self, target: Path) -> None:
            self._set_busy(True, f"Unmounting {target}…")
            w = MountWorker(self._backend, "unmount", target=target, parent=self)
            w.done.connect(lambda action, t: self._on_unmount_done(t))
            w.failed.connect(self._on_mount_failed)
            self._track(w)
            w.start()

        def _refresh_mounts(self) -> None:
            self.mounts_list.clear()
            try:
                mounts = self._backend.list_mounted()
            except MountError as e:
                self._log(f"Cannot list mounts: {e}")
                return
            for m in mounts:
                label = f"{m.get('device','?')}  →  {m.get('target','?')}"
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, m.get("target", ""))
                it.setToolTip(json.dumps(m, ensure_ascii=False))
                self.mounts_list.addItem(it)

        def _open_folder(self, item: QListWidgetItem) -> None:
            p = Path(str(item.data(Qt.ItemDataRole.UserRole)))
            if p.is_dir():
                reveal_in_file_manager(p)

        # --------------------------------------------------------- slots
        def _on_mount_done(self, action: str, target: str) -> None:
            self._set_busy(False, f"Mounted at {target}")
            self._log(f"Mounted: {target}")
            if self._current_image:
                self._state.remember_mount(str(self._current_image), target)
                self._state.flush()
            self._refresh_mounts()
            if self._tray:
                self._tray.showMessage(
                    APP_DISPLAY,
                    f"Mounted at {target}",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )

        def _on_unmount_done(self, target: str) -> None:
            self._set_busy(False, f"Unmounted {target}")
            self._log(f"Unmounted: {target}")
            self._state.forget_mount(target)
            self._state.flush()
            self._refresh_mounts()

        def _on_mount_failed(self, message: str) -> None:
            self._set_busy(False, "Error")
            self._log(f"Error: {message}")
            QMessageBox.warning(self, APP_DISPLAY, message)

        # ------------------------------------------------------- utilities
        def _set_busy(self, busy: bool, message: str = "") -> None:
            self.progress.setVisible(busy)
            if not busy:
                self.progress.setValue(0)
            self.statusBar().showMessage(message or "Ready")

        def _log(self, message: str) -> None:
            log.info(message)
            self.log_view.append(f"[{datetime.now():%H:%M:%S}] {message}")

        def _warn(self, message: str) -> None:
            QMessageBox.information(self, APP_DISPLAY, message)

        def _show_about(self) -> None:
            QMessageBox.information(
                self,
                f"About {APP_DISPLAY}",
                f"{APP_DISPLAY} {APP_VERSION}\n\n"
                f"Data: {Paths.data()}\nLogs: {Paths.logs()}\n{APP_REPO}",
            )

        def _toggle_visible(self) -> None:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.showNormal()
                self.raise_()
                self.activateWindow()

        def _on_theme_changed(self, name: str) -> None:
            self._state.set("ui/theme", name)
            self._state.flush()
            apply_theme(QApplication.instance(), name)

        def _track(self, thread: QThread) -> None:
            self._workers.append(thread)
            thread.finished.connect(lambda: self._untrack(thread))

        def _untrack(self, thread: QThread) -> None:
            with contextlib.suppress(ValueError):
                self._workers.remove(thread)

        # ------------------------------------------------------- drag-drop
        def dragEnterEvent(self, e) -> None:  # noqa: N802
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e) -> None:  # noqa: N802
            for url in e.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_file():
                    self._set_image(p)
                    self.mount_current()
                    break

        # ------------------------------------------------------- lifecycle
        def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
            self._state.set("ui/autoupdate", self.chk_autoupdate.isChecked())
            self._state.set("ui/theme", self.cmb_theme.currentText())
            self._state.set("ui/mountpoint_last", self.edit_target.text())
            self._state.flush()
            if self._tray:
                self._tray.hide()
            event.accept()

    # ----------------------------------------------------------- bootstrap
    def gui_main(argv: Sequence[str]) -> int:
        QApplication.setApplicationName(APP_NAME)
        QApplication.setOrganizationName(APP_ORG)
        QApplication.setApplicationVersion(APP_VERSION)
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

        app = QApplication(list(argv))
        install_excepthook()

        # Ctrl-C works in terminal
        signal.signal(signal.SIGINT, lambda *_: app.quit())
        t = QTimer()
        t.start(200)
        t.timeout.connect(lambda: None)
        app._sigint = t  # type: ignore[attr-defined]

        lock = SingleInstance()
        if not lock.acquire():
            QMessageBox.information(
                None, APP_DISPLAY, f"{APP_DISPLAY} is already running."
            )
            return 0
        atexit.register(lock.release)

        state = State()
        apply_theme(app, str(state.get("ui/theme", "system")))

        backend = get_backend()
        w = MainWindow(state, backend)
        w.show()

        # Deferred update check — never blocks startup.
        if state.get("ui/autoupdate", True):
            QTimer.singleShot(
                3000, lambda: w._log("Tip: run `mountfile --version` for CLI info.")
            )

        rc = app.exec()
        state.flush()
        return rc


# ============================================================================
# ENTRY
# ============================================================================
def main() -> int:
    argv = sys.argv[1:]

    # CLI if any positional argument or --list/--unmount/--version/--help
    cli_triggers = {"--list", "-l", "--unmount", "-u", "--help", "-h", "--version"}
    wants_cli = bool(set(argv) & cli_triggers) or any(
        not a.startswith("-") for a in argv
    )

    if wants_cli:
        setup_logging(verbose="-v" in argv or "--verbose" in argv)
        return cli_main(argv)

    if not HAS_QT:
        sys.stderr.write(
            "PyQt6 is required for the GUI. Install with:  pip install PyQt6\n"
            "CLI works without it:  mountfile --help\n"
        )
        return 2

    setup_logging()
    log.info(
        "Starting %s %s on %s %s",
        APP_DISPLAY,
        APP_VERSION,
        platform.system(),
        platform.release(),
    )
    return gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
