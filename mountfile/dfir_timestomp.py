# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Deterministic, read-only timestamp consistency analysis.

The API in this module analyses caller-supplied records only.  It never opens a
path, reads an evidence image, or changes a record.  Findings are conservative
heuristics: an ordering or boundary anomaly is a reason for review, not proof
of timestomping and not an assertion about any particular operating system or
filesystem.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from numbers import Real
from typing import Any, Iterable, Iterator, List, Optional, Tuple, Union


TimestampValue = Union[Real, date]


@dataclass(frozen=True)
class TimestampRecord:
    """Explicit timestamp input for one filesystem or timeline record.

    ``created``, ``modified`` and ``accessed`` may be numeric epoch seconds or
    :class:`datetime` values.  Numeric values are intentionally not interpreted
    as a particular filesystem's timestamp format.  ``record_id`` is optional
    because some timelines provide only a path.
    """

    path: str
    created: Optional[TimestampValue] = None
    modified: Optional[TimestampValue] = None
    accessed: Optional[TimestampValue] = None
    record_id: Optional[str] = None

    @property
    def creation_time(self) -> Optional[TimestampValue]:
        """Alias for callers that use the longer creation-time name."""

        return self.created

    @property
    def modification_time(self) -> Optional[TimestampValue]:
        """Alias for callers that use the longer modification-time name."""

        return self.modified

    @property
    def access_time(self) -> Optional[TimestampValue]:
        """Alias for callers that use the longer access-time name."""

        return self.accessed


# A short alias is useful when the surrounding code calls these records
# "timestomp records" rather than timeline records.
TimestompRecord = TimestampRecord


@dataclass(frozen=True)
class TimestompThresholds:
    """Configurable, unit-explicit thresholds for :func:`analyze_timestomp`.

    ``ordering_tolerance`` and ``boundary_tolerance`` are seconds.  Boundary
    values are opt-in because epoch and sentinel values vary by source format;
    no operating-system-specific boundary is assumed by default.
    """

    ordering_tolerance: float = 2.0
    boundary_values: Tuple[TimestampValue, ...] = ()
    boundary_tolerance: float = 0.0
    minimum_timestamp: Optional[TimestampValue] = None
    maximum_timestamp: Optional[TimestampValue] = None

    @property
    def ordering_tolerance_seconds(self) -> float:
        """Long-form alias for ``ordering_tolerance``."""

        return self.ordering_tolerance

    @property
    def boundary_tolerance_seconds(self) -> float:
        """Long-form alias for ``boundary_tolerance``."""

        return self.boundary_tolerance


# Common configuration spellings remain available without creating separate
# implementations or changing the canonical field names.
TimestampThresholds = TimestompThresholds
TimestompConfig = TimestompThresholds


