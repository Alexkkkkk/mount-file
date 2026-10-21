# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Read-only normalization and CSV export for caller-supplied DFIR timelines.

This module adapts the metadata records in :mod:`mountfile.dfir`, timestamp
records in :mod:`mountfile.dfir_timestomp`, and event-like mappings/objects
(such as OLMA runtime events).  It only reads fields that are already present
on those records.  In particular, an ``evidence_path`` is an identifier, not
a path that this module opens, parses, or changes.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from numbers import Real
from typing import IO, Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

UTC = timezone.utc
CSV_FIELDS = ("timestamp", "event", "source", "evidence_path", "record_id")
_MISSING = object()


@dataclass(frozen=True)
class TimelineEvent(Mapping[str, Any]):
    """One normalized, read-only timeline event.

    ``timestamp`` is always an aware :class:`datetime` in UTC.  ``source`` and
    ``evidence_path`` are copied from the supplied record and never interpreted
    as filesystem locations.  The mapping interface makes the result useful
    to JSON/CSV adapters while the attributes keep the API explicit.
    """

    timestamp: datetime
    event: str
    source: str = ""
    evidence_path: str = ""
    record_id: Optional[str] = None

    _KEYS = CSV_FIELDS

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.timestamp.tzinfo is not UTC:
            raise ValueError("timestamp must be normalized to UTC")

    @property
    def timestamp_utc(self) -> datetime:
        """Compatibility spelling for callers that name the field explicitly."""

        return self.timestamp

    @property
    def event_type(self) -> str:
        """Compatibility spelling for event records using ``event_type``."""

        return self.event

    @property
    def path(self) -> str:
        """Alias for the evidence path used by existing metadata records."""

        return self.evidence_path

    def __getitem__(self, key: str) -> Any:
        if key in ("timestamp", "timestamp_utc"):
            return self.timestamp
        if key in ("event", "event_type"):
            return self.event
        if key in ("source",):
            return self.source
        if key in ("evidence_path", "path"):
            return self.evidence_path
        if key == "record_id":
            return self.record_id
        raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    def as_dict(self) -> dict[str, Any]:
        """Return a detached canonical mapping with the datetime still typed."""

        return {key: self[key] for key in self._KEYS}

    to_dict = as_dict


# Descriptive aliases avoid forcing integrations to guess whether a row is an
# event or a record.  They intentionally share one implementation.
NormalizedTimelineEvent = TimelineEvent
NormalizedTimelineRecord = TimelineEvent
TimelineRecord = TimelineEvent
TimelineRow = TimelineEvent


def _value(
    record: Any, names: Sequence[str], metadata: Optional[Mapping[str, Any]] = None
) -> Any:
    """Read the first present field without mutating or opening the record."""

    mappings: List[Mapping[str, Any]] = []
    if isinstance(record, Mapping):
        mappings.append(record)
    if metadata is not None:
        mappings.append(metadata)

    for name in names:
        for mapping in mappings:
            if name in mapping:
                value = mapping[name]
                if value is not None:
                    return value
            # CSV and hand-written metadata often differ only in case.
            folded = name.casefold()
            for key, value in mapping.items():
                if (
                    isinstance(key, str)
                    and key.casefold() == folded
                    and value is not None
                ):
                    return value
        if not isinstance(record, Mapping) and hasattr(record, name):
            value = getattr(record, name)
            if value is not None and not callable(value):
                return value
        if metadata is not None and hasattr(metadata, name):
            value = getattr(metadata, name)
            if value is not None and not callable(value):
                return value
    return _MISSING


def _metadata(record: Any) -> Optional[Mapping[str, Any]]:
    raw = _value(record, ("metadata", "meta"))
    return raw if isinstance(raw, Mapping) else None


def _text(value: Any) -> str:
    if value is _MISSING or value is None:
        return ""
    try:
        value = os.fspath(value)
    except TypeError:
        pass
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timestamp(value: Any, *, field: str) -> datetime:
    """Convert an epoch/ISO/date value to an aware UTC datetime."""

    if isinstance(value, bool):
        raise TypeError("%s must be a timestamp, not bool" % field)
    if isinstance(value, datetime):
        candidate = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if candidate.utcoffset() is None:
            raise ValueError("%s has no usable timezone" % field)
        return candidate.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, Real):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("%s must not be empty" % field)
        try:
            seconds = float(text)
        except ValueError:
            iso = text
            if iso.endswith(("Z", "z")):
                iso = iso[:-1] + "+00:00"
            try:
                candidate = datetime.fromisoformat(iso)
            except ValueError:
                try:
                    day = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("%s is not a supported timestamp" % field) from exc
                return datetime(day.year, day.month, day.day, tzinfo=UTC)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=UTC)
            return candidate.astimezone(UTC)
    else:
        raise TypeError(
            "%s must be a numeric, ISO, date, or datetime timestamp" % field
        )

    if not math.isfinite(seconds):
        raise ValueError("%s must be finite" % field)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("%s is outside the supported datetime range" % field) from exc


def normalize_timestamp(value: Any) -> datetime:
    """Return one caller-supplied timestamp as an aware UTC datetime."""

    return _timestamp(value, field="timestamp")


