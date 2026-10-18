# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Filesystem-agnostic, read-only inventory of a raw image (issue #87).

One walker shared by the DFIR modules: content search, YARA, timestomp, slack
and live diff all read their data from :class:`FsInventory` instead of talking
to ``pytsk3`` directly, so filesystem specifics live in a single place.

``pytsk3`` stays optional.  It is imported lazily the first time a walk is
requested, and callers may inject a module object (the tests do) so the whole
inventory can be exercised without a real image.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence, Tuple

__all__ = [
    "FileEntry",
    "FsInventory",
    "TskUnavailableError",
    "merge_ranges",
    "subtract_ranges",
]


class TskUnavailableError(RuntimeError):
    """Raised when a filesystem walk is requested without ``pytsk3``."""


def merge_ranges(ranges: Iterable[Tuple[int, int]]) -> list[Tuple[int, int]]:
    """Merge overlapping/adjacent ``(offset, length)`` ranges, sorted."""

    items = sorted((int(o), int(l)) for o, l in ranges if int(l) > 0)
    merged: list[Tuple[int, int]] = []
    for offset, length in items:
        if merged and offset <= merged[-1][0] + merged[-1][1]:
            prev_off, prev_len = merged[-1]
            end = max(prev_off + prev_len, offset + length)
            merged[-1] = (prev_off, end - prev_off)
        else:
            merged.append((offset, length))
    return merged


def subtract_ranges(
    start: int, end: int, occupied: Iterable[Tuple[int, int]]
) -> list[Tuple[int, int]]:
    """Return the gaps of ``[start, end)`` left by ``occupied`` ranges."""

    free: list[Tuple[int, int]] = []
    cursor = int(start)
    for offset, length in merge_ranges(occupied):
        if offset + length <= cursor:
            continue
        if offset >= end:
            break
        if offset > cursor:
            free.append((cursor, offset - cursor))
        cursor = max(cursor, offset + length)
    if cursor < end:
        free.append((cursor, end - cursor))
    return free


@dataclass(frozen=True)
class FileEntry:
    """One regular file found in an image.

    ``allocated_size`` is the space the file really occupies (the sum of its
    data runs), so ``allocated_size - size`` is its slack.  ``fn_*`` fields are
    the ``$FILE_NAME`` timestamps on NTFS and stay ``None`` elsewhere.
    """

    path: str
    size: int
    allocated_size: int
    sha256: str
    runs: Tuple[Tuple[int, int], ...] = ()
    inode: Optional[int] = None
    mtime: Optional[int] = None
    atime: Optional[int] = None
    ctime: Optional[int] = None
    crtime: Optional[int] = None
    fn_mtime: Optional[int] = None
    fn_atime: Optional[int] = None
    fn_ctime: Optional[int] = None
    fn_crtime: Optional[int] = None
    data: Optional[bytes] = field(default=None, repr=False, compare=False)

    @property
    def slack_size(self) -> int:
        return max(0, int(self.allocated_size) - int(self.size))

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def as_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "size": self.size,
            "allocated_size": self.allocated_size,
            "slack_size": self.slack_size,
            "sha256": self.sha256,
            "inode": self.inode,
            "mtime": self.mtime,
            "atime": self.atime,
            "ctime": self.ctime,
            "crtime": self.crtime,
            "fn_mtime": self.fn_mtime,
            "fn_atime": self.fn_atime,
            "fn_ctime": self.fn_ctime,
            "fn_crtime": self.fn_crtime,
        }
        if include_data:
            payload["data"] = self.data
        return payload


