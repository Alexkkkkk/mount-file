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
from collections import OrderedDict
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from mountfile.safety import RefusalCode, RefusalError, checked_range

_READ_CHUNK_SIZE = 1 << 20

# Default read-through cache budget shared by ``open_image_source``.  Browsing
# an image re-reads the same boot sectors, FAT copies and MFT records thousands
# of times; 8 MiB is enough to absorb that without hiding a large image in RAM.
DEFAULT_CACHE_BYTES = 8 << 20
CACHE_BYTES_ENV = "MOUNTFILE_IMAGE_CACHE_MB"


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

    def __enter__(self) -> ImageSource: ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...


class FileImageSource:
    """Thread-safe read-only source backed by a single open file descriptor.

    Uses ``os.pread`` where available so concurrent reads do not share a file
    cursor. Platforms without ``pread`` use a lock around seek/read. This class
    detects a truncation as a short-read refusal; it does not claim to prevent
    external writers from modifying a file while it is being read.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        # O_CLOEXEC keeps the evidence handle out of child processes; the
        # POSIX access hint keeps readahead from wasting IO on random metadata.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        self._fd = os.open(os.fspath(self.path), flags)
        if hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(self._fd, 0, 0, os.POSIX_FADV_RANDOM)
            except OSError:
                pass
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
        self._initial_fingerprint = _fingerprint_of(self._fd)

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def fingerprint(self) -> SourceFingerprint:
        """Return the identity snapshot of the opened file.

        A forensic read should be able to show that the bytes it analysed are
        the bytes it opened.  ``fingerprint`` records device/inode/size/mtime
        at open time; ``unchanged`` re-stats the *same* descriptor to detect an
        in-place rewrite or truncation that a short read alone would miss.
        """
        if self._fd < 0:
            raise ImageSourceError("image source is closed", RefusalCode.UNSAFE)
        return _fingerprint_of(self._fd)

    def unchanged(self) -> bool:
        """Return True when the file identity still matches ``fingerprint``.

        This is a best-effort integrity signal, not a lock: it reports a change
        but cannot prevent one, and a same-size rewrite inside the timestamp
        granularity may go unnoticed.
        """
        if self._initial_fingerprint is None:
            return False
        try:
            return _fingerprint_of(self._fd) == self._initial_fingerprint
        except (OSError, ImageSourceError):
            return False

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


def _aligned_read_window(
    offset: int, length: int, lower_bound: int, upper_bound: int, sector_size: int
) -> tuple[int, int, int]:
    """Return a sector-aligned physical read window and requested-byte slice."""
    checked_range(
        offset,
        length,
        lower=lower_bound,
        upper=upper_bound,
        context="raw physical-drive read exceeds selected partition bound",
        error_type=ImageSourceError,
    )
    if not length:
        return offset, 0, 0
    aligned_start = offset - offset % sector_size
    end = offset + length
    aligned_end = ((end + sector_size - 1) // sector_size) * sector_size
    if aligned_start < lower_bound or aligned_end > upper_bound:
        raise ImageSourceError(
            "sector-aligned raw read would cross the selected partition bound",
            RefusalCode.RANGE_OUT_OF_BOUNDS,
        )
    return aligned_start, aligned_end - aligned_start, offset - aligned_start


class WindowsRawDeviceImageSource:
    """Read-only random access to an explicitly bounded Windows physical disk.

    ``lower_bound`` and ``size`` bound reads to the selected partition within
    the physical disk. Requests are expanded to sector-aligned reads and then
    sliced back to the exact requested bytes. The aligned read must remain
    inside the selected partition; positioned reads are serialized because the
    Win32 file pointer belongs to the shared device handle.
    """

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_BEGIN = 0
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(
        self,
        device_path: str,
        *,
        size: int,
        lower_bound: int = 0,
        sector_size: int = 512,
    ):
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
        if type(lower_bound) is not int or lower_bound < 0 or lower_bound >= size:
            raise ImageSourceError(
                "raw Windows source lower bound is invalid",
                RefusalCode.RANGE_OUT_OF_BOUNDS,
            )
        if (
            type(sector_size) is not int
            or sector_size < 512
            or sector_size > (1 << 20)
            or sector_size & (sector_size - 1)
        ):
            raise ImageSourceError(
                "raw Windows source sector size is invalid",
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
        self._lower_bound = lower_bound
        self._sector_size = sector_size
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
        aligned_start, aligned_length, requested_start = _aligned_read_window(
            offset,
            length,
            self._lower_bound,
            self._size,
            self._sector_size,
        )
        if self._handle is None:
            raise ImageSourceError(
                "raw physical-drive source is closed", RefusalCode.UNSAFE
            )
        if not aligned_length:
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
                ctypes.c_longlong(aligned_start),
                ctypes.byref(new_position),
                self._FILE_BEGIN,
            ):
                raise self._win_error("seeking raw physical drive failed")
            if new_position.value != aligned_start:
                raise ImageSourceError(
                    "raw physical-drive seek returned an unexpected position",
                    RefusalCode.CONCURRENT_CHANGE,
                )
            while len(output) < aligned_length:
                chunk_size = min(_READ_CHUNK_SIZE, aligned_length - len(output))
                chunk_size -= chunk_size % self._sector_size
                if chunk_size <= 0:
                    raise ImageSourceError(
                        "raw physical-drive read is smaller than one sector",
                        RefusalCode.RANGE_OUT_OF_BOUNDS,
                    )
                storage = ctypes.create_string_buffer(
                    chunk_size + self._sector_size - 1
                )
                address = ctypes.addressof(storage)
                aligned_address = (address + self._sector_size - 1) & ~(
                    self._sector_size - 1
                )
                bytes_read = wintypes.DWORD(0)
                if not self._kernel32.ReadFile(
                    self._handle,
                    ctypes.c_void_p(aligned_address),
                    chunk_size,
                    ctypes.byref(bytes_read),
                    None,
                ):
                    raise self._win_error(
                        "reading raw physical drive failed at "
                        f"offset {aligned_start + len(output)} "
                        f"for {chunk_size} bytes"
                    )
                if bytes_read.value != chunk_size:
                    raise ImageSourceError(
                        "raw physical-drive read was short or invalid",
                        RefusalCode.CONCURRENT_CHANGE,
                    )
                output.extend(ctypes.string_at(aligned_address, bytes_read.value))
        return bytes(output[requested_start : requested_start + length])

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            if not self._kernel32.CloseHandle(handle):
                raise self._win_error("closing raw physical drive failed")

    def __enter__(self) -> WindowsRawDeviceImageSource:
        if self.closed:
            raise ImageSourceError(
                "raw physical-drive source is closed", RefusalCode.UNSAFE
            )
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


@dataclass(frozen=True)
class SourceFingerprint:
    """Identity snapshot of an opened image file.

    Recorded once at open time so a long analysis can later prove it read the
    same bytes it opened: ``size``/``mtime_ns`` catch a rewrite or truncation,
    and ``device``/``inode`` catch the path being swapped for another file.
    This is a tamper *signal*, not a lock and not a hash of the content.
    """

    size: int
    mtime_ns: int
    device: int | None
    inode: int | None

    def to_dict(self) -> dict[str, int | None]:
        """Return a JSON-serialisable view of this fingerprint."""
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "device": self.device,
            "inode": self.inode,
        }


def _fingerprint_of(fd: int) -> SourceFingerprint:
    """Snapshot the identity of an already-open descriptor."""
    stat = os.fstat(fd)
    mtime_ns = getattr(stat, "st_mtime_ns", None)
    if mtime_ns is None:  # pragma: no cover - very old platforms
        mtime_ns = int(stat.st_mtime * 1_000_000_000)
    return SourceFingerprint(
        size=stat.st_size,
        mtime_ns=int(mtime_ns),
        device=getattr(stat, "st_dev", None),
        inode=getattr(stat, "st_ino", None),
    )


class CachedImageSource:
    """Read-through, byte-budgeted cache in front of another image source.

    Browsing one image re-reads a tiny set of metadata blocks (boot sectors,
    FAT copies, MFT records, directory clusters) thousands of times.  Caching
    those blocks turns a pathological random-IO pattern into a mostly
    sequential one **without changing any parsing decision**: every cached byte
    comes from an exact, bounds-checked read of the wrapped source.

    The cache is fixed-size, self-evicting (LRU) and read-only.  It never
    widens a caller's read past ``parent.size`` and streams long reads straight
    through so bulk copies cannot evict the live metadata set.
    """

    _MIN_BLOCK_SIZE = 512
    _MAX_BLOCK_SIZE = 64 * 1024
    _STREAM_THRESHOLD_BLOCKS = 4

    def __init__(
        self,
        parent: ImageSource,
        *,
        max_bytes: int = DEFAULT_CACHE_BYTES,
        block_size: int = 4096,
        close_parent: bool = True,
    ):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ImageSourceError(
                "image cache budget must be a non-negative integer",
                RefusalCode.UNSAFE,
            )
        if (
            type(block_size) is not int
            or block_size < self._MIN_BLOCK_SIZE
            or block_size > self._MAX_BLOCK_SIZE
            or block_size & (block_size - 1)
        ):
            raise ImageSourceError(
                "image cache block size must be a power of two in 512..65536",
                RefusalCode.UNSAFE,
            )
        self._parent = parent
        self._max_bytes = max_bytes
        self._block_size = block_size
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._cached_bytes = 0
        self._hits = 0
        self._misses = 0
        self._close_parent = close_parent
        self._closed = False
        self._lock = threading.RLock()

    @property
    def size(self) -> int:
        return self._parent.size

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict[str, int | float]:
        """Return cache counters, including the observed hit ratio."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_ratio": round(self._hits / total, 4) if total else 0.0,
                "cached_bytes": self._cached_bytes,
                "max_bytes": self._max_bytes,
                "blocks": len(self._blocks),
                "block_size": self._block_size,
            }

    def clear(self) -> None:
        """Drop every cached block without touching the wrapped source."""
        with self._lock:
            self._blocks.clear()
            self._cached_bytes = 0

    def read_at(self, offset: int, length: int) -> bytes:
        if self._closed:
            raise ImageSourceError("cached image source is closed", RefusalCode.UNSAFE)
        checked_range(
            offset,
            length,
            upper=self._parent.size,
            context="cached image read exceeds image bounds",
            error_type=ImageSourceError,
        )
        if length == 0:
            return b""
        if (
            self._max_bytes == 0
            or length > self._block_size * self._STREAM_THRESHOLD_BLOCKS
        ):
            with self._lock:
                self._misses += 1
            return self._parent.read_at(offset, length)

        block_size = self._block_size
        first = offset // block_size
        last = (offset + length - 1) // block_size
        chunks: list[bytes] = []
        with self._lock:
            if self._closed:
                raise ImageSourceError(
                    "cached image source is closed", RefusalCode.UNSAFE
                )
            for block in range(first, last + 1):
                data = self._blocks.get(block)
                if data is None:
                    self._misses += 1
                    data = self._fill_block(block)
                else:
                    self._hits += 1
                    self._blocks.move_to_end(block)
                chunks.append(data)
        lead = offset - first * block_size
        return b"".join(chunks)[lead : lead + length]

    def _fill_block(self, block: int) -> bytes:
        """Read one aligned block from the parent; caller holds the lock."""
        block_size = self._block_size
        start = block * block_size
        available = self._parent.size - start
        if available <= 0:  # pragma: no cover - guarded by checked_range
            return b""
        wanted = min(block_size, available)
        data = self._parent.read_at(start, wanted)
        if len(data) != wanted:
            raise ImageSourceError(
                "cached image block read was short",
                RefusalCode.CONCURRENT_CHANGE,
            )
        self._store_block(block, data)
        return data

    def _store_block(self, block: int, data: bytes) -> None:
        """Insert one block, evicting least-recently-used blocks as needed."""
        if not data or len(data) > self._max_bytes:
            return
        previous = self._blocks.pop(block, None)
        if previous is not None:
            self._cached_bytes -= len(previous)
        while self._blocks and self._cached_bytes + len(data) > self._max_bytes:
            _evicted_key, evicted = self._blocks.popitem(last=False)
            self._cached_bytes -= len(evicted)
        self._blocks[block] = data
        self._cached_bytes += len(data)

    def fingerprint(self) -> SourceFingerprint:
        """Return the identity snapshot of the wrapped source."""
        delegate = getattr(self._parent, "fingerprint", None)
        if not callable(delegate):
            raise ImageSourceError(
                "wrapped image source does not expose a fingerprint",
                RefusalCode.OPERATION_NOT_SUPPORTED,
            )
        return delegate()

    def unchanged(self) -> bool:
        """Return True when the wrapped source reports an unchanged identity."""
        delegate = getattr(self._parent, "unchanged", None)
        return bool(delegate()) if callable(delegate) else False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._blocks.clear()
            self._cached_bytes = 0
            parent, self._parent = self._parent, None
        if self._close_parent and parent is not None:
            parent.close()

    def __enter__(self) -> CachedImageSource:
        if self._closed:
            raise ImageSourceError("cached image source is closed", RefusalCode.UNSAFE)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def cache_budget_from_env(default: int = DEFAULT_CACHE_BYTES) -> int:
    """Return the cache budget in bytes from ``MOUNTFILE_IMAGE_CACHE_MB``.

    Invalid, negative or unparsable values fall back to ``default`` instead of
    silently disabling the cache or allocating an absurd amount of memory.
    """
    raw = os.environ.get(CACHE_BYTES_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        megabytes = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if megabytes < 0:
        return default
    return megabytes * (1 << 20)


def open_image_source(path: str | os.PathLike[str], *, cache_bytes: int | None = None):
    """Open ``path`` read-only and wrap it in the shared metadata cache.

    ``cache_bytes=0`` returns the bare :class:`FileImageSource`, which is what
    hashing and byte-identity commands want.  ``open_image_source`` is the
    recommended entry point for parsers that walk many small metadata blocks;
    the lower-level classes stay public and unchanged for callers that need an
    exact, uncached view.
    """
    source = FileImageSource(path)
    budget = cache_budget_from_env() if cache_bytes is None else cache_bytes
    try:
        if type(budget) is not int or budget < 0:
            raise ImageSourceError(
                "image cache budget must be a non-negative integer",
                RefusalCode.UNSAFE,
            )
        if budget == 0:
            return source
        return CachedImageSource(source, max_bytes=budget)
    except Exception:
        source.close()
        raise


__all__ = [
    "CACHE_BYTES_ENV",
    "DEFAULT_CACHE_BYTES",
    "BoundedImageSource",
    "CachedImageSource",
    "FileImageSource",
    "ImageSource",
    "ImageSourceError",
    "SourceFingerprint",
    "WindowsRawDeviceImageSource",
    "cache_budget_from_env",
    "open_image_source",
]
