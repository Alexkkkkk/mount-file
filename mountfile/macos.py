# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only adapters for macOS filesystems.

The HFS+ adapter delegates all filesystem reads to :class:`mountfile.vfs.TskImage`;
therefore it needs a pytsk3/libtsk build with HFS+ support.  APFS is detected from
its on-disk superblock, but is opened only through an external ``apfs-fuse``
executable when that executable is installed.

No adapter mounts anything during construction.  ``APFSFuseAdapter.mount`` is an
explicit operation, requires a caller-provided existing directory, and invokes
``apfs-fuse`` without a shell.  The adapter exposes no write operation.  Neither
adapter reports APFS snapshots: apfs-fuse and the pytsk3 facade used here do not
provide snapshot metadata, so ``snapshot_metadata`` remains ``None``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Union

from . import vfs as _vfs
from .vfs import TskImage

# Keep this module-level alias easy to inspect and monkeypatch in callers/tests.
# The actual pytsk3 import remains optional in mountfile.vfs.
pytsk3 = _vfs.pytsk3

PathLike = Union[str, os.PathLike]

HFS_PLUS_SIGNATURE = b"H+"
HFSX_SIGNATURE = b"HX"
APFS_CONTAINER_SIGNATURE = b"NXSB"
APFS_VOLUME_SIGNATURE = b"APSB"
_APFS_MAGIC_OFFSET = 32
_HFS_HEADER_OFFSET = 1024

__all__ = [
    "APFS_CONTAINER_SIGNATURE",
    "APFS_VOLUME_SIGNATURE",
    "HFS_PLUS_SIGNATURE",
    "HFSX_SIGNATURE",
    "APFSFuseAdapter",
    "APFSAdapter",
    "ApfsFuseAdapter",
    "AdapterError",
    "AdapterUnavailableError",
    "HFSPlusAdapter",
    "HFSAdapter",
    "HfsPlusAdapter",
    "MacOSAdapterError",
    "MountError",
    "UnsupportedFilesystemError",
    "apfs_fuse_available",
    "apfs_fuse_path",
    "detect_filesystem",
    "detect_macos_filesystem",
    "detect_macos_signature",
    "open_macos_adapter",
]


class MacOSAdapterError(RuntimeError):
    """Base class for errors raised by these optional read-only adapters."""


class AdapterUnavailableError(MacOSAdapterError):
    """A required optional dependency or external executable is unavailable."""


class UnsupportedFilesystemError(MacOSAdapterError):
    """The image is not a supported macOS filesystem for the selected adapter."""


class MountError(MacOSAdapterError):
    """An explicit APFS-FUSE mount or unmount operation failed."""


# A short compatibility alias for code that uses the generic spelling.
AdapterError = MacOSAdapterError


def _validate_offset(offset: int) -> int:
    try:
        value = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("offset must be a non-negative integer")
    return value


def detect_macos_signature(data: Union[bytes, bytearray, memoryview]) -> Optional[str]:
    """Return ``"HFS+"`` or ``"APFS"`` for a volume-header byte string.

    ``data`` is relative to the beginning of a filesystem volume.  APFS uses the
    object magic at byte 32 (``NXSB`` for a container or ``APSB`` for a volume),
    while HFS+ stores ``H+`` at byte 1024.  ``HX`` is HFSX, the case-sensitive
    HFS+ variant, and is reported as ``HFS+`` because it uses the same adapter.
    Short or unrecognised data returns ``None`` rather than guessing.
    """

    view = memoryview(data)
    if len(view) >= _APFS_MAGIC_OFFSET + 4:
        marker = bytes(view[_APFS_MAGIC_OFFSET : _APFS_MAGIC_OFFSET + 4])
        if marker in (APFS_CONTAINER_SIGNATURE, APFS_VOLUME_SIGNATURE):
            return "APFS"
    if len(view) >= _HFS_HEADER_OFFSET + 2:
        marker = bytes(view[_HFS_HEADER_OFFSET : _HFS_HEADER_OFFSET + 2])
        if marker in (HFS_PLUS_SIGNATURE, HFSX_SIGNATURE):
            return "HFS+"
    return None


def detect_macos_filesystem(image_path: PathLike, offset: int = 0) -> Optional[str]:
    """Detect HFS+ or APFS at ``offset`` in an image, without mounting it.

    Only the small signature regions are read.  ``offset`` is useful for a
    filesystem inside a partition; APFS-FUSE itself is currently invoked only
    for whole-image inputs by :class:`APFSFuseAdapter`.
    """

    base = _validate_offset(offset)
    with open(image_path, "rb") as stream:
        stream.seek(base + _APFS_MAGIC_OFFSET)
        apfs_marker = stream.read(4)
        if apfs_marker in (APFS_CONTAINER_SIGNATURE, APFS_VOLUME_SIGNATURE):
            return "APFS"
        stream.seek(base + _HFS_HEADER_OFFSET)
        hfs_marker = stream.read(2)
        if hfs_marker in (HFS_PLUS_SIGNATURE, HFSX_SIGNATURE):
            return "HFS+"
    return None


