# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Central management API for Mount File's advisory learning layers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import KnowledgeStore
from .registry import LAYER_NAMES, discover

# General operation telemetry is opt-in. Existing advisory layers retain their
# legacy default so an unavailable or empty store never changes core behavior.
DEFAULT_ENABLED = {"operations": False, "code_repair": True}


class LayerManager:
    """Inspect and control persisted layer state without changing evidence."""

    def __init__(
        self, root: str | os.PathLike[str] | KnowledgeStore | None = None
    ) -> None:
        if isinstance(root, KnowledgeStore):
            self.store = root
            return
        configured = root or os.environ.get("MOUNTFILE_LAYERS_DIR") or Path.home()
        self.store = KnowledgeStore(configured)

    @staticmethod
    def _validate_name(name: str) -> None:
        if name not in LAYER_NAMES:
            raise ValueError(f"unknown layer: {name}")

    def enabled(self, name: str) -> bool:
        """Return whether a layer is active; unknown settings use safe defaults."""
        self._validate_name(name)
        value = self.store.layer(name).get("enabled")
        if isinstance(value, bool):
            return value
        return DEFAULT_ENABLED.get(name, True)

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Persist an explicit enable/disable decision for one layer."""
        self._validate_name(name)
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        self.store.layer(name)["enabled"] = bool(enabled)
        self.store.save()

    def reset(self, name: str) -> None:
        """Clear learned statistics while preserving the explicit enable flag."""
        self._validate_name(name)
        layers = self.store.load().setdefault("layers", {})
        was_enabled = self.enabled(name)
        layers[name] = {"enabled": was_enabled}
        control = layers.get("__control__")
        if isinstance(control, dict):
            pending = control.get("pending_decisions")
            if isinstance(pending, dict):
                control["pending_decisions"] = {
                    key: value
                    for key, value in pending.items()
                    if not isinstance(value, dict) or value.get("layer") != name
                }
        self.store.save()

    def status(self) -> dict[str, Any]:
        """Return management state, aggregate counts, and live CLI routing."""
        layers = {}
        for name in LAYER_NAMES:
            state = self.store.layer(name)
            try:
                observations = max(0, int(state.get("observations", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                observations = 0
            layers[name] = {
                "enabled": self.enabled(name),
                "observations": observations,
            }
        return {
            "version": 1,
            "store": str(self.store.path),
            "layers": layers,
            "cli": discover(),
        }
