# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""YARA scanning of image files and free space (issue #87, Task 1.3).

Builds on :mod:`mountfile.dfir_yara`, which owns rule loading and match
normalisation, and adds the parts the issue asks for: rule-set expansion from a
directory, scanning of files inside an image, scanning of the unallocated
complement of the allocated runs, and the bundled ``rules/`` set.

``yara-python`` is required to compile rules and is imported lazily, so this
module imports cleanly without it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .dfir_yara import YaraUnavailableError, scan_yara
from .fs_walk import block_size, entry_data, entry_runs, iter_files, merge_ranges, subtract_ranges

__all__ = [
    "TARGETS",
    "YaraHit",
    "YaraScanError",
    "YaraUnavailableError",
    "allocated_ranges",
    "load_rules",
    "match_blob",
    "rule_files",
    "rules_directory",
    "scan_files_from_fs",
    "scan_image",
    "scan_unallocated",
    "unallocated_ranges",
]

#: Scan surfaces understood by :func:`scan_image`.
TARGETS = ("files", "unallocated")

RULE_SUFFIXES = (".yar", ".yara")
_RULE_DECLARATION = re.compile(
    r"^\s*(?:private\s+|global\s+)*rule\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE
)


class YaraScanError(RuntimeError):
    """Raised when a rule set cannot be located, read or applied."""


@dataclass(frozen=True)
class YaraHit:
    """One YARA match, tagged with the surface it came from."""

    rule: str
    namespace: str = ""
    tags: tuple[str, ...] = ()
    path: str = ""
    offset: Optional[int] = None
    target: str = "blob"
    data_sha256: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "namespace": self.namespace,
            "tags": list(self.tags),
            "path": self.path,
            "offset": self.offset,
            "target": self.target,
            "data_sha256": self.data_sha256,
            "meta": dict(self.meta or {}),
        }


def rules_directory() -> Path:
    """Directory holding the rule sets shipped with mountfile."""

    return Path(__file__).resolve().parent.parent / "rules"


def _rule_paths(source: Any) -> list[Path]:
    path = Path(source)
    if path.is_dir():
        found = sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and item.suffix.lower() in RULE_SUFFIXES
        )
        if not found:
            raise YaraScanError("no .yar/.yara files under %s" % source)
        return found
    if path.is_file():
        return [path]
    raise YaraScanError("rule source not found: %s" % source)


def rule_files(source: Any) -> set[str]:
    """Names of the rules defined in a file or in every rule file of a directory."""

    names: set[str] = set()
    for path in _rule_paths(source):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise YaraScanError("cannot read %s: %s" % (path, exc)) from exc
        names.update(_RULE_DECLARATION.findall(text))
    return names


def _import_yara() -> Any:
    try:
        import yara  # type: ignore
    except Exception as exc:
        raise YaraUnavailableError(
            "YARA scanning requires the optional 'yara-python' package; "
            "install it with 'python -m pip install yara-python' and retry"
        ) from exc
    return yara


def load_rules(source: Any) -> Any:
    """Compile a rule file, a directory of rule files, or rule source text."""

    yara = _import_yara()
    if hasattr(source, "match") and not isinstance(source, (str, bytes, Path)):
        return source
    candidate = Path(str(source))
    if candidate.exists():
        paths = _rule_paths(candidate)
        try:
            if len(paths) == 1:
                return yara.compile(filepath=str(paths[0]))
            return yara.compile(filepaths={item.stem: str(item) for item in paths})
        except Exception as exc:
            raise YaraScanError("cannot compile %s: %s" % (source, exc)) from exc
    try:
        return yara.compile(source=str(source))
    except Exception as exc:
        raise YaraScanError("cannot compile rules: %s" % exc) from exc


def _normalise(match: Any, *, path: str, target: str, digest: str) -> YaraHit:
    return YaraHit(
        rule=str(getattr(match, "rule", "")),
        namespace=str(getattr(match, "namespace", "") or ""),
        tags=tuple(getattr(match, "tags", ()) or ()),
        path=path,
        offset=getattr(match, "offset", None),
        target=target,
        data_sha256=digest,
        meta=dict(getattr(match, "meta", {}) or {}),
    )


def match_blob(
    compiled: Any,
    data: bytes,
    *,
    path: str = "",
    target: str = "blob",
    base_offset: Optional[int] = None,
) -> list[YaraHit]:
    """Match one in-memory buffer against compiled rules."""

    try:
        matches = compiled.match(data=data)
    except Exception as exc:
        raise YaraScanError("YARA match failed: %s" % exc) from exc
    digest = hashlib.sha256(data).hexdigest()
    hits = [_normalise(m, path=path, target=target, digest=digest) for m in matches]
    if base_offset is not None:
        hits = [
            YaraHit(
                rule=hit.rule,
                namespace=hit.namespace,
                tags=hit.tags,
                path=hit.path,
                offset=base_offset,
                target=hit.target,
                data_sha256=hit.data_sha256,
                meta=hit.meta,
            )
            for hit in hits
        ]
    return hits


