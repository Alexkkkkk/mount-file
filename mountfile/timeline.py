# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Filesystem-aware timeline collection (Task 1.1, issue #87).

Read-only.  Walks a raw image through ``pytsk3`` (The Sleuth Kit), turns every
timestamp exposed by the filesystem metadata layer into one normalized UTC
event and stores the result as SQLite + CSV with the schema requested by the
epic::

    timestamp | source | path | event | sha256

The collector never writes to the evidence image.  Events are produced for the
four metadata timestamps The Sleuth Kit exposes (``created`` / ``mtime`` /
``atime`` / ``ctime``); the NTFS ``$FILE_NAME`` attribute is **not** decoded
here, because ``pytsk3`` does not surface it separately from
``$STANDARD_INFORMATION`` -- a dedicated MFT parser is required for that and is
tracked separately in the epic.

Example::

    python -m mountfile timeline-build dump.img -o timeline.db --csv timeline.csv
"""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

UTC = timezone.utc

#: Metadata timestamps collected from the filesystem layer.
EVENT_CREATED = "created"
EVENT_MODIFIED = "mtime"
EVENT_ACCESSED = "atime"
EVENT_CHANGED = "ctime"
EVENT_TYPES = (EVENT_CREATED, EVENT_MODIFIED, EVENT_ACCESSED, EVENT_CHANGED)

CSV_FIELDS = ("timestamp", "source", "path", "event", "sha256")

#: Files larger than this are listed without a digest by default, so that a
#: timeline over a full evidence image stays bounded in time and memory.
DEFAULT_HASH_LIMIT = 4 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    timestamp TEXT NOT NULL,
    source    TEXT NOT NULL,
    path      TEXT NOT NULL,
    event     TEXT NOT NULL,
    sha256    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_path ON events(path);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
"""


class TimelineError(RuntimeError):
    """Raised when a timeline cannot be collected from the supplied image."""


@dataclass(frozen=True)
class TimelineEvent:
    """One normalized, read-only timeline event (aware UTC timestamp)."""

    timestamp: datetime
    source: str
    path: str
    event: str
    sha256: Optional[str] = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

    def as_row(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "source": self.source,
            "path": self.path,
            "event": self.event,
            "sha256": self.sha256,
        }

    def to_csv_row(self) -> dict[str, str]:
        return {key: ("" if value is None else str(value)) for key, value in self.as_row().items()}


# --------------------------------------------------------------------- helpers
def _as_utc(seconds: Any, nanoseconds: Any = 0) -> Optional[datetime]:
    """Convert a Sleuth Kit seconds/nanoseconds pair into an aware datetime."""

    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        # TSK reports "no timestamp" as 0 or -1.
        return None
    try:
        micro = int(int(nanoseconds or 0) // 1000)
    except (TypeError, ValueError):
        micro = 0
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=micro)
    except (OverflowError, OSError, ValueError):
        return None


def _entry_name(entry: Any) -> str:
    try:
        raw = entry.info.name.name
    except AttributeError:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _entry_is_dir(entry: Any) -> bool:
    return getattr(entry.info.meta, "type", None) == 2


def _entry_is_file(entry: Any) -> bool:
    return getattr(entry.info.meta, "type", None) == 1


def _hash_entry(entry: Any, hash_limit: Optional[int]) -> Optional[str]:
    size = int(getattr(entry.info.meta, "size", 0) or 0)
    if hash_limit is not None and size > hash_limit:
        return None
    try:
        data = entry.read_random(0, size)
    except Exception:  # pragma: no cover - unreadable file inside a damaged image
        return None
    if data is None:
        return None
    return hashlib.sha256(bytes(data)).hexdigest()


