# SPDX-License-Identifier: MIT
"""Opt-in timeline adapter example.

The adapter uses pandas when available for export, while retaining a small
stdlib-only event collector.  Set ``require_pandas=True`` when a deployment
must report an unavailable status instead of using the fallback collector.
"""

from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from mountfile.plugins import FileEvent, OpenEvent, PluginStatus


class TimelinePlugin:
    """Collect immutable file events for a timeline or optional DataFrame."""

    name = "timeline"

    def __init__(self, *, require_pandas: bool = False, pandas_module: Optional[Any] = None) -> None:
        self._pandas = pandas_module
        self._require_pandas = require_pandas
        self._events = []
        self._image_path = None
        self.reason = ""
        self.status = "available"
        if self._pandas is None:
            try:
                self._pandas = importlib.import_module("pandas")
            except Exception as exc:
                if require_pandas:
                    self._set_status("unavailable", "optional dependency pandas is unavailable: %s" % exc)
                else:
                    self._set_status("degraded", "pandas is unavailable; using stdlib event records: %s" % exc)

    @property
    def available(self) -> bool:
        return self.status in ("available", "degraded")

    @property
    def status_info(self) -> PluginStatus:
        return PluginStatus(self.name, self.status, self.reason)

    @property
    def events(self) -> Tuple[MappingProxyType, ...]:
        return tuple(MappingProxyType(dict(item)) for item in self._events)

    def on_open(self, event: OpenEvent) -> None:
        event = OpenEvent.from_value(event)
        self._events = []
        self._image_path = event.path

    def on_file(self, event: FileEvent, data: Optional[bytes] = None) -> None:
        if not self.available:
            return
        if data is not None:
            event = {"path": event, "data": data}
        event = FileEvent.from_value(event)
        record: Dict[str, Any] = {
            "path": event.path,
            "size": event.size,
            "offset": event.offset,
            "image": self._image_path,
        }
        record.update(dict(event.metadata))
        self._events.append(record)

    def on_close(self) -> None:
        return None

    def to_dataframe(self) -> Any:
        """Return a pandas DataFrame, or ``None`` when pandas is unavailable."""

        if self._pandas is None:
            return None
        return self._pandas.DataFrame([dict(item) for item in self._events])

    def _set_status(self, status: str, reason: str) -> None:
        self.status = status
        self.reason = reason


def create_plugin(**kwargs: Any) -> TimelinePlugin:
    """Construct a timeline plugin without registering it."""

    return TimelinePlugin(**kwargs)


def register(registry: Any, **kwargs: Any) -> TimelinePlugin:
    """Explicit discovery hook; no registration occurs merely on import."""

    plugin = create_plugin(**kwargs)
    registry.register(plugin)
    return plugin


TimelineAdapter = TimelinePlugin
create_timeline_plugin = create_plugin
