# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Live diff of an image between two moments (Task 2.2, issue #87).

Two granularities:

``LiveDiff``
    Session-oriented.  :meth:`LiveDiff.take` records a content snapshot,
    :meth:`LiveDiff.compare` reports what changed since then.  This is what the
    GUI uses to show "what did I just write?".

``diff_images``
    Snapshot-to-snapshot comparison of two images (or of the same image before
    and after an operation), returned as ``added`` / ``modified`` / ``removed``
    entries plus a Markdown report.

Read-only in both directions: the diff never writes to either image.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .timeline import iter_file_entries

ADDED = "added"
MODIFIED = "modified"
REMOVED = "removed"
STATUSES = (ADDED, MODIFIED, REMOVED)


class LiveDiffError(RuntimeError):
    """Raised when a snapshot cannot be taken."""


@dataclass(frozen=True)
class DiffEntry:
    """One path-level difference."""

    path: str
    status: str
    before_sha: Optional[str] = None
    after_sha: Optional[str] = None
    before_size: Optional[int] = None
    after_size: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError("unknown status: %s" % self.status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "before_sha": self.before_sha,
            "after_sha": self.after_sha,
            "before_size": self.before_size,
            "after_size": self.after_size,
        }


def _digest(entry: Any, size: int) -> Optional[str]:
    try:
        data = entry.read_random(0, size)
    except Exception:  # pragma: no cover - unreadable file
        return None
    if data is None:
        return None
    return hashlib.sha256(bytes(data)).hexdigest()


def snapshot_from_fs(
    fs: Any, *, hash_files: bool = True
) -> dict[str, tuple[Optional[str], int]]:
    """Return ``{path: (sha256|None, size)}`` for every file in ``fs``."""

    snapshot: dict[str, tuple[Optional[str], int]] = {}
    for path, entry in iter_file_entries(fs):
        size = int(getattr(entry.info.meta, "size", 0) or 0)
        snapshot[path] = (_digest(entry, size) if hash_files else None, size)
    return snapshot


def diff_snapshots(
    before: dict[str, tuple[Optional[str], int]],
    after: dict[str, tuple[Optional[str], int]],
) -> list[DiffEntry]:
    """Compare two :func:`snapshot_from_fs` results."""

    entries: list[DiffEntry] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            digest, size = after[path]
            entries.append(
                DiffEntry(path=path, status=ADDED, after_sha=digest, after_size=size)
            )
            continue
        if path not in after:
            digest, size = before[path]
            entries.append(
                DiffEntry(
                    path=path, status=REMOVED, before_sha=digest, before_size=size
                )
            )
            continue
        old_digest, old_size = before[path]
        new_digest, new_size = after[path]
        changed = old_size != new_size or (
            old_digest is not None
            and new_digest is not None
            and old_digest != new_digest
        )
        if changed:
            entries.append(
                DiffEntry(
                    path=path,
                    status=MODIFIED,
                    before_sha=old_digest,
                    after_sha=new_digest,
                    before_size=old_size,
                    after_size=new_size,
                )
            )
    return entries


class LiveDiff:
    """Session-scoped diff tracker."""

    def __init__(self, *, hash_files: bool = True) -> None:
        self.hash_files = hash_files
        self._snapshot: Optional[dict[str, tuple[Optional[str], int]]] = None

    @property
    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    def snapshot(
        self, image_path: os.PathLike | str
    ) -> dict[str, tuple[Optional[str], int]]:
        """Take a snapshot without storing it."""

        from .timeline import open_tsk_fs

        path = Path(image_path)
        if not path.exists():
            raise LiveDiffError("image not found: %s" % path)
        return snapshot_from_fs(open_tsk_fs(path), hash_files=self.hash_files)

    def take(
        self, image_path: os.PathLike | str
    ) -> dict[str, tuple[Optional[str], int]]:
        """Take and store the baseline snapshot."""

        self._snapshot = self.snapshot(image_path)
        return self._snapshot

    def compare(
        self,
        image_path: os.PathLike | str,
        *,
        baseline: Optional[dict[str, tuple[Optional[str], int]]] = None,
    ) -> list[DiffEntry]:
        """Compare the current image against the stored (or supplied) baseline."""

        base = baseline if baseline is not None else self._snapshot
        if base is None:
            raise LiveDiffError("no baseline snapshot; call take() first")
        return diff_snapshots(base, self.snapshot(image_path))

    def reset(self) -> None:
        self._snapshot = None


def diff_images(
    before: os.PathLike | str, after: os.PathLike | str, **kwargs: Any
) -> list[DiffEntry]:
    """Compare two images (or one image before/after an operation)."""

    return diff_snapshots(
        LiveDiff(**kwargs).snapshot(before),
        LiveDiff(**kwargs).snapshot(after),
    )


def to_markdown(entries: Iterable[DiffEntry], *, title: str = "Live diff") -> str:
    """Render a Markdown report with one section per status."""

    grouped: dict[str, list[DiffEntry]] = {status: [] for status in STATUSES}
    for entry in entries:
        grouped.setdefault(entry.status, []).append(entry)

    lines = ["# %s" % title, ""]
    lines.append("| status | count |")
    lines.append("|---|---|")
    for status in STATUSES:
        lines.append("| %s | %d |" % (status, len(grouped.get(status, []))))
    lines.append("")

    for status, heading in (
        (ADDED, "Added"),
        (MODIFIED, "Modified"),
        (REMOVED, "Removed"),
    ):
        items = grouped.get(status) or []
        lines.append("## %s (%d)" % (heading, len(items)))
        lines.append("")
        if not items:
            lines.append("_none_")
            lines.append("")
            continue
        lines.append(
            "| path | size before | size after | sha256 before | sha256 after |"
        )
        lines.append("|---|---|---|---|---|")
        for item in items:
            lines.append(
                "| `%s` | %s | %s | %s | %s |"
                % (
                    item.path,
                    "-" if item.before_size is None else item.before_size,
                    "-" if item.after_size is None else item.after_size,
                    "-" if not item.before_sha else item.before_sha[:16],
                    "-" if not item.after_sha else item.after_sha[:16],
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(
    entries: Iterable[DiffEntry], out_path: os.PathLike | str, **kwargs: Any
) -> int:
    """Write the Markdown report; returns the number of entries."""

    items = list(entries)
    target = Path(out_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_markdown(items, **kwargs), encoding="utf-8")
    return len(items)


def summarise(entries: Iterable[DiffEntry]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts
