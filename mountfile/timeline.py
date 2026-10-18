# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Filesystem timeline collection, storage and query (issue #87, Task 1.1).

Collects every timestamp a filesystem exposes, stores the result in SQLite and
CSV, and answers the "what happened on 12 March between 14:00 and 15:00"
question with :func:`query_timeline`.

Events are read through :mod:`mountfile.fs_walk`, so the collector works against
``pytsk3`` and against the fakes used by the tests.  ``pytsk3`` is imported
lazily by :func:`build_timeline` only.
"""

from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .fs_walk import entry_data, iter_files

__all__ = [
    "DEFAULT_HASH_LIMIT",
    "EVENT_TYPES",
    "TimelineError",
    "TimelineEvent",
    "build_timeline",
    "collect_events_from_fs",
    "query_timeline",
    "summarise",
    "write_csv",
    "write_sqlite",
]

UTC = timezone.utc

#: Event names emitted for every file, in the order they are written.
EVENT_TYPES = ("created", "mtime", "atime", "ctime")

#: Files larger than this are not hashed during collection.
DEFAULT_HASH_LIMIT = 16 * 1024 * 1024

CSV_FIELDS = ("timestamp", "source", "path", "event", "sha256")

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT '',
    path      TEXT NOT NULL,
    event     TEXT NOT NULL,
    sha256    TEXT,
    epoch     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(epoch);
CREATE INDEX IF NOT EXISTS idx_events_path  ON events(path);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
"""

#: ``pytsk3`` field name -> timeline event name.
_FIELDS = (
    ("crtime", "created"),
    ("mtime", "mtime"),
    ("atime", "atime"),
    ("ctime", "ctime"),
)


class TimelineError(RuntimeError):
    """Raised when a timeline cannot be collected or stored."""


@dataclass(frozen=True)
class TimelineEvent:
    """One UTC timestamp of one file."""

    timestamp: datetime
    source: str = ""
    path: str = ""
    event: str = ""
    sha256: Optional[str] = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

    @property
    def timestamp_utc(self) -> datetime:
        return self.timestamp.astimezone(UTC)

    @property
    def evidence_path(self) -> str:
        return self.path

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": _iso(self.timestamp),
            "source": self.source,
            "path": self.path,
            "event": self.event,
            "sha256": self.sha256,
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _aware(seconds: Any, nanoseconds: Any = None) -> Optional[datetime]:
    """Build an aware UTC datetime from a pytsk3 epoch/nanosecond pair."""

    if seconds is None or isinstance(seconds, bool):
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    moment = datetime.fromtimestamp(value, tz=UTC)
    if nanoseconds:
        micro = int(nanoseconds) // 1000
        if 0 <= micro <= 999999:
            moment = moment.replace(microsecond=micro)
    return moment


def _file_digest(entry: Any, size: Any, limit: Optional[int]) -> Optional[str]:
    if limit is None:
        return None
    try:
        length = int(size)
    except (TypeError, ValueError):
        return None
    if length <= 0 or length > int(limit):
        return None
    payload = entry_data(entry, length)
    if not payload:
        return None
    return hashlib.sha256(payload).hexdigest()


def collect_events_from_fs(
    fs: Any,
    *,
    hash_limit: Optional[int] = None,
    source: str = "pytsk3",
) -> Iterator[TimelineEvent]:
    """Yield the timeline events of every file in ``fs``.

    ``hash_limit`` is the largest file that gets hashed; ``None`` disables
    hashing.  Files without a single usable timestamp contribute no events.
    """

    for path, entry, meta in iter_files(fs):
        digest: Optional[str] = None
        digest_done = False
        for field_name, event in _FIELDS:
            seconds = getattr(meta, field_name, None)
            nanos = getattr(meta, "%s_nano" % field_name, None)
            moment = _aware(seconds, nanos)
            if moment is None:
                continue
            if not digest_done:
                digest = _file_digest(entry, getattr(meta, "size", 0), hash_limit)
                digest_done = True
            yield TimelineEvent(
                timestamp=moment,
                source=source,
                path=path,
                event=event,
                sha256=digest,
            )


