# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Content search inside a raw image without extracting it (Task 1.2, #87).

Two complementary read-only modes:

``search_image``
    Streams the raw bytes of the image in overlapping chunks -- finds strings
    that live in unallocated space, slack space or inside a container, with no
    filesystem parser in the loop.

``search_files``
    Walks the filesystem through pytsk3 and searches the *content* of every
    file, reporting the path each hit belongs to.  Files are read in parallel.

Both modes support plain text, ``utf-16le`` (Windows registry / event logs)
and regular expressions.  Nothing is written to the evidence image.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from .timeline import iter_file_entries

#: Chunk size for the streaming raw search (8 MiB keeps memory flat).
DEFAULT_CHUNK = 8 * 1024 * 1024
#: Overlap so a match straddling a chunk boundary is still found.
DEFAULT_OVERLAP = 64 * 1024

_UTF16_LE = ("utf-16le", "utf16le", "utf-16", "utf16")
_UTF16_BE = ("utf-16be", "utf16be")


class SearchError(RuntimeError):
    """Raised for an unusable search request."""


@dataclass(frozen=True)
class ContentHit:
    """A single read-only content match."""

    offset: int
    length: int
    context: str
    path: Optional[str] = None
    encoding: str = "utf-8"
    exact_offset: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "length": self.length,
            "context": self.context,
            "path": self.path,
            "encoding": self.encoding,
            "exact_offset": self.exact_offset,
        }


def _normalise_encoding(encoding: str) -> str:
    return (encoding or "utf-8").strip().lower().replace("_", "-")


def encode_pattern(pattern: str, encoding: str = "utf-8") -> bytes:
    """Encode a literal pattern into the byte form used inside the image."""

    if not pattern:
        raise SearchError("empty pattern")
    enc = _normalise_encoding(encoding)
    if enc in _UTF16_LE:
        return pattern.encode("utf-16-le")
    if enc in _UTF16_BE:
        return pattern.encode("utf-16-be")
    try:
        return pattern.encode(encoding or "utf-8", "replace")
    except LookupError as exc:
        raise SearchError("unknown encoding: %s" % encoding) from exc


def _context(data: bytes, start: int, end: int, width: int, encoding: str) -> str:
    if width <= 0:
        return ""
    low = max(0, start - width)
    high = min(len(data), end + width)
    snippet = bytes(data[low:high])
    codec = "utf-16-le" if _normalise_encoding(encoding) in _UTF16_LE else encoding
    try:
        text = snippet.decode(codec, "replace")
    except LookupError:
        text = snippet.decode("latin-1", "replace")
    return text.replace("\x00", "")


def _byte_hits(
    data: bytes, needle: bytes, base: int, width: int, encoding: str
) -> Iterator[tuple[int, int]]:
    start = data.find(needle)
    while start != -1:
        yield base + start, len(needle)
        start = data.find(needle, start + 1)


def _regex_hits(data: bytes, rx: Any, base: int) -> Iterator[tuple[int, int]]:
    for match in rx.finditer(data):
        yield base + match.start(), match.end() - match.start()


def _text_regex_hits(
    data: bytes, rx: Any, base: int, encoding: str, char_size: int
) -> Iterator[tuple[int, int, bool]]:
    """Regex over decoded text; offsets are approximate for multi-byte codecs."""

    codec = "utf-16-le" if _normalise_encoding(encoding) in _UTF16_LE else encoding
    try:
        text = data.decode(codec, "ignore")
    except LookupError:
        return
    for match in rx.finditer(text):
        yield base + match.start() * char_size, (
            match.end() - match.start()
        ) * char_size, False


