# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Timestamp manipulation detection (issue #87, Task 1.4).

:func:`analyse_timestamps` is the deterministic rule engine and works on plain
mappings, so it can be unit-tested without an image.  :func:`collect_timestomp`
feeds it from a real image.

Rules, all of them conservative:

* ``future`` — a timestamp later than the collection time;
* ``equal_mac`` — mtime, atime and ctime exactly equal, the classic signature
  of a tool that writes one value into every field;
* ``si_lt_fn`` — the ``$STANDARD_INFORMATION`` mtime precedes the
  ``$FILE_NAME`` mtime, which NTFS never does on its own;
* ``zero_nano`` — an NTFS timestamp whose 100 ns remainder is exactly zero.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

from .fs_walk import iter_files

__all__ = [
    "DEFAULT_SKEW",
    "REASON_EQUAL_MAC",
    "REASON_FUTURE",
    "REASON_SI_LT_FN",
    "REASON_ZERO_NANO",
    "TimestompError",
    "TimestompFinding",
    "analyse_timestamps",
    "collect_timestomp",
    "summarise",
]

UTC = timezone.utc

REASON_FUTURE = "future_timestamp"
REASON_EQUAL_MAC = "equal_mac_times"
REASON_SI_LT_FN = "si_mtime_before_fn_mtime"
REASON_ZERO_NANO = "zero_nanoseconds"

#: Seconds of tolerance applied to every comparison.
DEFAULT_SKEW = 2.0

_TIMESTAMP_FIELDS = ("crtime", "mtime", "atime", "ctime")


class TimestompError(RuntimeError):
    """Raised for an unusable request (e.g. a negative skew)."""


@dataclass(frozen=True)
class TimestompFinding:
    """One suspicious file, with every reason that fired."""

    path: str
    reasons: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


def _seconds(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    return None


def analyse_timestamps(
    record: Mapping[str, Any],
    *,
    now: Any = None,
    skew: float = DEFAULT_SKEW,
) -> Optional[TimestompFinding]:
    """Return the finding for one record, or ``None`` when it looks clean."""

    try:
        tolerance = float(skew)
    except (TypeError, ValueError) as exc:
        raise TimestompError("skew must be a number: %r" % (skew,)) from exc
    if tolerance < 0:
        raise TimestompError("skew must not be negative: %r" % (skew,))

    reference = _seconds(now)
    if reference is None:
        reference = time.time()
    path = str(record.get("path", "") or "")
    reasons: list[str] = []
    details: dict[str, Any] = {}

    stamps = {name: _seconds(record.get(name)) for name in _TIMESTAMP_FIELDS}
    present = {name: value for name, value in stamps.items() if value is not None}

    for name, value in present.items():
        if value > reference + tolerance:
            reasons.append(REASON_FUTURE)
            details["future_field"] = name
            details["future_value"] = value
            details["reference"] = reference
            break

    triple = [stamps.get(name) for name in ("mtime", "atime", "ctime")]
    if all(value is not None for value in triple) and len(set(triple)) == 1:
        reasons.append(REASON_EQUAL_MAC)
        details["equal_value"] = triple[0]

    si_mtime = _seconds(record.get("si_mtime", record.get("mtime")))
    fn_mtime = _seconds(record.get("fn_mtime"))
    if si_mtime is not None and fn_mtime is not None and si_mtime < fn_mtime - tolerance:
        reasons.append(REASON_SI_LT_FN)
        details["si_mtime"] = si_mtime
        details["fn_mtime"] = fn_mtime

    for name in _TIMESTAMP_FIELDS:
        key = "%s_nano" % name
        if key not in record:
            continue
        nanos = record.get(key)
        if nanos is None:
            continue
        try:
            value = int(nanos)
        except (TypeError, ValueError):
            continue
        if value == 0:
            reasons.append(REASON_ZERO_NANO)
            details["zero_nano_field"] = name
            break

    if not reasons:
        return None
    return TimestompFinding(path=path, reasons=tuple(reasons), details=details)


def _record_from_meta(path: str, meta: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"path": path}
    for name in _TIMESTAMP_FIELDS:
        value = getattr(meta, name, None)
        if value is not None:
            record[name] = value
        nano = getattr(meta, "%s_nano" % name, None)
        if nano is not None:
            # Only records that really expose nanoseconds are checked, so a
            # filesystem without the field is never flagged as zeroed.
            record["%s_nano" % name] = nano
    name_meta = getattr(getattr(meta, "fn", None), "meta", None)
    return record


def collect_timestomp(
    image: Any,
    *,
    offset: int = 0,
    now: Any = None,
    skew: float = DEFAULT_SKEW,
    limit: Optional[int] = None,
) -> list[TimestompFinding]:
    """Detect timestamp manipulation in a real image."""

    if float(skew) < 0:
        raise TimestompError("skew must not be negative: %r" % (skew,))
    try:
        import pytsk3  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        raise TimestompError(
            "scanning an image needs the optional 'pytsk3' package; install it "
            "with 'python -m pip install pytsk3'"
        ) from exc
    path = Path(image)
    if not path.exists():
        raise TimestompError("image not found: %s" % image)
    try:
        img = pytsk3.Img_Info(str(path))
        fs = pytsk3.FS_Info(img, int(offset))
    except Exception as exc:
        raise TimestompError("cannot open %s as a filesystem: %s" % (image, exc)) from exc

    findings: list[TimestompFinding] = []
    for file_path, entry, meta in iter_files(fs):
        record = _record_from_meta(file_path, meta)
        name_meta = getattr(getattr(entry, "info", None), "name", None)
        fn_meta = getattr(name_meta, "meta", None)
        fn_mtime = _seconds(getattr(fn_meta, "mtime", None))
        if fn_mtime is not None:
            record["fn_mtime"] = fn_mtime
        finding = analyse_timestamps(record, now=now, skew=skew)
        if finding is not None:
            findings.append(finding)
        if limit is not None and len(findings) >= int(limit):
            break
    return findings


def summarise(findings: Iterable[TimestompFinding]) -> dict[str, int]:
    """Count how often each reason fired."""

    counts: dict[str, int] = {}
    for finding in findings:
        for reason in finding.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts
