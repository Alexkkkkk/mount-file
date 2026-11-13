# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Opt-in transactional writers for native filesystem images.

The writers are deliberately disabled by default.  When enabled, they never
modify the source image in place: a temporary sibling copy is checked,
modified, checked again, fsynced, and atomically replaced.  Any unavailable
helper, unsafe image state, unsupported offset, invalid path, or non-zero
verification result fails closed.
"""
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import PurePosixPath

from mountfile.journal_safety import (
    Ext4JournalManager,
    JournalSafetyError,
    NTFSLogFileManager,
)

ENABLE_ENV = "MOUNTFILE_ENABLE_NATIVE_WRITE"
ENABLE_VALUE = "1"


class NativeWriteError(RuntimeError):
    """Base error returned by guarded native writers."""


class NativeWriteDisabled(NativeWriteError):
    """The explicit opt-in flag was not provided."""


class NativeWriteUnsafe(NativeWriteError):
    """The image or requested operation did not pass safety checks."""


def native_write_enabled() -> bool:
    """Return whether the user explicitly enabled experimental native writes."""

    return os.environ.get(ENABLE_ENV) == ENABLE_VALUE


def validate_internal_path(path: str) -> str:
    """Validate and normalize a filesystem-internal POSIX path.

    The external helper commands receive this value as one argument and never
    through a shell.  Traversal, NUL bytes, empty components, and absolute
    host paths are rejected to prevent image/host path confusion.
    """

    if not isinstance(path, str) or "\x00" in path:
        raise NativeWriteUnsafe("filesystem path is not a valid text path")
    normalized = path.replace("\\", "/")
    raw_parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
        or ":" in raw_parts[0]
    ):
        raise NativeWriteUnsafe("filesystem path must be relative and traversal-free")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise NativeWriteUnsafe("filesystem path must be relative and traversal-free")
    value = pure.as_posix()
    if not value or value == ".":
        raise NativeWriteUnsafe("filesystem path must name a file")
    return value


def _require_enabled() -> None:
    if not native_write_enabled():
        raise NativeWriteDisabled(
            f"native write is disabled; set {ENABLE_ENV}=1 for the experimental writer"
        )


def _require_zero_offset(offset: int) -> None:
    if int(offset) != 0:
        raise NativeWriteUnsafe("partition offsets are not supported by this writer")


def _payload_file(data: bytes, directory: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".mountfile-payload-",
        suffix=".bin",
        dir=directory,
        delete=False,
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return handle.name


def _image_copy(image_path: str) -> tuple[str, str]:
    source = os.path.abspath(os.fspath(image_path))
    if not os.path.isfile(source):
        raise NativeWriteUnsafe("image path is not a regular file")
    directory = os.path.dirname(source) or "."
    fd, temporary = tempfile.mkstemp(
        prefix=".mountfile-image-", suffix=".tmp", dir=directory
    )
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
    except Exception:
        _unlink(temporary)
        raise
    return source, temporary


def _unlink(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _run(
    command: Sequence[str], *, allowed: set[int] = frozenset({0}), timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise NativeWriteUnsafe(f"required helper failed: {exc}") from exc
    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout or "").strip()[-1000:]
        raise NativeWriteUnsafe(
            f"helper returned {result.returncode}: {' '.join(command[:2])}; {detail}"
        )
    return result


def _require_tools(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise NativeWriteDisabled(
            f"required native tools are unavailable: {', '.join(missing)}"
        )


def _fsync_path(path: str) -> None:
    """Flush a finished temporary file before it is renamed over the source.

    Windows requires a writable handle for ``FlushFileBuffers``; opening the
    copy read-only raises ``OSError: [Errno 9] Bad file descriptor``.  The
    handle is therefore opened read-write first, and a platform that refuses
    even that degrades to no explicit flush instead of failing the write.
    """

    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError:
            return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_replace(source: str, temporary: str) -> None:
    _fsync_path(temporary)
    os.replace(temporary, source)
    if os.name == "nt":
        return
    directory = os.path.dirname(source) or "."
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Ext4TransactionalWriter:
    """Write one existing ext4 file through debugfs on a verified copy.

    ``e2fsck -fy`` performs journal recovery on the temporary copy only;
    ``e2fsck -fn`` must then report a clean image before replacement.

    ``debugfs write`` creates a regular file when the destination does not
    exist, so ``create_file`` exposes that operation explicitly while keeping
    the same journal gate and atomic publication path.
    """

    def write_file(
        self, image_path: str, path: str, data: bytes, offset: int = 0
    ) -> None:
        _require_enabled()
        _require_zero_offset(offset)
        _require_tools("e2fsck", "debugfs")
        source = os.path.abspath(os.fspath(image_path))
        directory = os.path.dirname(source) or "."
        internal = validate_internal_path(path)
        payload = _payload_file(data, directory)
        source_path, temporary = _image_copy(source)
        try:
            journal = Ext4JournalManager(temporary)
            try:
                journal.replay_on_copy(temporary, _run)
            except JournalSafetyError as exc:
                raise NativeWriteUnsafe(str(exc)) from exc
            command = f"write {shlex.quote(payload)} {shlex.quote(internal)}"
            _run(["debugfs", "-w", "-R", command, temporary])
            _run(["e2fsck", "-fn", temporary])
            try:
                journal.require_clean(temporary)
            except JournalSafetyError as exc:
                raise NativeWriteUnsafe(str(exc)) from exc
            _atomic_replace(source_path, temporary)
            temporary = ""
        finally:
            _unlink(payload)
            _unlink(temporary)

    def create_file(
        self, image_path: str, path: str, data: bytes = b"", offset: int = 0
    ) -> None:
        """Create or replace a regular file on a verified disposable copy."""

        self.write_file(image_path, path, data, offset)


class NtfsTransactionalWriter:
    """Write one existing NTFS file through ntfscp on a verified copy."""

    def write_file(
        self, image_path: str, path: str, data: bytes, offset: int = 0
    ) -> None:
        _require_tools("ntfsfix", "ntfscp")
        _require_enabled()
        _require_zero_offset(offset)
        internal = validate_internal_path(path)
        source = os.path.abspath(os.fspath(image_path))
        source_path, temporary = _image_copy(source)
        payload = _payload_file(data, os.path.dirname(source) or ".")
        try:
            log_file = NTFSLogFileManager()
            try:
                log_file.require_clean(temporary, _run)
            except JournalSafetyError as exc:
                raise NativeWriteUnsafe(str(exc)) from exc
            _run(["ntfscp", "-f", temporary, payload, internal])
            try:
                log_file.require_clean(temporary, _run)
            except JournalSafetyError as exc:
                raise NativeWriteUnsafe(str(exc)) from exc
            _atomic_replace(source_path, temporary)
            temporary = ""
        finally:
            _unlink(payload)
            _unlink(temporary)


__all__ = [
    "ENABLE_ENV",
    "Ext4TransactionalWriter",
    "NativeWriteDisabled",
    "NativeWriteError",
    "NativeWriteUnsafe",
    "NtfsTransactionalWriter",
    "native_write_enabled",
    "validate_internal_path",
]
