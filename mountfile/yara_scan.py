# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""YARA scanning over image files and unallocated space (Task 1.3, issue #87).

Read-only.  ``yara-python`` is imported lazily: this module can be imported (and
its helpers unit-tested) without the dependency installed, and only a real scan
raises :class:`~mountfile.dfir_yara.YaraUnavailableError`.

Two targets are supported, mirroring the epic:

``files``
    Every regular file reachable through pytsk3, matched in memory so that no
    evidence file is ever written to disk.

``unallocated``
    The complement of the allocated cluster runs -- carved malware, deleted
    configuration and anti-forensic leftovers.

Rules are compiled from a single ``.yar`` file or from a directory of them
(``rules/`` ships ``pii.yar`` and ``anti_forensics.yar``).
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .dfir_yara import YaraUnavailableError
from .timeline import iter_file_entries

#: Target names accepted by :func:`scan_image`.
TARGET_FILES = "files"
TARGET_UNALLOCATED = "unallocated"
TARGETS = (TARGET_FILES, TARGET_UNALLOCATED)

DEFAULT_CHUNK = 8 * 1024 * 1024


class YaraScanError(RuntimeError):
    """Raised for an unusable rule set or scan request."""


@dataclass
class YaraHit:
    """One YARA match, optionally tied to a path and a byte offset."""

    rule: str
    namespace: str = "default"
    tags: tuple[str, ...] = ()
    path: Optional[str] = None
    offset: Optional[int] = None
    target: str = TARGET_FILES
    strings: tuple[Any, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "namespace": self.namespace,
            "tags": list(self.tags),
            "path": self.path,
            "offset": self.offset,
            "target": self.target,
            "strings": [
                (
                    item
                    if isinstance(item, (list, tuple))
                    else getattr(item, "identifier", str(item))
                )
                for item in self.strings
            ],
        }


# ----------------------------------------------------------------------- rules
def rule_files(rules_path: os.PathLike | str) -> dict[str, str]:
    """Return ``{name: path}`` for a rules file or every ``*.yar`` in a folder."""

    path = Path(rules_path)
    if not path.exists():
        raise YaraScanError("rules not found: %s" % path)
    if path.is_dir():
        found = {
            item.stem: str(item)
            for item in sorted(path.iterdir())
            if item.is_file() and item.suffix.lower() in (".yar", ".yara")
        }
        if not found:
            raise YaraScanError("no .yar/.yara files in %s" % path)
        return found
    return {path.stem: str(path)}


def load_rules(rules_path: os.PathLike | str) -> Any:
    """Compile a rules file or directory; needs ``yara-python``."""

    try:
        import yara  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise YaraUnavailableError(
            "YARA scanning requires the optional 'yara-python' package; "
            "install it with 'python -m pip install yara-python' and retry"
        ) from exc

    files = rule_files(rules_path)
    if len(files) == 1:
        return yara.compile(filepath=next(iter(files.values())))
    return yara.compile(filepaths=files)


def _normalise_match(
    match: Any, *, path: Optional[str], offset: Optional[int], target: str
) -> YaraHit:
    namespace = getattr(match, "namespace", None) or "default"
    tags = tuple(getattr(match, "tags", ()) or ())
    strings = tuple(getattr(match, "strings", ()) or ())
    return YaraHit(
        rule=getattr(match, "rule", str(match)),
        namespace=str(namespace),
        tags=tags,
        path=path,
        offset=offset,
        target=target,
        strings=strings,
    )


def match_blob(
    rules: Any, data: bytes, *, path: Optional[str] = None, target: str = TARGET_FILES
) -> list[YaraHit]:
    """Match a single in-memory blob."""

    try:
        matches = rules.match(data=data)
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        raise YaraScanError("YARA match failed: %s" % detail) from exc
    return [
        _normalise_match(
            item,
            path=path,
            offset=(
                min(offsets)
                if (offsets := _string_offsets(getattr(item, "strings", ())))
                else None
            ),
            target=target,
        )
        for item in matches
    ]


