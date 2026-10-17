# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Fast, metadata-only forensic copilot summaries.

The copilot deliberately sits below any expensive analysis.  Callers inject
providers for an already-derived image fingerprint, timeline events, and
anomaly findings; this module never opens a path, reads an image, imports a
model client, or performs network I/O.  ``build_summary`` is deterministic and
bounded, while ``build_summary_background`` provides a one-worker entry point
for UI callers that must not block their event loop.
"""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


SECTION_NAMES = ("image", "timeline", "anomalies")
DEFAULT_MAX_ITEMS = 100
MAX_MAX_ITEMS = 1000
MAX_TEXT_LENGTH = 512
MAX_NESTED_FIELDS = 128
MAX_NESTED_DEPTH = 6
_MISSING = object()

Provider = Callable[[], Any]


class CopilotSummaryBuilder:
    """Build a stable summary from caller-owned, already-derived evidence.

    Providers are ordinary zero-argument callables.  They are called only by
    :meth:`build` or :meth:`build_background`; constructing the builder does
    no work.  A provider may also be a direct value, which is useful when the
    caller already has all evidence in memory.

    ``max_items`` bounds each collection independently.  A provider failure is
    represented in that section instead of aborting the other sections.
    """

    def __init__(
        self,
        image_provider: Optional[Provider] = None,
        timeline_provider: Optional[Provider] = None,
        anomalies_provider: Optional[Provider] = None,
        *,
        anomaly_provider: Optional[Provider] = None,
        image_fingerprint_provider: Optional[Provider] = None,
        timeline_events_provider: Optional[Provider] = None,
        anomaly_findings_provider: Optional[Provider] = None,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        self.image_provider = _coalesce_provider(
            image_provider, image_fingerprint_provider, "image"
        )
        self.timeline_provider = _coalesce_provider(
            timeline_provider, timeline_events_provider, "timeline"
        )
        self.anomalies_provider = _coalesce_provider(
            anomalies_provider, anomaly_provider, "anomalies"
        )
        self.anomaly_findings_provider = anomaly_findings_provider
        if self.anomalies_provider is not None and self.anomaly_findings_provider is not None:
            raise ValueError("provide only one anomalies provider")
        if self.anomalies_provider is None:
            self.anomalies_provider = self.anomaly_findings_provider
        self.max_items = _bounded_max_items(max_items)

    def build(
        self,
        image_fingerprint: Any = _MISSING,
        timeline_events: Any = _MISSING,
        anomaly_findings: Any = _MISSING,
    ) -> Dict[str, Dict[str, Any]]:
        """Return ``image``, ``timeline``, and ``anomalies`` sections.

        Explicit arguments take precedence over providers.  ``None`` is a
        valid explicit empty value and therefore does not invoke a provider.
        All returned values are JSON-compatible and raw byte values are
        replaced with a redaction marker before they can leave this module.
        """

        image_value, image_error = _resolve(
            self.image_provider, image_fingerprint
        )
        timeline_value, timeline_error = _resolve(
            self.timeline_provider, timeline_events
        )
        anomalies_value, anomalies_error = _resolve(
            self.anomalies_provider, anomaly_findings
        )

        return {
            "image": _image_section(image_value, image_error),
            "timeline": _collection_section(
                "events", timeline_value, timeline_error, self.max_items
            ),
            "anomalies": _collection_section(
                "findings", anomalies_value, anomalies_error, self.max_items
            ),
        }

    # Descriptive spellings kept as aliases, not separate execution paths.
    summarize = build
    build_summary = build

    def build_background(
        self,
        image_fingerprint: Any = _MISSING,
        timeline_events: Any = _MISSING,
        anomaly_findings: Any = _MISSING,
        *,
        executor: Optional[Executor] = None,
    ) -> Future:
        """Schedule one bounded summary build and return immediately.

        An injected executor is never shut down by this method.  Without one,
        a private one-worker executor is created for this invocation and
        cleaned up after the future finishes.
        """

        return _submit_background(
            self.build,
            (
                image_fingerprint,
                timeline_events,
                anomaly_findings,
            ),
            executor=executor,
        )


# A short class name is convenient for integrations while retaining the
# explicit public spelling used in documentation.
ForensicCopilot = CopilotSummaryBuilder
SummaryBuilder = CopilotSummaryBuilder


def build_summary(
    image_fingerprint: Any = _MISSING,
    timeline_events: Any = _MISSING,
    anomaly_findings: Any = _MISSING,
    *,
    image_provider: Optional[Provider] = None,
    timeline_provider: Optional[Provider] = None,
    anomalies_provider: Optional[Provider] = None,
    anomaly_provider: Optional[Provider] = None,
    image_fingerprint_provider: Optional[Provider] = None,
    timeline_events_provider: Optional[Provider] = None,
    anomaly_findings_provider: Optional[Provider] = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Dict[str, Dict[str, Any]]:
    """Build a summary in one call without inspecting or transmitting files."""

    builder = CopilotSummaryBuilder(
        image_provider=image_provider,
        timeline_provider=timeline_provider,
        anomalies_provider=anomalies_provider,
        anomaly_provider=anomaly_provider,
        image_fingerprint_provider=image_fingerprint_provider,
        timeline_events_provider=timeline_events_provider,
        anomaly_findings_provider=anomaly_findings_provider,
        max_items=max_items,
    )
    return builder.build(image_fingerprint, timeline_events, anomaly_findings)


def build_summary_background(
    image_fingerprint: Any = _MISSING,
    timeline_events: Any = _MISSING,
    anomaly_findings: Any = _MISSING,
    *,
    image_provider: Optional[Provider] = None,
    timeline_provider: Optional[Provider] = None,
    anomalies_provider: Optional[Provider] = None,
    anomaly_provider: Optional[Provider] = None,
    image_fingerprint_provider: Optional[Provider] = None,
    timeline_events_provider: Optional[Provider] = None,
    anomaly_findings_provider: Optional[Provider] = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    executor: Optional[Executor] = None,
) -> Future:
    """Submit a summary build to a bounded background worker and return a future."""

    builder = CopilotSummaryBuilder(
        image_provider=image_provider,
        timeline_provider=timeline_provider,
        anomalies_provider=anomalies_provider,
        anomaly_provider=anomaly_provider,
        image_fingerprint_provider=image_fingerprint_provider,
        timeline_events_provider=timeline_events_provider,
        anomaly_findings_provider=anomaly_findings_provider,
        max_items=max_items,
    )
    return builder.build_background(
        image_fingerprint,
        timeline_events,
        anomaly_findings,
        executor=executor,
    )


# Compatibility spelling for callers that use the full feature name.
build_forensic_summary = build_summary
build_forensic_summary_background = build_summary_background


def summary_to_markdown(summary: Mapping[str, Mapping[str, Any]]) -> str:
    """Render a previously built summary without doing any additional analysis."""

    lines: List[str] = []
    labels = (("image", "Image"), ("timeline", "Timeline"), ("anomalies", "Anomalies"))
    for key, label in labels:
        section = summary.get(key, {}) if isinstance(summary, Mapping) else {}
        lines.append("## %s" % label)
        if section.get("status") == "error":
            lines.append("Provider unavailable (%s)." % section.get("error", "unknown"))
            continue
        field = "fingerprint" if key == "image" else ("events" if key == "timeline" else "findings")
        value = section.get(field)
        if not value:
            lines.append("No data.")
        else:
            lines.append("```json")
            lines.append(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
            lines.append("```")
    return "\n".join(lines)


def _coalesce_provider(primary: Optional[Provider], alias: Optional[Provider], name: str) -> Optional[Provider]:
    if primary is not None and alias is not None:
        raise ValueError("provide only one %s provider" % name)
    return primary if primary is not None else alias


def _bounded_max_items(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = DEFAULT_MAX_ITEMS
    return max(1, min(result, MAX_MAX_ITEMS))


def _resolve(provider: Any, explicit: Any) -> Tuple[Any, Optional[str]]:
    source = provider if explicit is _MISSING else explicit
    if source is None or source is _MISSING:
        return None, None
    if not (explicit is _MISSING and callable(source)):
        return source, None
    try:
        return source(), None
    except Exception as exc:  # providers are isolated per section
        return None, _provider_error(exc)


def _provider_error(exc: Exception) -> str:
    # Do not copy exception text into the report: it may contain paths, tokens,
    # or snippets of data.  The exception class is enough for a safe diagnosis.
    return "provider failed (%s)" % type(exc).__name__


def _image_section(value: Any, error: Optional[str]) -> Dict[str, Any]:
    if error is not None:
        return {"status": "error", "fingerprint": {}, "error": error}
    if value is None or value == "":
        return {"status": "empty", "fingerprint": {}, "error": None}
    try:
        fingerprint = _safe_value(_object_to_value(value))
    except Exception as exc:  # defensive boundary for unusual provider values
        return {"status": "error", "fingerprint": {}, "error": _provider_error(exc)}
    if fingerprint is None or fingerprint == "":
        return {"status": "empty", "fingerprint": {}, "error": None}
    return {"status": "ok", "fingerprint": fingerprint, "error": None}


def _collection_section(
    field: str,
    value: Any,
    error: Optional[str],
    max_items: int,
) -> Dict[str, Any]:
    if error is not None:
        return {"status": "error", field: [], "count": 0, "truncated": False, "error": error}
    if value is None or value == "":
        return {"status": "empty", field: [], "count": 0, "truncated": False, "error": None}
    try:
        items, truncated, iteration_error = _bounded_items(value, max_items)
    except Exception as exc:  # defensive boundary for hostile iterables
        return {
            "status": "error",
            field: [],
            "count": 0,
            "truncated": False,
            "error": _provider_error(exc),
        }
    if iteration_error is not None:
        return {
            "status": "error",
            field: items,
            "count": len(items),
            "truncated": truncated,
            "error": iteration_error,
        }
    return {
        "status": "ok" if items else "empty",
        field: items,
        "count": len(items),
        "truncated": truncated,
        "error": None,
    }


def _bounded_items(value: Any, max_items: int) -> Tuple[List[Any], bool, Optional[str]]:
    if isinstance(value, Mapping) or isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_safe_value(_object_to_value(value))], False, None
    if isinstance(value, (set, frozenset)):
        ordered = sorted(
            value,
            key=lambda item: _canonical(_safe_value(_object_to_value(item))),
        )
        return (
            [_safe_value(_object_to_value(item)) for item in ordered[:max_items]],
            len(ordered) > max_items,
            None,
        )

    try:
        iterator = iter(value)
    except TypeError:
        return [_safe_value(_object_to_value(value))], False, None

    items: List[Any] = []
    for _ in range(max_items):
        try:
            item = next(iterator)
        except StopIteration:
            return items, False, None
        except Exception as exc:
            return items, False, _provider_error(exc)
        items.append(_safe_value(_object_to_value(item)))

    # One bounded look-ahead lets callers distinguish a capped collection from
    # a complete one without consuming an unbounded provider.
    try:
        next(iterator)
    except StopIteration:
        return items, False, None
    except Exception as exc:
        return items, True, _provider_error(exc)
    return items, True, None


def _object_to_value(value: Any) -> Any:
    """Convert common record objects without touching paths or file contents."""

    if isinstance(value, Mapping):
        return value
    for method_name in ("to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return method()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _safe_key(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<redacted bytes>"
    return _safe_text(str(value))


def _safe_text(value: str) -> str:
    text = str(value)
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[:MAX_TEXT_LENGTH] + "…"


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Return bounded JSON-like data, redacting every bytes-like value."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<redacted bytes>"
    if value is None or isinstance(value, (bool, int, str)):
        return _safe_text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if depth >= MAX_NESTED_DEPTH:
        return "<nested value truncated>"

    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        keys = sorted(value.keys(), key=_safe_key)
        for key in keys[:MAX_NESTED_FIELDS]:
            result[_safe_key(key)] = _safe_value(value[key], depth + 1)
        if len(keys) > MAX_NESTED_FIELDS:
            result["<truncated>"] = "%d more fields" % (len(keys) - MAX_NESTED_FIELDS)
        return result

    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value[:MAX_NESTED_FIELDS]]
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=lambda item: _canonical(_safe_value(item)))
        return [_safe_value(item, depth + 1) for item in ordered[:MAX_NESTED_FIELDS]]

    # Avoid arbitrary repr output: it can contain paths, bytes, secrets, or a
    # process-specific memory address, none of which belongs in a summary.
    return "<%s>" % type(value).__name__


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _submit_background(
    function: Callable[..., Dict[str, Dict[str, Any]]],
    args: Tuple[Any, ...],
    *,
    executor: Optional[Executor],
) -> Future:
    if executor is not None:
        return executor.submit(function, *args)

    # One worker is intentional: this is a bounded convenience entry point,
    # not an analysis scheduler.  No executor is created during module import.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mountfile-copilot")
    future = pool.submit(function, *args)

    def close_pool(_finished: Future) -> None:
        pool.shutdown(wait=False)

    future.add_done_callback(close_pool)
    return future


__all__ = [
    "CopilotSummaryBuilder",
    "DEFAULT_MAX_ITEMS",
    "ForensicCopilot",
    "MAX_MAX_ITEMS",
    "SECTION_NAMES",
    "SummaryBuilder",
    "build_forensic_summary",
    "build_forensic_summary_background",
    "build_summary",
    "build_summary_background",
    "summary_to_markdown",
]