def search_blob(
    data: bytes,
    pattern: str,
    *,
    encoding: str = "utf-8",
    regex: bool = False,
    base_offset: int = 0,
    context: int = 32,
    path: Optional[str] = None,
    max_hits: Optional[int] = None,
) -> list[ContentHit]:
    """Search one in-memory blob; used directly by the file-content mode."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SearchError("data must be bytes-like")
    blob = bytes(data)
    enc = _normalise_encoding(encoding)
    hits: list[ContentHit] = []

    if regex:
        if enc in _UTF16_LE or enc in _UTF16_BE:
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                raise SearchError("invalid regex: %s" % exc) from exc
            size = 2
            pairs = _text_regex_hits(blob, rx, base_offset, enc, size)
            for offset, length, exact in pairs:
                hits.append(
                    ContentHit(
                        offset=offset,
                        length=length,
                        context=_context(
                            blob,
                            offset - base_offset,
                            offset - base_offset + length,
                            context,
                            enc,
                        ),
                        path=path,
                        encoding=enc,
                        exact_offset=exact,
                    )
                )
                if max_hits and len(hits) >= max_hits:
                    break
            return hits
        try:
            rx = re.compile(pattern.encode(encoding or "utf-8", "replace"))
        except (re.error, LookupError) as exc:
            raise SearchError("invalid regex: %s" % exc) from exc
        pairs = _regex_hits(blob, rx, base_offset)
    else:
        needle = encode_pattern(pattern, encoding)
        pairs = _byte_hits(blob, needle, base_offset, context, enc)

    for offset, length in pairs:
        local = offset - base_offset
        hits.append(
            ContentHit(
                offset=offset,
                length=length,
                context=_context(blob, local, local + length, context, enc),
                path=path,
                encoding=enc,
            )
        )
        if max_hits and len(hits) >= max_hits:
            break
    return hits


def search_image(
    image_path: os.PathLike | str,
    pattern: str,
    *,
    encoding: str = "utf-8",
    regex: bool = False,
    chunk: int = DEFAULT_CHUNK,
    overlap: int = DEFAULT_OVERLAP,
    context: int = 32,
    max_hits: Optional[int] = None,
) -> list[ContentHit]:
    """Stream ``image_path`` and return every match, chunked with overlap."""

    path = Path(image_path)
    if not path.exists():
        raise SearchError("image not found: %s" % path)
    if chunk <= 0:
        raise SearchError("chunk must be positive")
    overlap = max(0, min(overlap, chunk - 1)) if chunk > 1 else 0

    size = path.stat().st_size
    hits: list[ContentHit] = []
    seen: set[tuple[int, int]] = set()
    cursor = 0
    with open(path, "rb") as stream:
        while cursor < size:
            stream.seek(cursor)
            data = stream.read(chunk + overlap)
            if not data:
                break
            for hit in search_blob(
                data,
                pattern,
                encoding=encoding,
                regex=regex,
                base_offset=cursor,
                context=context,
            ):
                key = (hit.offset, hit.length)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
                if max_hits and len(hits) >= max_hits:
                    return hits
            if len(data) <= overlap:
                break
            cursor += len(data) - overlap
    return hits


def search_fs(
    fs: Any,
    pattern: str,
    *,
    encoding: str = "utf-8",
    regex: bool = False,
    context: int = 32,
    max_hits: Optional[int] = None,
    workers: int = 4,
    max_file_size: Optional[int] = None,
) -> list[ContentHit]:
    """Search the content of every file in an already-open pytsk3 filesystem."""

    files = list(iter_file_entries(fs))

    def one(item: tuple[str, Any]) -> list[ContentHit]:
        path, entry = item
        size = int(getattr(entry.info.meta, "size", 0) or 0)
        if max_file_size is not None and size > max_file_size:
            return []
        try:
            data = entry.read_random(0, size)
        except Exception:  # pragma: no cover - unreadable file
            return []
        if not data:
            return []
        try:
            return search_blob(
                data,
                pattern,
                encoding=encoding,
                regex=regex,
                context=context,
                path=path,
            )
        except SearchError:
            return []

    results: list[ContentHit] = []
    if files:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for found in pool.map(one, files):
                results.extend(found)
                if max_hits and len(results) >= max_hits:
                    return results[:max_hits]
    return results


def search_files(
    image_path: os.PathLike | str, pattern: str, **kwargs: Any
) -> list[ContentHit]:
    """Filesystem-aware content search over ``image_path`` (needs pytsk3)."""

    from .timeline import open_tsk_fs  # local import keeps pytsk3 optional

    return search_fs(open_tsk_fs(image_path), pattern, **kwargs)


def _load_ranges(source: Any) -> Sequence[tuple[int, int]]:
    if source is None:
        return ()
    if isinstance(source, (str, os.PathLike)):
        import json

        with open(source, encoding="utf-8") as stream:
            payload = json.load(stream)
    else:
        payload = source
    ranges: list[tuple[int, int]] = []
    for item in payload:
        if isinstance(item, dict):
            start = int(item.get("offset", item.get("start", 0)))
            length = int(item.get("length", item.get("len", 0)))
        else:
            start, length = int(item[0]), int(item[1])
        if length > 0:
            ranges.append((start, length))
    return tuple(ranges)


def search_regions(
    image_path: os.PathLike | str,
    ranges: Any,
    pattern: str,
    **kwargs: Any,
) -> list[ContentHit]:
    """Search only the supplied ``(offset, length)`` ranges (e.g. unallocated)."""

    path = Path(image_path)
    hits: list[ContentHit] = []
    with open(path, "rb") as stream:
        for offset, length in _load_ranges(ranges):
            stream.seek(offset)
            data = stream.read(length)
            hits.extend(search_blob(data, pattern, base_offset=offset, **kwargs))
    return hits