def _string_offsets(strings: Any) -> tuple[int, ...]:
    """Read occurrence offsets exposed by yara-python 4.5+ or legacy tuples.

    New yara-python results expose ``StringMatch.instances`` with ``offset``;
    older versions returned ``(offset, identifier, data)`` tuples.  If neither
    representation supplies an offset, no location is inferred.
    """
    offsets: set[int] = set()

    def visit(value: Any) -> None:
        if value is None or isinstance(value, (str, bytes, bytearray)):
            return
        if isinstance(value, dict):
            if "offset" in value:
                offset = value.get("offset")
                if isinstance(offset, int) and not isinstance(offset, bool):
                    offsets.add(offset)
            for key in ("instances", "strings"):
                if key in value:
                    visit(value[key])
            return
        if isinstance(value, (tuple, list, set, frozenset)):
            if value and isinstance(value[0], int) and not isinstance(value[0], bool):
                offsets.add(value[0])
                return
            for item in value:
                visit(item)
            return
        offset = getattr(value, "offset", None)
        if isinstance(offset, int) and not isinstance(offset, bool):
            offsets.add(offset)
        instances = getattr(value, "instances", None)
        if instances is not None:
            visit(instances)

    visit(strings)
    return tuple(sorted(offsets))


# ----------------------------------------------------------------------- scans
def scan_files_from_fs(
    fs: Any, rules: Any, *, max_file_size: Optional[int] = None
) -> list[YaraHit]:
    """Scan every regular file of an open filesystem, in memory."""

    hits: list[YaraHit] = []
    for path, entry in iter_file_entries(fs):
        size = int(getattr(entry.info.meta, "size", 0) or 0)
        if max_file_size is not None and size > max_file_size:
            continue
        try:
            data = entry.read_random(0, size)
        except Exception as exc:
            raise YaraScanError("unable to read file %s: %s" % (path, exc)) from exc
        if not data:
            continue
        hits.extend(match_blob(rules, bytes(data), path=path, target=TARGET_FILES))
    return hits


def scan_files(
    image_path: os.PathLike | str, rules: Any, **kwargs: Any
) -> list[YaraHit]:
    """Filesystem-aware scan of ``image_path`` (needs pytsk3)."""

    from .timeline import open_tsk_fs

    return scan_files_from_fs(open_tsk_fs(image_path), rules, **kwargs)