@dataclass(frozen=True)
class TimestampFinding(Mapping[str, Any]):
    """One deterministic, structured timestamp anomaly finding.

    The mapping interface makes a finding convenient for JSON adapters while
    attributes keep the result explicit for Python callers.  ``timestamps`` is
    a new dictionary containing only the values involved in this finding.
    """

    path: str
    record_id: Optional[str]
    severity: str
    reason: str
    timestamps: Mapping[str, TimestampValue]

    _KEYS = ("path", "record_id", "severity", "reason", "timestamps")

    def __getitem__(self, key: str) -> Any:
        if key == "path":
            return self.path
        if key == "record_id":
            return self.record_id
        if key == "severity":
            return self.severity
        if key == "reason":
            return self.reason
        if key == "timestamps":
            return self.timestamps
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    def as_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-adapter-friendly representation."""

        return {
            "path": self.path,
            "record_id": self.record_id,
            "severity": self.severity,
            "reason": self.reason,
            "timestamps": dict(self.timestamps),
        }


# ``TimestompFinding`` reads naturally for clients using the feature name.
TimestompFinding = TimestampFinding


def _timestamp_seconds(value: TimestampValue, *, field: str) -> float:
    """Convert a supported value to seconds without changing the value itself."""

    if isinstance(value, bool):
        raise TypeError("%s must be a numeric timestamp or datetime, not bool" % field)
    if isinstance(value, datetime):
        # Naive datetimes have no timezone claim in the input.  Treating them as
        # UTC here keeps analysis deterministic instead of consulting the host
        # machine's local timezone.
        candidate = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        seconds = candidate.timestamp()
    elif isinstance(value, date):
        seconds = datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp()
    elif isinstance(value, Real):
        seconds = float(value)
    else:
        raise TypeError("%s must be a numeric timestamp or datetime" % field)
    if not math.isfinite(seconds):
        raise ValueError("%s must be finite" % field)
    return seconds


def _value_from(record: Any, names: Tuple[str, ...]) -> Any:
    """Read a field from an explicit record or mapping without modifying it."""

    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _coerce_record(record: Any) -> TimestampRecord:
    if isinstance(record, TimestampRecord):
        return record
    path = _value_from(record, ("path", "file_path"))
    if path is None:
        raise TypeError("each timestamp record must provide a path")
    record_id = _value_from(record, ("record_id", "id"))
    return TimestampRecord(
        path=str(path),
        record_id=None if record_id is None else str(record_id),
        created=_value_from(record, ("created", "creation", "creation_time", "birth", "birth_time", "ctime")),
        modified=_value_from(record, ("modified", "modification", "modification_time", "mtime")),
        accessed=_value_from(record, ("accessed", "access", "access_time", "atime")),
    )


def _validate_nonnegative(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError("%s must be a non-negative number" % name)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("%s must be a non-negative number" % name) from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("%s must be finite and non-negative" % name)
    return result


def _validate_thresholds(thresholds: TimestompThresholds) -> TimestompThresholds:
    if not isinstance(thresholds, TimestompThresholds):
        raise TypeError("thresholds must be a TimestompThresholds instance")
    ordering = _validate_nonnegative(thresholds.ordering_tolerance, name="ordering_tolerance")
    boundary_tolerance = _validate_nonnegative(
        thresholds.boundary_tolerance, name="boundary_tolerance"
    )
    boundaries = tuple(thresholds.boundary_values)
    for index, boundary in enumerate(boundaries):
        _timestamp_seconds(boundary, field="boundary_values[%d]" % index)
    minimum = (
        None
        if thresholds.minimum_timestamp is None
        else _timestamp_seconds(thresholds.minimum_timestamp, field="minimum_timestamp")
    )
    maximum = (
        None
        if thresholds.maximum_timestamp is None
        else _timestamp_seconds(thresholds.maximum_timestamp, field="maximum_timestamp")
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum_timestamp must not exceed maximum_timestamp")
    # Return a normalized copy so a caller's mutable boundary list, if passed
    # despite the tuple annotation, cannot affect an in-progress analysis.
    return TimestompThresholds(
        ordering_tolerance=ordering,
        boundary_values=boundaries,
        boundary_tolerance=boundary_tolerance,
        minimum_timestamp=thresholds.minimum_timestamp,
        maximum_timestamp=thresholds.maximum_timestamp,
    )


def _finding(
    record: TimestampRecord,
    *,
    severity: str,
    reason: str,
    timestamps: Mapping[str, TimestampValue],
) -> TimestampFinding:
    # Copy the involved values so retaining or changing a caller-owned mapping
    # cannot alter a returned finding.  The evidence record itself is untouched.
    return TimestampFinding(
        path=record.path,
        record_id=record.record_id,
        severity=severity,
        reason=reason,
        timestamps=dict(timestamps),
    )


def analyze_timestomp(
    records: Iterable[Any],
    thresholds: Optional[TimestompThresholds] = None,
    *,
    ordering_tolerance: Optional[float] = None,
    boundary_values: Optional[Iterable[TimestampValue]] = None,
    boundary_tolerance: Optional[float] = None,
    minimum_timestamp: Optional[TimestampValue] = None,
    maximum_timestamp: Optional[TimestampValue] = None,
    skew: Optional[float] = None,
) -> List[TimestampFinding]:
    """Return timestamp-order and configured-boundary findings.

    ``records`` is an iterable of explicit :class:`TimestampRecord` objects.
    Mapping/object records are accepted as a narrow adapter for existing
    timelines, using ``path`` plus the aliases ``created``/``birth``,
    ``modified``/``mtime`` and ``accessed``/``atime``.  No path is opened.

    The normal ordering is ``created <= modified <= accessed``.  A comparison
    is reported only when it exceeds ``ordering_tolerance`` seconds.  Boundary
    and range checks are opt-in through ``TimestompThresholds`` or the direct
    keyword overrides.  ``skew`` is a compatibility spelling for ordering
    tolerance and may not be combined with ``ordering_tolerance``.
    """

    if thresholds is None:
        thresholds = TimestompThresholds()
    elif not isinstance(thresholds, TimestompThresholds):
        raise TypeError("thresholds must be a TimestompThresholds instance")

    if skew is not None:
        if ordering_tolerance is not None:
            raise ValueError("use only one of skew and ordering_tolerance")
        ordering_tolerance = skew
    if ordering_tolerance is not None:
        thresholds = TimestompThresholds(
            ordering_tolerance=ordering_tolerance,
            boundary_values=thresholds.boundary_values,
            boundary_tolerance=thresholds.boundary_tolerance,
            minimum_timestamp=thresholds.minimum_timestamp,
            maximum_timestamp=thresholds.maximum_timestamp,
        )
    if boundary_values is not None:
        thresholds = TimestompThresholds(
            ordering_tolerance=thresholds.ordering_tolerance,
            boundary_values=tuple(boundary_values),
            boundary_tolerance=thresholds.boundary_tolerance,
            minimum_timestamp=thresholds.minimum_timestamp,
            maximum_timestamp=thresholds.maximum_timestamp,
        )
    if boundary_tolerance is not None:
        thresholds = TimestompThresholds(
            ordering_tolerance=thresholds.ordering_tolerance,
            boundary_values=thresholds.boundary_values,
            boundary_tolerance=boundary_tolerance,
            minimum_timestamp=thresholds.minimum_timestamp,
            maximum_timestamp=thresholds.maximum_timestamp,
        )
    if minimum_timestamp is not None:
        thresholds = TimestompThresholds(
            ordering_tolerance=thresholds.ordering_tolerance,
            boundary_values=thresholds.boundary_values,
            boundary_tolerance=thresholds.boundary_tolerance,
            minimum_timestamp=minimum_timestamp,
            maximum_timestamp=thresholds.maximum_timestamp,
        )
    if maximum_timestamp is not None:
        thresholds = TimestompThresholds(
            ordering_tolerance=thresholds.ordering_tolerance,
            boundary_values=thresholds.boundary_values,
            boundary_tolerance=thresholds.boundary_tolerance,
            minimum_timestamp=thresholds.minimum_timestamp,
            maximum_timestamp=maximum_timestamp,
        )

    thresholds = _validate_thresholds(thresholds)
    boundary_seconds = tuple(
        _timestamp_seconds(boundary, field="boundary_values[%d]" % index)
        for index, boundary in enumerate(thresholds.boundary_values)
    )
    findings: List[TimestampFinding] = []

    for item in records:
        record = _coerce_record(item)
        original = {
            "created": record.created,
            "modified": record.modified,
            "accessed": record.accessed,
        }
        seconds = {
            name: _timestamp_seconds(value, field=name)
            for name, value in original.items()
            if value is not None
        }

        for earlier, later in (("created", "modified"), ("modified", "accessed")):
            if earlier not in seconds or later not in seconds:
                continue
            delta = seconds[earlier] - seconds[later]
            if delta > thresholds.ordering_tolerance:
                findings.append(
                    _finding(
                        record,
                        severity="high",
                        reason=(
                            "%s occurs after %s by %.3f seconds, exceeding the "
                            "configured ordering tolerance of %.3f seconds"
                        )
                        % (earlier, later, delta, thresholds.ordering_tolerance),
                        timestamps={earlier: original[earlier], later: original[later]},
                    )
                )

        for name, value in original.items():
            if value is None:
                continue
            value_seconds = seconds[name]
            if thresholds.minimum_timestamp is not None:
                minimum = _timestamp_seconds(
                    thresholds.minimum_timestamp, field="minimum_timestamp"
                )
                if value_seconds < minimum:
                    findings.append(
                        _finding(
                            record,
                            severity="medium",
                            reason=(
                                "%s is below the configured minimum timestamp %.3f"
                                % (name, minimum)
                            ),
                            timestamps={name: value},
                        )
                    )
            if thresholds.maximum_timestamp is not None:
                maximum = _timestamp_seconds(
                    thresholds.maximum_timestamp, field="maximum_timestamp"
                )
                if value_seconds > maximum:
                    findings.append(
                        _finding(
                            record,
                            severity="medium",
                            reason=(
                                "%s is above the configured maximum timestamp %.3f"
                                % (name, maximum)
                            ),
                            timestamps={name: value},
                        )
                    )
            for boundary, boundary_value in zip(thresholds.boundary_values, boundary_seconds):
                if abs(value_seconds - boundary_value) <= thresholds.boundary_tolerance:
                    findings.append(
                        _finding(
                            record,
                            severity="medium",
                            reason=(
                                "%s is at or near the configured boundary %.3f "
                                "(within %.3f seconds)"
                            )
                            % (name, boundary_value, thresholds.boundary_tolerance),
                            timestamps={name: value},
                        )
                    )

    return findings


# Descriptive aliases keep the same deterministic implementation available to
# callers who prefer "timestamps" or "inconsistencies" over "timestomp".
analyze_timestamps = analyze_timestomp
find_timestamp_inconsistencies = analyze_timestomp
detect_timestamp_anomalies = analyze_timestomp


__all__ = [
    "TimestampFinding",
    "TimestampRecord",
    "TimestampThresholds",
    "TimestompConfig",
    "TimestompFinding",
    "TimestompRecord",
    "TimestompThresholds",
    "analyze_timestamps",
    "analyze_timestomp",
    "detect_timestamp_anomalies",
    "find_timestamp_inconsistencies",
]