def scan_files_from_fs(fs: Any, compiled: Any) -> list[YaraHit]:
    """Scan every file of a filesystem-like object."""

    hits: list[YaraHit] = []
    for path, entry, meta in iter_files(fs):
        size = int(getattr(meta, "size", 0) or 0)
        if size <= 0:
            continue
        data = entry_data(entry, size)
        if not data:
            continue
        hits.extend(match_blob(compiled, data, path=path, target="files"))
    return hits


def allocated_ranges(fs: Any, image_size: int) -> list[tuple[int, int]]:
    """Merged ``[start, end)`` byte ranges occupied by file data."""

    size = block_size(fs)
    ranges: list[tuple[int, int]] = []
    for _path, _entry, meta in iter_files(fs):
        for address, length in entry_runs(fs, meta):
            ranges.append((address * size, (address + length) * size))
    return merge_ranges(ranges)


def unallocated_ranges(fs: Any, image_size: int) -> list[tuple[int, int]]:
    """Complement of :func:`allocated_ranges` inside ``[0, image_size)``."""

    return subtract_ranges(0, int(image_size), allocated_ranges(fs, image_size))


def scan_unallocated(
    image: Any,
    compiled: Any,
    *,
    offset: int = 0,
    chunk: int = 8 * 1024 * 1024,
) -> list[YaraHit]:
    """Scan the free space of an image (derived from the allocated runs)."""

    try:
        import pytsk3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        raise YaraUnavailableError(
            "scanning unallocated space needs the optional 'pytsk3' package"
        ) from exc
    path = Path(image)
    if not path.exists():
        raise YaraScanError("image not found: %s" % image)
    try:
        img = pytsk3.Img_Info(str(path))
        fs = pytsk3.FS_Info(img, int(offset))
    except Exception as exc:
        raise YaraScanError("cannot open %s as a filesystem: %s" % (image, exc)) from exc
    size = int(getattr(fs.info, "img", None) and fs.info.img.get_size() or path.stat().st_size)
    hits: list[YaraHit] = []
    with open(path, "rb") as stream:
        for start, end in unallocated_ranges(fs, size):
            cursor = start
            while cursor < end:
                stream.seek(cursor)
                data = stream.read(min(int(chunk), end - cursor))
                if not data:
                    break
                hits.extend(
                    match_blob(
                        compiled,
                        data,
                        path="<unallocated>",
                        target="unallocated",
                        base_offset=cursor,
                    )
                )
                cursor += len(data)
    return hits


def scan_image(
    image: Any,
    rules: Any,
    *,
    files: bool = True,
    unallocated: bool = True,
    offset: int = 0,
) -> list[YaraHit]:
    """Scan an image, optionally limiting the scan surfaces."""

    if hasattr(rules, "match") and not isinstance(rules, (str, bytes, Path)):
        compiled = rules
    elif isinstance(rules, (list, tuple, set)):
        paths = [item for item in rules]
        if not paths:
            raise YaraScanError("no YARA rules supplied")
        if len(paths) == 1:
            compiled = load_rules(paths[0])
        else:
            yara = _import_yara()
            try:
                compiled = yara.compile(
                    filepaths={Path(p).stem: str(p) for p in paths}
                )
            except Exception as exc:
                raise YaraScanError("cannot compile rules: %s" % exc) from exc
    else:
        compiled = load_rules(rules)

    hits: list[YaraHit] = []
    if files:
        try:
            import pytsk3  # type: ignore

            img = pytsk3.Img_Info(str(image))
            fs = pytsk3.FS_Info(img, int(offset))
            hits.extend(scan_files_from_fs(fs, compiled))
        except YaraScanError:
            raise
        except Exception:
            # Without a readable filesystem the whole-image scan below is the
            # honest answer: it never silently reports "no files found".
            hits.extend(
                YaraHit(
                    rule=hit["rule"],
                    namespace=str(hit.get("namespace", "")),
                    tags=tuple(hit.get("tags", ()) or ()),
                    path=str(image),
                    offset=hit.get("offset"),
                    target="image",
                    data_sha256=str(hit.get("sha256", "") or ""),
                    meta=hit.get("meta") or {},
                )
                for hit in scan_yara(str(image), compiled)
            )
    if unallocated:
        hits.extend(scan_unallocated(image, compiled, offset=offset))
    return hits
