# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Live diff between two images (issue #87, Task 2.2).

:func:`diff_snapshots` compares two ``path -> (sha256, size)`` maps and
classifies every entry as added, modified or removed.  :class:`LiveDiff` keeps a
baseline snapshot so a write session can be reported on afterwards, and
:func:`to_markdown` renders the added/modified/removed report the issue asks for.

Snapshots are plain mappings, so a session can be fed from an image walk, from
the FAT32 engine, or from a test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "ADDED",
    "MODIFIED",
    "REMOVED",
    "DiffEntry",
    "LiveDiff",
    "LiveDiffError",
    "diff_images",
    "diff_snapshots",
    "summarise",
    "to_markdown",
    "write_markdown",
]

ADDED = "added"
MODIFIED = "modified"
REMOVED = "removed"
STATUSES = (ADDED, MODIFIED, REMOVED)

DEFAULT_TITLE = "Live diff"
REGION_CHUNK = 8 * 1024 * 1024


class LiveDiffError(RuntimeError):
    """Raised when a diff cannot be produced."""


@dataclass(frozen=True)
class DiffEntry:
    """One changed path."""

    path: str
    status: str
    before_sha: Optional[str] = None
    after_sha: Optional[str] = None
    before_size: Optional[int] = None
    after_size: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(
                "unknown diff status %r; expected one of %s"
                % (self.status, ", ".join(STATUSES))
            )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(value: Any) -> tuple[Optional[str], Optional[int]]:
    """Normalise a snapshot value to ``(sha256, size)``."""

    if value is None:
        return None, None
    if isinstance(value, Mapping):
        return value.get("sha256"), value.get("size")
    if isinstance(value, (tuple, list)):
        digest = value[0] if len(value) > 0 else None
        size = value[1] if len(value) > 1 else None
        return digest, size
    return getattr(value, "sha256", None), getattr(value, "size", None)


