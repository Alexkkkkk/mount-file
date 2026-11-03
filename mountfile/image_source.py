# SPDX-License-Identifier: MIT
"""Read-only, positioned byte sources for bounded forensic image access.

The source contract intentionally exposes no write method. Reads are exact:
invalid ranges are refused and a short read indicates that the opened image
changed or was truncated, rather than silently returning partial metadata.
"""

from __future__ import annotations

import os
import threading
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
