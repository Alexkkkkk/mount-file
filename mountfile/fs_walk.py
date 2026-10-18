# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Minimal read-only walker shared by the issue-#87 DFIR modules.

The timeline, content-search, YARA, slack and timestomp modules all need the
same three things from a filesystem: the list of files with their paths, the
data runs of a file, and the file's bytes.  Those live here so each module only
contains its own logic.

Everything works against a *filesystem-like* object, which is what ``pytsk3``
gives us (``FS_Info``) and what the tests fake: ``info.block_size``,
``open_dir(path)`` and ``open_meta(inode=...)``.  ``pytsk3`` itself is never
imported here, so this module loads fine without it.
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

__all__ = [
    "block_size",
    "entry_data",
    "entry_name",
    "entry_runs",
    "iter_files",
    "merge_ranges",
    "subtract_ranges",
]

FILE_TYPE = 1
DIRECTORY_TYPE = 2
DATA_ATTRIBUTE = 1


def block_size(fs: Any) -> int:
    """Block size of the filesystem, defaulting to 512 when unspecified."""

    try:
        value = int(fs.info.block_size)
    except Exception:
        return 512
    return value if value > 0 else 512


def entry_name(entry: Any) -> str:
    """Decoded name of a directory entry."""

    raw = getattr(getattr(entry, "info", None), "name", None)
    raw = getattr(raw, "name", b"")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def entry_meta(entry: Any) -> Any:
    return getattr(getattr(entry, "info", None), "meta", None)


def entry_data(entry: Any, size: Any = None) -> bytes:
    """Read a file's bytes, returning ``b""`` when the read is not possible."""

    if size is None:
        meta = entry_meta(entry)
        size = int(getattr(meta, "size", 0) or 0)
    try:
        payload = entry.read_random(0, int(size))
    except Exception:
        return b""
    return payload if isinstance(payload, bytes) else bytes(payload or b"")


def iter_files(
    fs: Any, *, prefix: str = ""
) -> Iterator[Tuple[str, Any, Any]]:
    """Yield ``(path, entry, meta)`` for every regular file, depth first."""

    def walk(directory: Any, base: str) -> Iterator[Tuple[str, Any, Any]]:
        try:
            children = list(directory)
        except Exception:
            return
        for entry in children:
            name = entry_name(entry)
            if name in ("", ".", ".."):
                continue
            meta = entry_meta(entry)
            if meta is None:
                continue
            path = "%s/%s" % (base, name)
            if int(getattr(meta, "type", 0) or 0) == DIRECTORY_TYPE:
                try:
                    sub = entry.as_directory()
                except Exception:
                    continue
                yield from walk(sub, path)
            elif int(getattr(meta, "type", 0) or 0) == FILE_TYPE:
                yield path, entry, meta

    yield from walk(fs.open_dir("/"), prefix)


def entry_runs(fs: Any, meta: Any) -> list[Tuple[int, int]]:
    """Data runs of a file as ``(block_address, block_length)`` pairs.

    Resident attributes and unreadable records yield no runs, which is the
    correct answer for slack and unallocated accounting: the data is not
    stored in clusters at all.
    """

    addr = getattr(meta, "addr", None)
    if addr is None:
        return []
    try:
        record = fs.open_meta(inode=int(addr))
    except Exception:
        return []
    try:
        attributes = list(record)
    except Exception:
        return []
    runs: list[Tuple[int, int]] = []
    for attribute in attributes:
        if int(getattr(attribute, "type", DATA_ATTRIBUTE) or 0) != DATA_ATTRIBUTE:
            continue
        try:
            attribute_runs = list(attribute)
        except Exception:
            # Resident data lives inside the MFT record: there are no runs.
            continue
        for run in attribute_runs:
            length = int(getattr(run, "len", 0) or 0)
            if length <= 0:
                continue
            start = int(getattr(run, "addr", 0) or 0)
            if start < 0:
                continue
            runs.append((start, length))
    return runs


def merge_ranges(ranges: Any) -> list[Tuple[int, int]]:
    """Merge overlapping/adjacent ``(start, end)`` ranges, sorted."""

    items = sorted((int(a), int(b)) for a, b in ranges if int(b) > int(a))
    merged: list[Tuple[int, int]] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_ranges(
    start: int, end: int, occupied: Any
) -> list[Tuple[int, int]]:
    """Gaps of ``[start, end)`` left by the ``occupied`` ranges."""

    free: list[Tuple[int, int]] = []
    cursor = int(start)
    for occupied_start, occupied_end in merge_ranges(occupied):
        if occupied_end <= cursor:
            continue
        if occupied_start >= end:
            break
        if occupied_start > cursor:
            free.append((cursor, occupied_start))
        cursor = max(cursor, occupied_end)
    if cursor < end:
        free.append((cursor, end))
    return free