class FsInventory:
    """Read-only walker over a raw image.

    ``offset`` is the byte offset of the filesystem inside the image; every
    range reported by this class is an *absolute* image offset, so callers can
    read it straight from the raw file.
    """

    def __init__(
        self,
        image_path: Any,
        *,
        offset: int = 0,
        tsk_module: Any = None,
        read_data: bool = True,
        max_file_size: int = 64 * 1024 * 1024,
    ) -> None:
        self.image_path = Path(image_path)
        self.offset = int(offset)
        self.read_data = bool(read_data)
        self.max_file_size = int(max_file_size)
        self._tsk_module = tsk_module
        self._fs: Any = None

    # ------------------------------------------------------------- plumbing
    def _tsk(self) -> Any:
        if self._tsk_module is None:
            try:
                self._tsk_module = importlib.import_module("pytsk3")
            except Exception as exc:  # pragma: no cover - depends on install
                raise TskUnavailableError(
                    "a filesystem walk needs the optional 'pytsk3' package; "
                    "install it with 'python -m pip install pytsk3' and retry"
                ) from exc
        return self._tsk_module

    def _open(self) -> Any:
        if self._fs is None:
            tsk = self._tsk()
            image = tsk.Img_Info(str(self.image_path))
            self._fs = tsk.FS_Info(image, self.offset)
        return self._fs

    def available(self) -> bool:
        """True when the image can actually be opened as a filesystem."""

        try:
            self._open()
        except Exception:
            return False
        return True

    @property
    def block_size(self) -> int:
        return int(self._open().info.block_size)

    # ------------------------------------------------------------ inventory
    def entries(self) -> Iterator[FileEntry]:
        """Yield every regular file in the image."""

        yield from self._walk(self._open().open_dir("/"), "")

    def _walk(self, directory: Any, prefix: str) -> Iterator[FileEntry]:
        try:
            children = list(directory)
        except Exception:
            return
        for entry in children:
            name = self._entry_name(entry)
            if name in ("", ".", ".."):
                continue
            meta = getattr(getattr(entry, "info", None), "meta", None)
            if meta is None:
                continue
            path = f"{prefix}/{name}"
            kind = int(getattr(meta, "type", 0) or 0)
            if kind == 2:
                try:
                    sub = entry.as_directory()
                except Exception:
                    continue
                yield from self._walk(sub, path)
            elif kind == 1:
                yield self._build_entry(entry, meta, path)

    @staticmethod
    def _entry_name(entry: Any) -> str:
        name = getattr(getattr(entry, "info", None), "name", None)
        raw = getattr(name, "name", b"")
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    def _build_entry(self, entry: Any, meta: Any, path: str) -> FileEntry:
        size = int(getattr(meta, "size", 0) or 0)
        runs = self._data_runs(meta)
        allocated = sum(length for _, length in runs)
        data: Optional[bytes] = None
        if self.read_data and size > 0 and size <= self.max_file_size:
            data = self._read_file(entry, size)
        digest = hashlib.sha256(data).hexdigest() if data is not None else ""
        fn = getattr(getattr(entry, "info", None), "name", None)
        fn_meta = getattr(fn, "meta", None)
        return FileEntry(
            path=path,
            size=size,
            allocated_size=allocated,
            sha256=digest,
            runs=tuple(runs),
            inode=self._meta_addr(meta),
            mtime=self._int_or_none(getattr(meta, "mtime", None)),
            atime=self._int_or_none(getattr(meta, "atime", None)),
            ctime=self._int_or_none(getattr(meta, "ctime", None)),
            crtime=self._int_or_none(getattr(meta, "crtime", None)),
            fn_mtime=self._int_or_none(getattr(fn_meta, "mtime", None)),
            fn_atime=self._int_or_none(getattr(fn_meta, "atime", None)),
            fn_ctime=self._int_or_none(getattr(fn_meta, "ctime", None)),
            fn_crtime=self._int_or_none(getattr(fn_meta, "crtime", None)),
            data=data,
        )

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _meta_addr(meta: Any) -> Optional[int]:
        addr = getattr(meta, "addr", None)
        if addr is None:
            return None
        try:
            return int(addr)
        except (TypeError, ValueError):
            return None

    def _data_runs(self, meta: Any) -> list[Tuple[int, int]]:
        """Absolute ``(offset, length)`` runs of a file's data attribute."""

        block_size = self.block_size
        addr = self._meta_addr(meta)
        if addr is None:
            return []
        try:
            meta_file = self._open().open_meta(inode=addr)
        except Exception:
            return []
        runs: list[Tuple[int, int]] = []
        try:
            attributes = list(meta_file)
        except Exception:
            return []
        for attribute in attributes:
            if int(getattr(attribute, "type", 1) or 1) not in (1, 0):
                continue
            try:
                attribute_runs = list(attribute)
            except Exception:
                continue
            for run in attribute_runs:
                length = int(getattr(run, "len", 0) or 0)
                if length <= 0:
                    continue
                start = int(getattr(run, "addr", 0) or 0) * block_size + self.offset
                runs.append((start, length * block_size))
        return runs

    def _read_file(self, entry: Any, size: int) -> Optional[bytes]:
        try:
            return entry.read_random(0, size)
        except Exception:
            return None

    # --------------------------------------------------------- free space
    def allocated_ranges(self) -> list[Tuple[int, int]]:
        """Merged data runs of every file in the image."""

        return merge_ranges(
            run for item in self.entries() for run in item.runs
        )

    def image_size(self) -> int:
        try:
            return int(self._open().info.img.get_size())
        except Exception:
            return int(Path(self.image_path).stat().st_size)

    def free_ranges(self, *, min_length: int = 1) -> list[Tuple[int, int]]:
        """Free ranges derived from file runs only.

        This is the portable fallback: it never sees filesystem metadata
        (MFT, FAT, inode tables), so it can over-report free space.  For an
        exact map use :func:`mountfile.unallocated.build_unallocated_map`,
        which reads the NTFS ``$Bitmap`` / FAT table when it can.
        """

        total = self.image_size()
        gaps = subtract_ranges(0, total, self.allocated_ranges())
        return [(o, l) for o, l in gaps if l >= min_length]

    def snapshot(self) -> dict[str, FileEntry]:
        """Path -> entry map, handy for diffing two images."""

        return {item.path: item for item in self.entries()}