# The generic aliases are intentionally local to this module; they do not alter
# mountfile.firmware's broader filesystem detection API.
detect_filesystem = detect_macos_filesystem


def apfs_fuse_path() -> Optional[str]:
    """Return the ``apfs-fuse`` executable selected from ``PATH``, if any."""

    return shutil.which("apfs-fuse")


def apfs_fuse_available() -> bool:
    """Whether an ``apfs-fuse`` executable is available on ``PATH``."""

    return apfs_fuse_path() is not None


def _require_pytsk3() -> None:
    # Check both aliases so applications/tests can disable the optional backend
    # by changing either the public module alias or mountfile.vfs.pytsk3.
    if pytsk3 is None or _vfs.pytsk3 is None:
        raise AdapterUnavailableError(
            "HFS+ read-only access requires pytsk3 and a libtsk build with HFS+ support"
        )


def _require_hfs_plus(image_path: PathLike, offset: int) -> None:
    detected = detect_macos_filesystem(image_path, offset=offset)
    if detected != "HFS+":
        raise UnsupportedFilesystemError(
            "image does not contain an HFS+ filesystem at offset %d" % offset
        )


class HFSPlusAdapter:
    """Read-only HFS+/HFSX access backed by the existing :class:`TskImage` API.

    This class never mounts the image.  It provides the same ``list_dir``,
    ``file_size``, ``read_file_range`` and ``read_file`` methods as ``TskImage``.
    """

    filesystem = "HFS+"
    read_only = True
    mount_required = False
    requirements = ("pytsk3", "libtsk with HFS+ support")
    snapshots_supported = False
    snapshot_metadata = None

    def __init__(self, image_path: PathLike, offset: int = 0) -> None:
        _require_pytsk3()
        self.image_path = os.fspath(image_path)
        self.offset = _validate_offset(offset)
        _require_hfs_plus(self.image_path, self.offset)
        # TskImage is read-only and already translates pytsk3 errors to TskError.
        self._provider = TskImage(self.image_path, offset=self.offset)

    def list_dir(self, path: str = "/"):
        return self._provider.list_dir(path)

    def file_size(self, path: str) -> int:
        return self._provider.file_size(path)

    def read_file_range(self, path: str, offset: int = 0, length: int = 1 << 20) -> bytes:
        return self._provider.read_file_range(path, offset=offset, length=length)

    def read_file(self, path: str) -> bytes:
        return self._provider.read_file(path)


# Naming variants are harmless aliases and keep the adapter discoverable for
# callers that use normal Python title casing rather than filesystem acronyms.
HFSAdapter = HFSPlusAdapter
HfsPlusAdapter = HFSPlusAdapter


def _format_mtime(value: float) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return ""


