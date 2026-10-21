# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Timestomp detection (Task 1.4, issue #87).

Read-only heuristics over filesystem metadata.  Four independent rules from the
epic are implemented, each producing its own reason string so an analyst can see
*why* a file was flagged:

``si_mtime_lt_fn_mtime``
    ``$STANDARD_INFORMATION`` modification time older than ``$FILE_NAME`` --
    the classic ``timestomp``/``SetMace`` artefact.  Requires a record that
    carries both values (an MFT parser or an injected mapping); pytsk3 alone
    exposes only one set, so this rule stays silent unless the caller supplies
    the pair.

``future_timestamp``
    Any of the four metadata timestamps lies after "now" plus a tolerance.

``equal_mac``
    ``mtime == atime == ctime`` exactly -- almost never true for a real file.

``zero_nanoseconds``
    NTFS stores 100 ns precision; a genuine modern write has non-zero
    sub-second bits, while many timestomping tools round them off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from .timeline import iter_file_entries

UTC = timezone.utc

REASON_SI_LT_FN = "si_mtime_lt_fn_mtime"
REASON_FUTURE = "future_timestamp"
REASON_EQUAL_MAC = "mtime_eq_atime_eq_ctime"
REASON_ZERO_NANO = "zero_nanoseconds"

#: Tolerance (seconds) applied to the "future" rule and to time equality.
DEFAULT_SKEW = 2.0


class TimestompError(RuntimeError):
    """Raised for an unusable timestomp request."""


@dataclass
class TimestompFinding:
    """One suspicious file with the reasons that flagged it."""

    path: str
    reasons: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reasons": list(self.reasons),
            "detail": dict(self.detail),
        }


def _seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
        return stamp.timestamp()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _field(record: Any, *names: str) -> Any:
    for name in names:
        if isinstance(record, Mapping) and name in record:
            return record[name]
        if hasattr(record, name):
            return getattr(record, name)
    return None


def analyse_timestamps(
    record: Any,
    *,
    now: Optional[float] = None,
    skew: float = DEFAULT_SKEW,
    path: Optional[str] = None,
) -> Optional[TimestompFinding]:
    """Apply every rule to one record; returns ``None`` when it looks clean.

    ``record`` may be a mapping or an object exposing any of ``path``,
    ``mtime``/``atime``/``ctime``/``crtime``, the matching ``*_nano`` fields and
    the optional ``si_mtime`` / ``fn_mtime`` pair.
    """

    if skew < 0:
        raise TimestompError("skew must be non-negative")
    reference = float(now) if now is not None else datetime.now(UTC).timestamp()
    label = path or str(_field(record, "path", "name") or "<unknown>")

    mtime = _seconds(_field(record, "mtime", "modification_time"))
    atime = _seconds(_field(record, "atime", "access_time"))
    ctime = _seconds(_field(record, "ctime", "change_time", "metadata_time"))
    crtime = _seconds(_field(record, "crtime", "birth", "creation_time"))

    reasons: list[str] = []
    detail: dict[str, Any] = {}

    for name, value in (
        ("mtime", mtime),
        ("atime", atime),
        ("ctime", ctime),
        ("crtime", crtime),
    ):
        if value is not None and value > reference + skew:
            reasons.append(REASON_FUTURE)
            detail["future"] = name
            break

    if mtime is not None and atime is not None and ctime is not None:
        if abs(mtime - atime) <= skew and abs(mtime - ctime) <= skew:
            reasons.append(REASON_EQUAL_MAC)
            detail["equal"] = {"mtime": mtime, "atime": atime, "ctime": ctime}

    for name in ("mtime", "atime", "ctime", "crtime"):
        nano = _field(record, "%s_nano" % name)
        if nano in (0, "0"):
            reasons.append(REASON_ZERO_NANO)
            detail["zero_nano"] = name
            break

    si_mtime = _seconds(_field(record, "si_mtime", "standard_information_mtime"))
    fn_mtime = _seconds(_field(record, "fn_mtime", "file_name_mtime"))
    if si_mtime is not None and fn_mtime is not None and si_mtime < fn_mtime - skew:
        reasons.append(REASON_SI_LT_FN)
        detail["si_fn"] = {"si_mtime": si_mtime, "fn_mtime": fn_mtime}

    if not reasons:
        return None
    return TimestompFinding(
        path=label, reasons=tuple(dict.fromkeys(reasons)), detail=detail
    )


def analyse_records(
    records: Iterable[Any],
    *,
    now: Optional[float] = None,
    skew: float = DEFAULT_SKEW,
) -> list[TimestompFinding]:
    """Apply the rules to many records."""

    findings: list[TimestompFinding] = []
    for record in records:
        finding = analyse_timestamps(record, now=now, skew=skew)
        if finding is not None:
            findings.append(finding)
    return findings


def analyse_fs(
    fs: Any,
    *,
    now: Optional[float] = None,
    skew: float = DEFAULT_SKEW,
    mft_pairs: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[TimestompFinding]:
    """Apply the rules to every file of an open pytsk3 filesystem.

    ``mft_pairs`` optionally maps a path to ``{"si_mtime": ..., "fn_mtime": ...}``
    so a caller with an MFT parser can enable the ``$SI`` vs ``$FN`` rule.
    """

    findings: list[TimestompFinding] = []
    for path, entry in iter_file_entries(fs):
        meta = entry.info.meta
        record: dict[str, Any] = {
            "path": path,
            "mtime": getattr(meta, "mtime", None),
            "atime": getattr(meta, "atime", None),
            "ctime": getattr(meta, "ctime", None),
            "crtime": getattr(meta, "crtime", None),
            "mtime_nano": getattr(meta, "mtime_nano", None),
            "atime_nano": getattr(meta, "atime_nano", None),
            "ctime_nano": getattr(meta, "ctime_nano", None),
            "crtime_nano": getattr(meta, "crtime_nano", None),
        }
        if mft_pairs and path in mft_pairs:
            record.update(mft_pairs[path])
        finding = analyse_timestamps(record, now=now, skew=skew, path=path)
        if finding is not None:
            findings.append(finding)
    return findings


def collect_timestomp(image_path: Any, **kwargs: Any) -> list[TimestompFinding]:
    """Convenience wrapper: open the image read-only and run :func:`analyse_fs`."""

    from .timeline import open_tsk_fs

    return analyse_fs(open_tsk_fs(image_path), **kwargs)


def summarise(findings: Sequence[TimestompFinding]) -> dict[str, int]:
    """Count findings per reason -- handy for the copilot summary."""

    counts: dict[str, int] = {}
    for finding in findings:
        for reason in finding.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts
