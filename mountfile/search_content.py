# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Content search inside an image (issue #87, Task 1.2).

Plain literals, UTF-16LE (the Windows default for registry hives, ``$UsnJrnl``
and many logs) and regular expressions.  Image reads are chunked with an
overlap so a needle split across a chunk boundary is still found, and the
matches found in the overlap region are de-duplicated.

All reads are read-only and nothing is extracted to disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .fs_walk import entry_data, entry_name, entry_meta, iter_files

__all__ = [
    "ContentHit",
    "SearchError",
    "encode_pattern",
    "search_blob",
    "search_fs",
    "search_files",
    "search_image",
    "search_regions",
]

DEFAULT_CHUNK = 8 * 1024 * 1024
DEFAULT_OVERLAP = 64 * 1024
ENCODINGS = ("utf-8", "utf-16le", "utf-16be", "latin-1", "ascii")


class SearchError(RuntimeError):
    """Raised for an unusable pattern, encoding or image."""


@dataclass(frozen=True)
class ContentHit:
    """One match with its absolute offset and a printable context window."""

    offset: int
    length: int
    context: str
    encoding: str = "utf-8"
    path: str = ""
    region: Optional[tuple[int, int]] = None

    @property
    def source(self) -> str:
        return self.path

    def as_dict(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "length": self.length,
            "context": self.context,
            "encoding": self.encoding,
            "path": self.path,
        }


def _encoding(name: Any) -> str:
    value = str(name or "utf-8").lower().replace("_", "-")
    if value not in ENCODINGS:
        raise SearchError(
            "unsupported encoding %r; expected one of %s" % (name, ", ".join(ENCODINGS))
        )
    return value


def encode_pattern(pattern: Any, encoding: str = "utf-8") -> bytes:
    """Encode a literal pattern the way it is stored on disk."""

    if pattern is None or pattern == "":
        raise SearchError("pattern must not be empty")
    try:
        return str(pattern).encode(_encoding(encoding))
    except (UnicodeEncodeError, LookupError) as exc:
        raise SearchError("cannot encode pattern: %s" % exc) from exc


