# SPDX-License-Identifier: MIT
"""Opt-in YARA adapter example.

Importing this module never imports ``yara-python`` and never registers a
plugin.  Call :func:`create_plugin` and explicitly register the returned object,
or call :func:`register` from an explicit discovery operation.
"""

from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Any, Optional, Tuple

from mountfile.plugins import FileEvent, OpenEvent, PluginStatus


class YaraPlugin:
    """Scan immutable file-content snapshots with optional ``yara-python``."""

    name = "yara"

    def __init__(
        self,
        rules: Optional[Any] = None,
        rule_path: Optional[str] = None,
        *,
        yara_module: Optional[Any] = None,
    ) -> None:
        if rules is not None and rule_path is not None:
            raise ValueError("provide rules or rule_path, not both")
        self._rules_source = rules
        self._rule_path = rule_path
        self._yara = yara_module
        self._compiled = None
        self._matches = []
        self.reason = ""
        self.status = "unconfigured"
        if self._yara is None:
            try:
                self._yara = importlib.import_module("yara")
            except Exception as exc:
                self._set_status(
                    "unavailable",
                    "optional dependency yara-python is unavailable: %s" % exc,
                )
        if self._yara is not None and (rules is not None or rule_path is not None):
            self._set_status("available", "yara-python loaded; rules compile on open")
        elif self._yara is not None:
            self._set_status(
                "unconfigured", "provide rules or rule_path to enable scanning"
            )

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def status_info(self) -> PluginStatus:
        return PluginStatus(self.name, self.status, self.reason)

    @property
    def matches(self) -> Tuple[MappingProxyType, ...]:
        return tuple(MappingProxyType(dict(item)) for item in self._matches)

    def on_open(self, event: OpenEvent) -> None:
        event = OpenEvent.from_value(event)
        self._matches = []
        if self.status in ("unavailable", "unconfigured") or self._compiled is not None:
            return
        try:
            if self._rule_path is not None:
                self._compiled = self._yara.compile(filepath=self._rule_path)
            else:
                source = self._rules_source
                if isinstance(source, bytes):
                    source = source.decode("utf-8")
                self._compiled = self._yara.compile(source=source)
        except Exception as exc:
            self._compiled = None
            self._set_status(
                "unavailable", "YARA rules could not be compiled: %s" % exc
            )

    def on_file(self, event: FileEvent, data: Optional[bytes] = None) -> None:
        if self._compiled is None:
            return
        if data is not None:
            event = {"path": event, "data": data}
        event = FileEvent.from_value(event)
        if event.data is None:
            return
        try:
            found = self._compiled.match(data=event.data)
            for match in found or ():
                rule = getattr(match, "rule", str(match))
                tags = tuple(getattr(match, "tags", ()) or ())
                self._matches.append(
                    {"path": event.path, "rule": str(rule), "tags": tags}
                )
        except Exception as exc:
            self._set_status("unavailable", "YARA scan failed: %s" % exc)

    def on_close(self) -> None:
        return None

    def _set_status(self, status: str, reason: str) -> None:
        self.status = status
        self.reason = reason


def create_plugin(**kwargs: Any) -> YaraPlugin:
    """Construct a YARA plugin without registering it."""

    return YaraPlugin(**kwargs)


def register(registry: Any, **kwargs: Any) -> YaraPlugin:
    """Explicit discovery hook; no registration occurs merely on import."""

    plugin = create_plugin(**kwargs)
    registry.register(plugin)
    return plugin


YaraAdapter = YaraPlugin
create_yara_plugin = create_plugin
PLUGINS = (YaraPlugin,)

__all__ = [
    "PLUGINS",
    "YaraAdapter",
    "YaraPlugin",
    "create_plugin",
    "create_yara_plugin",
    "register",
]
