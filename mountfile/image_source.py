# SPDX-License-Identifier: MIT
"""Read-only, positioned byte sources for bounded forensic image access.

The source contract intentionally exposes no write method. Reads are exact:
invalid ranges are refused and a short read indicates that the opened image
changed or was truncated, rather than silently returning partial metadata.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Protocol, runtime_checkable

from mountfile.safety import RefusalCode, RefusalError, checked_range

_READ_CHUNK_SIZE = 1 << 20


class ImageSourceError(RefusalError):
    """A positioned image read was invalid, incomplete, or attempted after close."""


@runtime_checkable
class ImageSource(Protocol):
    """Read-only random-access source with a fixed, explicit byte length."""

    @property
    def size(self) -> int:
        """Number of readable bytes in this source."""
        ...

    def read_at(self, offset: int, length: int) -> bytes:
        """Read exactly ``length`` bytes at ``offset`` or raise a refusal."""
        ...

    def close(self) -> None:
        """Release the underlying read-only source handle."""
        ...

    def __enter__(self) -> ImageSource:
        ...

    def __exit__(self, exc_type, exc, traceback) -> None:
        ...


class FileImageSource:
    """Thread-safe read-only source backed by a single open file descriptor.

    Uses ``os.pread`` where available so concurrent reads do not share a file
    cursor. Platforms without ``pread`` use a lock around seek/read. This class
    detects a truncation as a short-read refusal; it does not claim to prevent
    external writers from modifying a file while it is being read.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        self._fd = os.open(os.fspath(self.path), flags)
        try:
            self._size = os.fstat(self._fd).st_size
            if self._size < 0:
                raise ImageSourceError(
                    "image source reports a negative file size",
                    RefusalCode.RANGE_OUT_OF_BOUNDS,
                )
        except Exception:
            os.close(self._fd)
            self._fd = -1
            raise
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def read_at(self, offset: int, length: int) -> bytes:
        checked_range(
            offset,
            length,
            upper=self._size,
            context="image source read exceeds image bounds",
            error_type=ImageSourceError,
        )
        if self._fd < 0:
            raise ImageSourceError("image source is closed", RefusalCode.UNSAFE)
        if length == 0:
            return b""

        output = bytearray()
        with self._lock:
            if self._fd < 0:
                raise ImageSourceError("image source is closed", RefusalCode.UNSAFE)
            while len(output) < length:
                remaining = min(_READ_CHUNK_SIZE, length - len(output))
                absolute = offset + len(output)
                if hasattr(os, "pread"):
                    part = os.pread(self._fd, remaining, absolute)
                else:  # pragma: no cover - exercised on platforms without pread
                    os.lseek(self._fd, absolute, os.SEEK_SET)
                    part = os.read(self._fd, remaining)
                if not part:
                    raise ImageSourceError(
                        "image changed or was truncated during positioned read",
                        RefusalCode.CONCURRENT_CHANGE,
                    )
                output.extend(part)
        return bytes(output)

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                fd, self._fd = self._fd, -1
                os.close(fd)

    def __enter__(self) -> FileImageSource:
        if self.closed:
            raise ImageSourceError("image source is closed", RefusalCode.UNSAFE)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class WindowsRawDeviceImageSource:
    """Read-only random access to an explicitly bounded Windows physical disk.

    ``size`` is the exclusive upper bound in bytes from the start of the disk,
    normally ``partition_offset + partition_size``. The source never queries or
    exposes bytes beyond that bound. Positioned reads are serialized because
    the Win32 file pointer belongs to the shared device handle.
    """

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_BEGIN = 0
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, device_path: str, *, size: int):
        prefix = "\\\\.\\PhysicalDrive"
        suffix = device_path[len(prefix) :] if isinstance(device_path, str) else ""
        if (
            not isinstance(device_path, str)
            or not device_path.startswith(prefix)
            or not suffix
            or not suffix.isascii()
            or not suffix.isdigit()
        ):
            raise ImageSourceError(
                "raw Windows source requires a \\\\.\\PhysicalDriveN path",
                RefusalCode.UNSAFE,
            )
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > (1 << 63) - 1
        ):
            raise ImageSourceError(
                "raw Windows source size is invalid",
                RefusalCode.RANGE_OUT_OF_BOUNDS,
            )
        if os.name != "nt":
            raise ImageSourceError(
                "raw Windows devices are available only on Windows",
                RefusalCode.UNSAFE,
            )

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        self._handle = self._kernel32.CreateFileW(
            device_path,
            self._GENERIC_READ,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None,
            self._OPEN_EXISTING,
            0,
            None,
        )
        if self._handle is None or self._handle == self._INVALID_HANDLE_VALUE:
            raise self._win_error("opening raw physical drive failed")
        self._size = size
        self._lock = threading.Lock()

    def _configure_api(self) -> None:
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        ]
        self._kernel32.SetFilePointerEx.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _win_error(operation: str) -> ImageSourceError:
        error_code = ctypes.get_last_error()
        error = ctypes.WinError(error_code)
        return ImageSourceError(
            f"{operation}: {error}",
            RefusalCode.UNSAFE,
        )

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._handle is None

    def read_at(self, offset: int, length: int) -> bytes:
        checked_range(
            offset,
            length,
            upper=self._size,
            context="raw physical-drive read exceeds selected partition bound",
            error_type=ImageSourceError,
        )
        if self._handle is None:
            raise ImageSourceError("raw physical-drive source is closed", RefusalCode.UNSAFE)
        if length == 0:
            return b""

        output = bytearray()
        with self._lock:
            if self._handle is None:
                raise ImageSourceError(
                    "raw physical-drive source is closed", RefusalCode.UNSAFE
                )
            new_position = ctypes.c_longlong()
            if not self._kernel32.SetFilePointerEx(
                self._handle,
                ctypes.c_longlong(offset),
                ctypes.byref(new_position),
                self._FILE_BEGIN,
            ):
                raise self._win_error("seeking raw physical drive failed")
            if new_position.value != offset:
                raise ImageSourceError(
                    "raw physical-drive seek returned an unexpected position",
                    RefusalCode.CONCURRENT_CHANGE,
                )
            while len(output) < length:
                chunk_size = min(_READ_CHUNK_SIZE, length - len(output))
                buffer = ctypes.create_string_buffer(chunk_size)
                bytes_read = wintypes.DWORD(0)
                if not self._kernel32.ReadFile(
                    self._handle,
                    buffer,
                    chunk_size,
                    ctypes.byref(bytes_read),
                    None,
                ):
                    raise self._win_error("reading raw physical drive failed")
                if bytes_read.value == 0 or bytes_read.value > chunk_size:
                    raise ImageSourceError(
                        "raw physical-drive read was short or invalid",
                        RefusalCode.CONCURRENT_CHANGE,
                    )
                output.extend(buffer.raw[: bytes_read.value])
        return bytes(output)

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            if not self._kernel32.CloseHandle(handle):
                raise self._win_error("closing raw physical drive failed")

    def __enter__(self) -> WindowsRawDeviceImageSource:
        if self.closed:
            raise ImageSourceError("raw physical-drive source is closed", RefusalCode.UNSAFE)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class BoundedImageSource:
    """A strict subrange view which cannot read adjacent image bytes."""

    def __init__(
        self,
        parent: ImageSource,
        start: int,
        length: int,
        *,
        close_parent: bool = False,
    ):
        checked_range(
            start,
            length,
            upper=parent.size,
            context="image source window exceeds parent bounds",
            error_type=ImageSourceError,
        )
        self._parent = parent
        self._start = start
        self._size = length
        self._close_parent = close_parent
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    def read_at(self, offset: int, length: int) -> bytes:
        checked_range(
            offset,
            length,
            upper=self._size,
            context="image source read exceeds selected extent",
            error_type=ImageSourceError,
        )
        if self._closed:
            raise ImageSourceError("image source window is closed", RefusalCode.UNSAFE)
        result = self._parent.read_at(self._start + offset, length)
        if len(result) != length:
            raise ImageSourceError(
                "parent image source returned a short bounded read",
                RefusalCode.CONCURRENT_CHANGE,
            )
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_parent:
            self._parent.close()

    def __enter__(self) -> BoundedImageSource:
        if self._closed:
            raise ImageSourceError("image source window is closed", RefusalCode.UNSAFE)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