def _window(data: bytes, start: int, end: int, context: int) -> str:
    left = max(0, start - max(0, int(context)))
    right = min(len(data), end + max(0, int(context)))
    return (
        data[left:right]
        .decode("utf-8", "replace")
        .replace("\x00", ".")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def search_blob(
    data: bytes,
    pattern: Any,
    *,
    encoding: str = "utf-8",
    regex: bool = False,
    context: int = 0,
    base_offset: int = 0,
    path: str = "",
    region: Optional[tuple[int, int]] = None,
    max_hits: Optional[int] = None,
) -> list[ContentHit]:
    """Search one in-memory buffer."""

    name = _encoding(encoding)
    hits: list[ContentHit] = []
    if regex:
        try:
            compiled = re.compile(str(pattern).encode(name))
        except (re.error, UnicodeEncodeError) as exc:
            raise SearchError("invalid regular expression: %s" % exc) from exc
        for match in compiled.finditer(data):
            start, end = match.span()
            if start == end:
                continue
            hits.append(
                ContentHit(
                    offset=int(base_offset) + start,
                    length=end - start,
                    context=_window(data, start, end, context),
                    encoding=name,
                    path=path,
                    region=region,
                )
            )
            if max_hits is not None and len(hits) >= max_hits:
                break
        return hits
    needle = encode_pattern(pattern, name)
    cursor = data.find(needle)
    while cursor != -1:
        hits.append(
            ContentHit(
                offset=int(base_offset) + cursor,
                length=len(needle),
                context=_window(data, cursor, cursor + len(needle), context),
                encoding=name,
                path=path,
                region=region,
            )
        )
        if max_hits is not None and len(hits) >= max_hits:
            break
        cursor = data.find(needle, cursor + len(needle))
    return hits


def _chunks(size: int, chunk: int, overlap: int) -> Iterator[tuple[int, int]]:
    step = max(1, int(chunk))
    over = max(0, int(overlap))
    start = 0
    while start < size:
        yield start, min(step + over, size - start)
        start += step


def search_image(
    image: Any,
    pattern: Any,
    *,
    encoding: str = "utf-8",
    regex: bool = False,
    context: int = 0,
    chunk: Optional[int] = None,
    overlap: Optional[int] = None,
    chunk_size: Optional[int] = None,
    overlap_size: Optional[int] = None,
    max_hits: Optional[int] = None,
    start: int = 0,
    end: Optional[int] = None,
) -> list[ContentHit]:
    """Stream an image in overlapping chunks and return the matches."""

    path = Path(image)
    if not path.is_file():
        raise SearchError("image not found: %s" % image)
    size = path.stat().st_size
    step = int(chunk or chunk_size or DEFAULT_CHUNK)
    over = int(overlap if overlap is not None else (overlap_size or DEFAULT_OVERLAP))
    begin = max(0, int(start))
    stop = size if end is None else min(size, int(end))
    if stop <= begin:
        return []
    seen: set[tuple[int, int]] = set()
    hits: list[ContentHit] = []
    with open(path, "rb") as stream:
        for offset, length in _chunks(stop - begin, step, over):
            stream.seek(begin + offset)
            data = stream.read(length)
            if not data:
                break
            for hit in search_blob(
                data,
                pattern,
                encoding=encoding,
                regex=regex,
                context=context,
                base_offset=begin + offset,
                path=str(path),
            ):
                key = (hit.offset, hit.length)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
                if max_hits is not None and len(hits) >= max_hits:
                    return hits
    hits.sort(key=lambda hit: (hit.offset, hit.length))
    return hits


def search_regions(
    image: Any,
    regions: Iterable[Any],
    pattern: Any,
    **kwargs: Any,
) -> list[ContentHit]:
    """Search only the supplied ``(offset, length)`` regions of an image."""

    path = Path(image)
    if not path.is_file():
        raise SearchError("image not found: %s" % image)
    hits: list[ContentHit] = []
    with open(path, "rb") as stream:
        for region in regions:
            offset, length = _region(region)
            stream.seek(offset)
            data = stream.read(length)
            if not data:
                continue
            found = search_blob(
                data,
                pattern,
                base_offset=offset,
                path=str(path),
                region=(offset, offset + length),
                **kwargs,
            )
            hits.extend(found)
            limit = kwargs.get("max_hits")
            if limit is not None and len(hits) >= limit:
                break
    hits.sort(key=lambda hit: (hit.offset, hit.length))
    return hits


def _region(region: Any) -> tuple[int, int]:
    if isinstance(region, dict):
        return int(region.get("offset", 0)), int(region.get("length", 0))
    if isinstance(region, (tuple, list)) and len(region) >= 2:
        return int(region[0]), int(region[1])
    offset = int(getattr(region, "offset"))
    length = int(getattr(region, "length"))
    return offset, length


def search_fs(fs: Any, pattern: Any, **kwargs: Any) -> list[ContentHit]:
    """Search the contents of every file in a filesystem-like object."""

    hits: list[ContentHit] = []
    limit = kwargs.pop("max_hits", None)
    for path, entry, meta in iter_files(fs):
        size = int(getattr(meta, "size", 0) or 0)
        if size <= 0:
            continue
        data = entry_data(entry, size)
        if not data:
            continue
        hits.extend(search_blob(data, pattern, path=path, **kwargs))
        if limit is not None and len(hits) >= limit:
            return hits[:limit]
    return hits


def search_files(
    image: Any,
    pattern: Any,
    *,
    offset: int = 0,
    **kwargs: Any,
) -> list[ContentHit]:
    """Search the files of an image, opening it with ``pytsk3``.

    An already-open filesystem-like object is accepted as well, which keeps the
    CLI and the tests on the same code path.
    """

    if hasattr(image, "open_dir"):
        return search_fs(image, pattern, **kwargs)
    try:
        import pytsk3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        raise SearchError(
            "searching files needs the optional 'pytsk3' package; install it "
            "with 'python -m pip install pytsk3'"
        ) from exc
    path = Path(image)
    if not path.exists():
        raise SearchError("image not found: %s" % image)
    try:
        img = pytsk3.Img_Info(str(path))
        fs = pytsk3.FS_Info(img, int(offset))
    except Exception as exc:
        raise SearchError("cannot open %s as a filesystem: %s" % (image, exc)) from exc
    return search_fs(fs, pattern, **kwargs)