def iter_file_entries(fs: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(path, entry)`` for every regular file reachable from ``/``."""

    def walk(directory: Any, base: str) -> Iterator[tuple[str, Any]]:
        try:
            entries = list(directory)
        except Exception:
            return
        for entry in entries:
            name = _entry_name(entry)
            if name in (".", ".."):
                continue
            path = "%s/%s" % (base, name)
            if _entry_is_dir(entry):
                try:
                    child = entry.as_directory()
                except Exception:
                    continue
                yield from walk(child, path)
            elif _entry_is_file(entry):
                yield path, entry

    yield from walk(fs.open_dir("/"), prefix)


# ------------------------------------------------------------------ collection
def collect_events_from_fs(
    fs: Any,
    *,
    source: str = "pytsk3",
    hash_limit: Optional[int] = DEFAULT_HASH_LIMIT,
    events: Sequence[str] = EVENT_TYPES,
) -> Iterator[TimelineEvent]:
    """Yield one :class:`TimelineEvent` per timestamp found in ``fs``.

    ``fs`` is any object exposing the small pytsk3 surface used here
    (``open_dir``, ``info.block_size``), which keeps the collector testable
    without The Sleuth Kit installed.
    """

    wanted = tuple(events)
    for path, entry in iter_file_entries(fs):
        meta = entry.info.meta
        digest = _hash_entry(entry, hash_limit) if hash_limit != 0 else None
        candidates = (
            (EVENT_CREATED, getattr(meta, "crtime", None), getattr(meta, "crtime_nano", 0)),
            (EVENT_MODIFIED, getattr(meta, "mtime", None), getattr(meta, "mtime_nano", 0)),
            (EVENT_ACCESSED, getattr(meta, "atime", None), getattr(meta, "atime_nano", 0)),
            (EVENT_CHANGED, getattr(meta, "ctime", None), getattr(meta, "ctime_nano", 0)),
        )
        for event, seconds, nano in candidates:
            if event not in wanted:
                continue
            stamp = _as_utc(seconds, nano)
            if stamp is None:
                continue
            yield TimelineEvent(
                timestamp=stamp, source=source, path=path, event=event, sha256=digest
            )


def open_tsk_fs(image_path: os.PathLike | str) -> Any:
    """Open ``image_path`` read-only through pytsk3."""

    path = Path(image_path)
    if not path.exists():
        raise TimelineError("image not found: %s" % path)
    try:
        import pytsk3  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TimelineError(
            "filesystem-aware timeline collection requires pytsk3; "
            "install it with 'python -m pip install pytsk3'"
        ) from exc
    try:
        return pytsk3.FS_Info(pytsk3.Img_Info(str(path)))
    except Exception as exc:
        raise TimelineError("cannot open %s: %s" % (path, exc)) from exc


def collect_timeline(image_path: os.PathLike | str, **kwargs: Any) -> list[TimelineEvent]:
    """Collect the full timeline of ``image_path`` into a list of events."""

    fs = open_tsk_fs(image_path)
    return list(collect_events_from_fs(fs, **kwargs))


# --------------------------------------------------------------------- outputs
def write_sqlite(events: Iterable[TimelineEvent], db_path: os.PathLike | str) -> int:
    """Write ``events`` into a fresh SQLite database; returns the row count."""

    rows = [event.as_row() for event in events]
    target = Path(db_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    try:
        connection.executescript(_SCHEMA)
        connection.execute("DELETE FROM events")
        connection.executemany(
            "INSERT INTO events (timestamp, source, path, event, sha256) "
            "VALUES (:timestamp, :source, :path, :event, :sha256)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return len(rows)


def write_csv(events: Iterable[TimelineEvent], csv_path: os.PathLike | str) -> int:
    """Write ``events`` as CSV with the epic's column order; returns row count."""

    target = Path(csv_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(target, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for event in events:
            writer.writerow(event.to_csv_row())
            count += 1
    return count


def build_timeline(
    image_path: os.PathLike | str,
    *,
    db_path: Optional[os.PathLike | str] = None,
    csv_path: Optional[os.PathLike | str] = None,
    **kwargs: Any,
) -> tuple[int, list[TimelineEvent]]:
    """Collect a timeline and persist it to SQLite and/or CSV."""

    events = collect_timeline(image_path, **kwargs)
    if db_path is not None:
        write_sqlite(events, db_path)
    if csv_path is not None:
        write_csv(events, csv_path)
    return len(events), events


# ---------------------------------------------------------------------- queries
def _parse_bound(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def query_timeline(
    db_path: os.PathLike | str,
    *,
    start: Any = None,
    end: Any = None,
    event: Optional[str] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return events inside ``[start, end]`` -- the AI "what happened when" hook."""

    sql = "SELECT timestamp, source, path, event, sha256 FROM events WHERE 1=1"
    params: list[Any] = []
    low, high = _parse_bound(start), _parse_bound(end)
    if low:
        sql += " AND timestamp >= ?"
        params.append(low)
    if high:
        sql += " AND timestamp <= ?"
        params.append(high)
    if event:
        sql += " AND event = ?"
        params.append(event)
    sql += " ORDER BY timestamp ASC LIMIT ?"
    params.append(int(limit))

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def summarise(events: Iterable[TimelineEvent]) -> dict[str, Any]:
    """Small helper used by the CLI and by the forensic copilot summary."""

    events = list(events)
    per_event: dict[str, int] = {name: 0 for name in EVENT_TYPES}
    per_source: dict[str, int] = {}
    paths = set()
    for item in events:
        per_event[item.event] = per_event.get(item.event, 0) + 1
        per_source[item.source] = per_source.get(item.source, 0) + 1
        paths.add(item.path)
    return {
        "events": len(events),
        "files": len(paths),
        "by_event": per_event,
        "by_source": per_source,
    }