def _timestamp_values(
    record: Any, metadata: Optional[Mapping[str, Any]]
) -> List[Tuple[str, Any]]:
    """Extract one or more timestamp fields from metadata/event records."""

    values: List[Tuple[str, Any]] = []
    seen: set[str] = set()

    # A generic event timestamp wins its event name and is emitted once.
    generic_names = (
        "timestamp",
        "timestamp_utc",
        "occurred_at",
        "event_time",
        "time",
        "ts",
        "datetime",
        "date",
    )
    generic = _value(record, generic_names, metadata)
    if generic is not _MISSING:
        values.append(("event", generic))
        seen.add("event")

    # These aliases cover TimelineRecord and TimestampRecord without reading a
    # filesystem.  The canonical labels retain the forensic meaning of each
    # timestamp rather than exposing whichever spelling a producer happened to
    # use.
    fields = (
        ("created", ("created", "creation", "creation_time", "created_at")),
        ("modified", ("modified", "modification", "modification_time", "modified_at")),
        ("accessed", ("accessed", "access", "access_time", "accessed_at")),
        ("changed", ("changed", "change_time", "changed_time")),
        ("birth", ("birth", "birth_time")),
        ("mtime", ("mtime",)),
        ("atime", ("atime",)),
        ("ctime", ("ctime",)),
    )
    for label, names in fields:
        if label in seen:
            continue
        value = _value(record, names, metadata)
        if value is not _MISSING:
            values.append((label, value))
            seen.add(label)
    return values


def _coerce_events(record: Any) -> List[TimelineEvent]:
    if isinstance(record, TimelineEvent):
        # It is already immutable and normalized; returning it avoids changing
        # identity while normalization still sorts a fresh result list.
        return [record]

    metadata = _metadata(record)
    source_value = _value(
        record, ("source", "origin", "provider", "collector", "module"), metadata
    )
    path_value = _value(
        record,
        ("evidence_path", "evidence", "path", "file_path", "filename", "name"),
        metadata,
    )
    event_value = _value(
        record, ("event_type", "event", "kind", "type", "action", "operation"), metadata
    )
    record_id_value = _value(record, ("record_id", "id", "inode"), metadata)
    source = _text(source_value)
    evidence_path = _text(path_value)
    explicit_event = _text(event_value) if event_value is not _MISSING else ""
    record_id = None if record_id_value is _MISSING else _text(record_id_value)

    result: List[TimelineEvent] = []
    for label, value in _timestamp_values(record, metadata):
        # For a generic event, preserve the producer's explicit kind/action;
        # metadata fields use their stable canonical labels.
        event = explicit_event or label
        result.append(
            TimelineEvent(
                timestamp=_timestamp(value, field=label),
                event=event,
                source=source,
                evidence_path=evidence_path,
                record_id=record_id,
            )
        )
    return result


def normalize_timeline(records: Iterable[Any]) -> List[TimelineEvent]:
    """Normalize and deterministically sort caller-supplied timeline records.

    Inputs may be mappings or objects from existing modules, including
    ``mountfile.dfir.TimelineRecord``, ``dfir_timestomp.TimestampRecord``, and
    OLMA-style objects with ``kind``, ``path`` and ``ts`` fields.  One row is
    emitted for each timestamp present in a metadata record.  No evidence path
    is opened, stat'ed, parsed, or modified.
    """

    normalized: List[TimelineEvent] = []
    if isinstance(records, (Mapping, TimelineEvent)):
        iterable: Iterable[Any] = (records,)
    else:
        iterable = records
    for record in iterable:
        normalized.extend(_coerce_events(record))
    normalized.sort(
        key=lambda item: (
            item.timestamp,
            item.event,
            item.source,
            item.evidence_path,
            "" if item.record_id is None else item.record_id,
        )
    )
    return normalized


# Common descriptive spellings are aliases, not alternate code paths.
normalize_events = normalize_timeline
normalize_records = normalize_timeline
build_timeline = normalize_timeline
normalise_timeline = normalize_timeline


def _csv_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _csv_row(event: TimelineEvent) -> dict[str, str]:
    return {
        "timestamp": _csv_timestamp(event.timestamp),
        "event": event.event,
        "source": event.source,
        "evidence_path": event.evidence_path,
        "record_id": "" if event.record_id is None else event.record_id,
    }


def _write_csv(events: Sequence[TimelineEvent], stream: IO[str]) -> None:
    writer = csv.DictWriter(
        stream,
        fieldnames=list(CSV_FIELDS),
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(_csv_row(event) for event in events)


def export_timeline_csv(
    records: Iterable[Any], destination: Union[str, os.PathLike, IO[str]]
) -> int:
    """Write normalized events as safely quoted UTF-8 CSV and return row count.

    ``destination`` may be a path or a text stream.  The exporter uses the
    standard CSV writer with all fields quoted, preserving commas, quotes,
    newlines, Unicode, and formula-looking values as data.  It opens only the
    requested output destination; evidence paths in records are never opened.
    """

    events = normalize_timeline(records)
    if hasattr(destination, "write"):
        _write_csv(events, destination)  # type: ignore[arg-type]
        return len(events)
    with open(destination, "w", newline="", encoding="utf-8") as stream:
        _write_csv(events, stream)
    return len(events)


write_timeline_csv = export_timeline_csv
export_csv = export_timeline_csv
timeline_to_csv = export_timeline_csv


__all__ = [
    "CSV_FIELDS",
    "NormalizedTimelineEvent",
    "NormalizedTimelineRecord",
    "TimelineEvent",
    "TimelineRecord",
    "TimelineRow",
    "build_timeline",
    "export_csv",
    "export_timeline_csv",
    "normalize_events",
    "normalize_records",
    "normalize_timestamp",
    "normalize_timeline",
    "normalise_timeline",
    "timeline_to_csv",
    "write_timeline_csv",
]