def build_timeline(
    image: Any,
    *,
    db_path: Any = None,
    csv_path: Any = None,
    offset: int = 0,
    hash_limit: Optional[int] = DEFAULT_HASH_LIMIT,
    max_events: Optional[int] = None,
) -> tuple[int, list[TimelineEvent]]:
    """Collect a timeline from a raw image and persist it.

    Returns ``(count, events)``.  ``db_path`` and ``csv_path`` are optional;
    when given, the events are written to SQLite and/or CSV.
    """

    events = list(_collect_from_image(image, offset=offset, hash_limit=hash_limit))
    if max_events is not None:
        events = events[: int(max_events)]
    if db_path is not None:
        write_sqlite(events, db_path)
    if csv_path is not None:
        write_csv(events, csv_path)
    return len(events), events


def _collect_from_image(
    image: Any, *, offset: int, hash_limit: Optional[int]
) -> Iterator[TimelineEvent]:
    try:
        import pytsk3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        raise TimelineError(
            "collecting a timeline from an image needs the optional 'pytsk3' "
            "package; install it with 'python -m pip install pytsk3'"
        ) from exc
    path = Path(image)
    if not path.exists():
        raise TimelineError("image not found: %s" % image)
    try:
        img = pytsk3.Img_Info(str(path))
        fs = pytsk3.FS_Info(img, int(offset))
    except Exception as exc:
        raise TimelineError("cannot open %s as a filesystem: %s" % (image, exc)) from exc
    yield from collect_events_from_fs(fs, hash_limit=hash_limit)


def write_sqlite(events: Iterable[Any], db_path: Any) -> int:
    """Store events in SQLite, replacing any previous content."""

    rows = [_row(event) for event in events]
    target = Path(db_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target))
    try:
        connection.executescript(_SQLITE_SCHEMA)
        connection.execute("DELETE FROM events")
        connection.executemany(
            "INSERT INTO events (timestamp, source, path, event, sha256, epoch)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["timestamp"],
                    row["source"],
                    row["path"],
                    row["event"],
                    row["sha256"],
                    row["epoch"],
                )
                for row in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return len(rows)


def write_csv(events: Iterable[Any], csv_path: Any) -> int:
    """Write events as CSV with the documented header."""

    rows = [_row(event) for event in events]
    target = Path(csv_path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp": row["timestamp"],
                    "source": row["source"],
                    "path": row["path"],
                    "event": row["event"],
                    "sha256": row["sha256"] or "",
                }
            )
    return len(rows)


def _row(event: Any) -> dict[str, Any]:
    moment = getattr(event, "timestamp", None)
    if moment is None and isinstance(event, dict):
        moment = event.get("timestamp")
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    if moment is None:
        raise TimelineError("event without a timestamp: %r" % (event,))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    path = getattr(event, "path", None) or (event.get("path") if isinstance(event, dict) else "")
    name = getattr(event, "event", None) or (event.get("event") if isinstance(event, dict) else "")
    source = getattr(event, "source", None) or (event.get("source") if isinstance(event, dict) else "")
    digest = getattr(event, "sha256", None)
    if digest is None and isinstance(event, dict):
        digest = event.get("sha256")
    return {
        "timestamp": _iso(moment),
        "epoch": moment.timestamp(),
        "source": str(source or ""),
        "path": str(path or ""),
        "event": str(name or ""),
        "sha256": digest,
    }


def query_timeline(
    db_path: Any,
    *,
    start: Any = None,
    end: Any = None,
    event: Any = None,
    path: Any = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Read events back, filtered by window, event name and/or path."""

    target = Path(db_path)
    if not target.exists():
        raise TimelineError("timeline database not found: %s" % db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append("epoch >= ?")
        params.append(_epoch(start))
    if end is not None:
        clauses.append("epoch <= ?")
        params.append(_epoch(end))
    if event:
        names = [event] if isinstance(event, str) else list(event)
        clauses.append("event IN (%s)" % ", ".join("?" * len(names)))
        params.extend(names)
    if path:
        clauses.append("path = ?")
        params.append(str(path))
    sql = "SELECT timestamp, source, path, event, sha256, epoch FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY epoch, path, event"
    if limit is not None:
        sql += " LIMIT %d" % int(limit)
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.timestamp()
    return float(value)


def summarise(events: Iterable[Any]) -> dict[str, Any]:
    """Counts used by the CLI summary line."""

    total = 0
    files: set[str] = set()
    by_event: dict[str, int] = {}
    for event in events:
        total += 1
        files.add(str(getattr(event, "path", "") or ""))
        name = str(getattr(event, "event", "") or "")
        by_event[name] = by_event.get(name, 0) + 1
    return {"events": total, "files": len(files), "by_event": by_event}