def _merge(ranges: Iterable[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, length in sorted(ranges):
        end = min(start + length, limit)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def allocated_ranges(fs: Any, image_size: int) -> list[tuple[int, int]]:
    """Merged physical ``(start, end)`` runs of every allocated file."""

    block_size = int(getattr(fs.info, "block_size", 512) or 512)
    collected: list[tuple[int, int]] = []
    for _path, entry in iter_file_entries(fs):
        try:
            meta_file = fs.open_meta(inode=entry.info.meta.addr)
        except Exception:
            continue
        for attribute in meta_file:
            for run in attribute:
                length = int(getattr(run, "len", 0) or 0)
                if length <= 0:
                    continue
                start = int(getattr(run, "addr", 0) or 0) * block_size
                collected.append((start, length * block_size))
    return _merge(collected, image_size)


def unallocated_ranges(fs: Any, image_size: int) -> list[tuple[int, int]]:
    """Complement of :func:`allocated_ranges` over the whole image."""

    free: list[tuple[int, int]] = []
    cursor = 0
    for start, end in allocated_ranges(fs, image_size):
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < image_size:
        free.append((cursor, image_size))
    return free


def scan_unallocated_from_fs(
    fs: Any,
    rules: Any,
    image_size: int,
    *,
    chunk: int = DEFAULT_CHUNK,
) -> list[YaraHit]:
    """Scan complete unallocated extents without signature-length limits.

    YARA scans a private 0600 temporary file for each contiguous extent.  Input
    is still read in bounded chunks, but matching is performed over the whole
    extent so arbitrarily long signatures cannot disappear at chunk edges.
    The temporary file is removed even when reading or matching fails.
    """

    if chunk <= 0:
        raise ValueError("chunk must be positive")
    hits: list[YaraHit] = []
    source = getattr(fs, "_image_path", None)
    for start, end in unallocated_ranges(fs, image_size):
        fd, temp_path = tempfile.mkstemp(prefix="mountfile-yara-")
        try:
            with os.fdopen(fd, "wb") as stream:
                for offset in range(start, end, chunk):
                    length = min(chunk, end - offset)
                    data = _read_region(fs, source, offset, length)
                    if len(data) != length:
                        raise YaraScanError(
                            "short read in unallocated range at %d: expected %d, got %d"
                            % (offset, length, len(data))
                        )
                    stream.write(data)
            try:
                matches = rules.match(filepath=temp_path)
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                raise YaraScanError("YARA match failed: %s" % detail) from exc
            for item in matches:
                offsets = _string_offsets(getattr(item, "strings", ()))
                if offsets:
                    for relative in offsets:
                        hits.append(
                            _normalise_match(
                                item,
                                path="<unallocated>",
                                offset=start + relative,
                                target=TARGET_UNALLOCATED,
                            )
                        )
                else:
                    hits.append(
                        _normalise_match(
                            item,
                            path="<unallocated>",
                            offset=None,
                            target=TARGET_UNALLOCATED,
                        )
                    )
        except YaraScanError:
            raise
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            raise YaraScanError(
                "unable to scan unallocated range %d:%d: %s" % (start, end, detail)
            ) from exc
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return hits


def _read_region(fs: Any, image_path: Optional[str], offset: int, length: int) -> bytes:
    reader = getattr(fs, "read_random", None)
    if callable(reader):  # pragma: no cover - not part of the public pytsk3 API
        try:
            return bytes(reader(offset, length))
        except Exception as exc:
            raise YaraScanError(
                "unable to read image region %d:%d: %s" % (offset, length, exc)
            ) from exc
    if not image_path:
        raise YaraScanError(
            "image source is unavailable for region %d:%d" % (offset, length)
        )
    try:
        with open(image_path, "rb") as stream:
            stream.seek(offset)
            return stream.read(length)
    except OSError as exc:
        raise YaraScanError(
            "unable to read image region %d:%d: %s" % (offset, length, exc)
        ) from exc


def scan_unallocated(
    image_path: os.PathLike | str,
    rules: Any,
    *,
    chunk: int = DEFAULT_CHUNK,
) -> list[YaraHit]:
    """Scan unallocated space of ``image_path`` (needs pytsk3)."""

    path = Path(image_path)
    if not path.exists():
        raise YaraScanError("image not found: %s" % path)
    fs = _open_fs_with_path(path)
    return scan_unallocated_from_fs(fs, rules, path.stat().st_size, chunk=chunk)


def _open_fs_with_path(path: Path) -> Any:
    """Open pytsk3 and remember the image path for raw region reads."""

    from .timeline import open_tsk_fs

    fs = open_tsk_fs(path)
    try:
        setattr(fs, "_image_path", str(path))
    except Exception:  # pragma: no cover - read-only proxy objects
        pass
    return fs


def scan_image(
    image_path: os.PathLike | str,
    rules_path: os.PathLike | str,
    *,
    targets: Sequence[str] = TARGETS,
    chunk: int = DEFAULT_CHUNK,
    max_file_size: Optional[int] = None,
) -> list[YaraHit]:
    """Compile ``rules_path`` and scan the requested targets of ``image_path``."""

    unknown = [name for name in targets if name not in TARGETS]
    if unknown:
        raise YaraScanError("unknown target(s): %s" % ", ".join(unknown))

    rules = load_rules(rules_path)
    path = Path(image_path)
    if not path.exists():
        raise YaraScanError("image not found: %s" % path)

    hits: list[YaraHit] = []
    fs = _open_fs_with_path(path)
    if TARGET_FILES in targets:
        hits.extend(scan_files_from_fs(fs, rules, max_file_size=max_file_size))
    if TARGET_UNALLOCATED in targets:
        hits.extend(
            scan_unallocated_from_fs(fs, rules, path.stat().st_size, chunk=chunk)
        )
    return hits


def iter_hits(hits: Iterable[YaraHit]) -> Iterator[dict[str, Any]]:
    """Serialize hits for JSON output."""

    for hit in hits:
        yield hit.as_dict()