class APFSFuseAdapter:
    """Optional read-only APFS access through an installed ``apfs-fuse`` tool.

    Construction performs detection and dependency validation only.  It does not
    start a process or mount anything.  Call ``mount(existing_directory)``
    explicitly before using the file-reading methods, then call ``unmount`` or
    ``close`` when finished.  The external tool accepts a whole image here;
    non-zero partition offsets are rejected instead of being guessed or silently
    ignored.
    """

    filesystem = "APFS"
    read_only = True
    mount_required = True
    requirements = (
        "apfs-fuse on PATH",
        "a working FUSE runtime and permission to mount",
        "an existing caller-provided mount directory",
    )
    snapshots_supported = False
    snapshot_metadata = None

    def __init__(
        self,
        image_path: PathLike,
        offset: int = 0,
        executable: Optional[PathLike] = None,
    ) -> None:
        self.image_path = os.fspath(image_path)
        self.offset = _validate_offset(offset)
        if self.offset:
            raise UnsupportedFilesystemError(
                "APFS-FUSE adapter accepts whole-image input only; slice the partition first"
            )
        self.executable = os.fspath(executable) if executable is not None else apfs_fuse_path()
        if not self.executable:
            raise AdapterUnavailableError(
                "APFS read-only access requires the external apfs-fuse executable on PATH"
            )
        detected = detect_macos_filesystem(self.image_path, offset=0)
        if detected != "APFS":
            raise UnsupportedFilesystemError("image does not contain an APFS filesystem")
        self._process = None  # type: Any
        self._mountpoint: Optional[Path] = None

    @property
    def mounted(self) -> bool:
        """Whether this adapter currently owns an explicit apfs-fuse process."""

        return self._process is not None and self._mountpoint is not None

    @property
    def mountpoint(self) -> Optional[str]:
        return str(self._mountpoint) if self._mountpoint is not None else None

    def mount(self, mountpoint: PathLike, startup_timeout: float = 5.0) -> str:
        """Explicitly mount into an existing absolute directory and return it.

        No ``sudo``, shell, or automatic temporary directory is used.  The
        caller owns the directory itself; this adapter only owns the apfs-fuse
        process and terminates it from :meth:`unmount`.
        """

        if self.mounted:
            raise MountError("APFS image is already mounted")
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")

        requested = Path(mountpoint).expanduser()
        if not requested.is_absolute():
            raise MountError("mountpoint must be an existing absolute directory")
        if not requested.is_dir():
            raise MountError("mountpoint must be an existing directory")
        target = requested.resolve()

        command = [self.executable, self.image_path, str(target)]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise MountError("could not start apfs-fuse: %s" % exc) from exc

        # A foreground FUSE process is the readiness signal.  Check briefly
        # for immediate command failure, but do not make every successful mount
        # wait for the caller's entire timeout.
        deadline = time.monotonic() + min(float(startup_timeout), 0.25)
        while True:
            returncode = process.poll()
            if returncode is not None:
                try:
                    _stdout, stderr = process.communicate(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    stderr = ""
                detail = (stderr or "").strip()
                suffix = ": %s" % detail if detail else ""
                raise MountError(
                    "apfs-fuse exited with status %d%s" % (returncode, suffix)
                )
            # A foreground FUSE process remains alive while the mount is active.
            # There is no portable mount-table API, so process liveness is the
            # only readiness signal that can be checked without another tool.
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        self._process = process
        self._mountpoint = target
        return str(target)

    def unmount(self) -> None:
        """Stop the process started by :meth:`mount`, if it is running."""

        process = self._process
        self._process = None
        self._mountpoint = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except OSError as exc:
            raise MountError("could not stop apfs-fuse: %s" % exc) from exc

    close = unmount

    def __enter__(self):
        if not self.mounted:
            raise MountError("call mount(existing_directory) before entering the adapter")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.unmount()

    def _root(self) -> Path:
        if not self.mounted:
            raise MountError("APFS image is not mounted; call mount(existing_directory) first")
        # The invariant is established by mount(); keep this guard for type
        # checkers and to fail closed if a caller mutates internal state.
        assert self._mountpoint is not None
        return self._mountpoint

    def _safe_path(self, path: str) -> Path:
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        root = self._root().resolve()
        relative = path.lstrip("/")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("path escapes the APFS mountpoint") from exc
        return candidate

    def list_dir(self, path: str = "/"):
        directory = self._safe_path(path)
        try:
            entries = []
            for entry in directory.iterdir():
                # Resolve before inspecting metadata so a symlink cannot make
                # this read-only facade escape the caller-provided mountpoint.
                try:
                    safe_entry = self._safe_path(str(entry.relative_to(self._root())))
                except (ValueError, OSError):
                    continue
                info = safe_entry.stat()
                is_dir = stat.S_ISDIR(info.st_mode)
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else int(info.st_size),
                        "date": "" if is_dir else _format_mtime(info.st_mtime),
                    }
                )
        except OSError as exc:
            raise MountError("cannot list APFS directory %r: %s" % (path, exc)) from exc
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return entries

    def file_size(self, path: str) -> int:
        try:
            return int(self._safe_path(path).stat().st_size)
        except OSError as exc:
            raise MountError("cannot stat APFS file %r: %s" % (path, exc)) from exc

    def read_file_range(self, path: str, offset: int = 0, length: int = 1 << 20) -> bytes:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length <= 0:
            return b""
        target = self._safe_path(path)
        try:
            with target.open("rb") as stream:
                stream.seek(offset)
                return stream.read(length)
        except OSError as exc:
            raise MountError("cannot read APFS file %r: %s" % (path, exc)) from exc

    def read_file(self, path: str) -> bytes:
        return self.read_file_range(path, 0, self.file_size(path))


APFSAdapter = APFSFuseAdapter
ApfsFuseAdapter = APFSFuseAdapter


def open_macos_adapter(image_path: PathLike, offset: int = 0):
    """Return the appropriate optional read-only adapter for an image.

    Detection is side-effect-free.  For APFS, the returned adapter still needs
    an explicit ``mount`` call before file reads; for HFS+ no mount is needed.
    """

    filesystem = detect_macos_filesystem(image_path, offset=offset)
    if filesystem == "HFS+":
        return HFSPlusAdapter(image_path, offset=offset)
    if filesystem == "APFS":
        return APFSFuseAdapter(image_path, offset=offset)
    raise UnsupportedFilesystemError("image is not an HFS+ or APFS filesystem")