def diff_snapshots(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[DiffEntry]:
    """Classify every path of two snapshots as added, modified or removed."""

    entries: list[DiffEntry] = []
    for path in sorted(set(before) | set(after)):
        old_sha, old_size = _digest(before.get(path))
        new_sha, new_size = _digest(after.get(path))
        if path not in before:
            entries.append(
                DiffEntry(path, ADDED, after_sha=new_sha, after_size=new_size)
            )
        elif path not in after:
            entries.append(
                DiffEntry(path, REMOVED, before_sha=old_sha, before_size=old_size)
            )
        elif old_sha != new_sha or old_size != new_size:
            entries.append(
                DiffEntry(
                    path,
                    MODIFIED,
                    before_sha=old_sha,
                    after_sha=new_sha,
                    before_size=old_size,
                    after_size=new_size,
                )
            )
    return entries


def summarise(entries: Iterable[DiffEntry]) -> dict[str, int]:
    """Counts per status, always including zeroes for all three statuses."""

    counts = {ADDED: 0, MODIFIED: 0, REMOVED: 0}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts


def to_markdown(entries: Iterable[DiffEntry], *, title: str = DEFAULT_TITLE) -> str:
    """Render the diff as a Markdown report."""

    items = list(entries)
    counts = summarise(items)
    lines = [
        "# %s" % title,
        "",
        "| Status | Count |",
        "| --- | --- |",
        "| Added | %d |" % counts[ADDED],
        "| Modified | %d |" % counts[MODIFIED],
        "| Removed | %d |" % counts[REMOVED],
        "",
        "## Added (%d)" % counts[ADDED],
        "",
    ]
    lines.extend(_section(items, ADDED, "after_sha"))
    lines.extend(["## Modified (%d)" % counts[MODIFIED], ""])
    lines.extend(_section(items, MODIFIED, "after_sha"))
    lines.extend(["## Removed (%d)" % counts[REMOVED], ""])
    lines.extend(_section(items, REMOVED, "before_sha"))
    return "\n".join(lines).rstrip() + "\n"


def _section(items: Sequence[DiffEntry], status: str, digest_field: str) -> list[str]:
    group = [item for item in items if item.status == status]
    if not group:
        return ["_none_", ""]
    lines = ["| Path | SHA-256 | Size |", "| --- | --- | --- |"]
    for item in group:
        digest = getattr(item, digest_field) or "-"
        size = item.after_size if status != REMOVED else item.before_size
        lines.append("| `%s` | `%s` | %s |" % (item.path, str(digest)[:16], size if size is not None else "-"))
    lines.append("")
    return lines


def write_markdown(
    entries: Iterable[DiffEntry], destination: Any, *, title: str = DEFAULT_TITLE
) -> int:
    """Write the Markdown report and return the number of entries."""

    items = list(entries)
    report = to_markdown(items, title=title)
    target = Path(destination)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    return len(items)


def _snapshot_from_image(image: Any, offset: int = 0) -> dict[str, tuple[str, int]]:
    """``path -> (sha256, size)`` for every file of an image."""

    try:
        import pytsk3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        raise LiveDiffError(
            "snapshotting an image needs the optional 'pytsk3' package; install "
            "it with 'python -m pip install pytsk3'"
        ) from exc
    from .fs_walk import entry_data, iter_files

    path = Path(image)
    if not path.exists():
        raise LiveDiffError("image not found: %s" % image)
    try:
        img = pytsk3.Img_Info(str(path))
        fs = pytsk3.FS_Info(img, int(offset))
    except Exception as exc:
        raise LiveDiffError("cannot open %s as a filesystem: %s" % (image, exc)) from exc
    snapshot: dict[str, tuple[str, int]] = {}
    for file_path, entry, meta in iter_files(fs):
        size = int(getattr(meta, "size", 0) or 0)
        data = entry_data(entry, size) if size > 0 else b""
        snapshot[file_path] = (hashlib.sha256(data).hexdigest(), size)
    return snapshot


def diff_images(before: Any, after: Any, *, offset: int = 0) -> list[DiffEntry]:
    """Compare two images file by file.

    A filesystem-level comparison is used when both images can be read; the
    raw region comparison is the fallback, so a damaged image still yields a
    real answer instead of an exception.
    """

    try:
        return diff_snapshots(
            _snapshot_from_image(before, offset=offset),
            _snapshot_from_image(after, offset=offset),
        )
    except LiveDiffError:
        return _diff_regions(before, after)


def _diff_regions(before: Any, after: Any) -> list[DiffEntry]:
    left_path = Path(before)
    right_path = Path(after)
    if not left_path.is_file() or not right_path.is_file():
        raise LiveDiffError("both images must exist to compare them")
    entries: list[DiffEntry] = []
    with open(left_path, "rb") as left, open(right_path, "rb") as right:
        size = max(left_path.stat().st_size, right_path.stat().st_size)
        for offset in range(0, size, REGION_CHUNK):
            left.seek(offset)
            right.seek(offset)
            old = left.read(REGION_CHUNK)
            new = right.read(REGION_CHUNK)
            if old == new:
                continue
            entries.append(
                DiffEntry(
                    path="<region:%08x>" % offset,
                    status=MODIFIED,
                    before_sha=hashlib.sha256(old).hexdigest() if old else None,
                    after_sha=hashlib.sha256(new).hexdigest() if new else None,
                    before_size=len(old),
                    after_size=len(new),
                )
            )
    return entries


class LiveDiff:
    """Session-scoped diff: record a baseline, then compare against it."""

    def __init__(self, snapshot: Optional[Mapping[str, Any]] = None) -> None:
        self._snapshot: Optional[dict[str, Any]] = dict(snapshot) if snapshot else None

    @property
    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    @property
    def snapshot(self) -> Optional[dict[str, Any]]:
        return dict(self._snapshot) if self._snapshot is not None else None

    def take_snapshot(self, source: Any) -> dict[str, Any]:
        """Record the baseline, from a mapping or from an image."""

        if isinstance(source, Mapping):
            self._snapshot = {str(key): _digest(value) for key, value in source.items()}
        else:
            self._snapshot = _snapshot_from_image(source)
        return dict(self._snapshot)

    def reset(self) -> None:
        self._snapshot = None

    def compare(self, source: Any) -> list[DiffEntry]:
        """Diff ``source`` against the baseline."""

        if self._snapshot is None:
            raise LiveDiffError(
                "no baseline snapshot: call take_snapshot() before compare()"
            )
        if isinstance(source, Mapping):
            after = {str(key): _digest(value) for key, value in source.items()}
        else:
            after = _snapshot_from_image(source)
        return diff_snapshots(self._snapshot, after)

    def to_markdown(self, source: Any, *, title: str = DEFAULT_TITLE) -> str:
        return to_markdown(self.compare(source), title=title)

    def to_json(self, source: Any, destination: Any) -> int:
        entries = self.compare(source)
        text = json.dumps([entry.as_dict() for entry in entries], ensure_ascii=False, indent=2)
        target = Path(destination)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return len(entries)
