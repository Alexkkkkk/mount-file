# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Bounded, read-only DFIR primitives used by the package CLI.

This module deliberately does not mutate images.  It provides raw-byte search,
SHA-256 identity, CSV timeline ingestion and conservative timestomp heuristics.
Filesystem-aware collection, carving, YARA and slack discovery remain optional
layers and are not implied by these primitives.
"""
from __future__ import annotations

import csv
import hashlib
import mmap
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


@dataclass(frozen=True)
class TimelineRecord:
    path: str
    size: Optional[int] = None
    mtime: Optional[float] = None
    atime: Optional[float] = None
    ctime: Optional[float] = None
    birth: Optional[float] = None
    sha256: Optional[str] = None


def _float(value: Any) -> Optional[float]:
    return None if value in (None, "") else float(value)


def _int(value: Any) -> Optional[int]:
    return None if value in (None, "") else int(value)


def read_timeline_csv(path: str | Path) -> Iterator[TimelineRecord]:
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            yield TimelineRecord(
                path=row.get("path") or row.get("Path") or "",
                size=_int(row.get("size")),
                mtime=_float(row.get("mtime")),
                atime=_float(row.get("atime")),
                ctime=_float(row.get("ctime")),
                birth=_float(row.get("birth")),
                sha256=row.get("sha256") or None,
            )


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _context(mm: mmap.mmap, start: int, end: int, context: int) -> str:
    left = max(0, start - context)
    right = min(len(mm), end + context)
    return mm[left:right].decode("utf-8", errors="replace")


def grep_image(
    image_path: str | Path,
    pattern: str,
    *,
    regex: bool = False,
    encoding: str = "utf-8",
    context: int = 64,
    max_hits: int = 1000,
) -> Iterator[dict[str, Any]]:
    """Search an image read-only with bounded results and byte offsets."""
    if max_hits < 1:
        return
    if encoding == "raw":
        needle = pattern.encode("latin-1")
    else:
        needle = pattern.encode(encoding)
    if not needle:
        return
    with open(image_path, "rb") as stream:
        if stream.seek(0, 2) == 0:
            return
        stream.seek(0)
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if regex:
                expression = re.compile(needle)
                iterator = expression.finditer(mm)
            else:
                def literal_matches():
                    position = 0
                    while True:
                        position = mm.find(needle, position)
                        if position < 0:
                            return
                        yield position, position + len(needle)
                        position += 1
                iterator = ((start, end) for start, end in literal_matches())
            for number, match in enumerate(iterator):
                if number >= max_hits:
                    break
                start, end = match.span() if hasattr(match, "span") else match
                yield {
                    "offset": start,
                    "length": end - start,
                    "context": _context(mm, start, end, context),
                }


def detect_timestomp(
    records: Iterable[TimelineRecord], *, skew: float = 2.0
) -> Iterator[tuple[TimelineRecord, list[str]]]:
    """Apply conservative timestamp-order heuristics to supplied records."""
    for record in records:
        reasons: list[str] = []
        if record.mtime is not None and record.ctime is not None:
            if record.mtime + skew < record.ctime:
                reasons.append("mtime < ctime")
        if record.atime is not None and record.mtime is not None:
            if record.atime + skew < record.mtime:
                reasons.append("atime < mtime")
        if record.birth is not None and record.mtime is not None:
            if record.mtime + skew < record.birth:
                reasons.append("mtime < birth")
        if reasons:
            yield record, reasons


__all__ = [
    "TimelineRecord",
    "detect_timestomp",
    "grep_image",
    "read_timeline_csv",
    "sha256_file",
]
