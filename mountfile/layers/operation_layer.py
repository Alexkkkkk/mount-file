# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Privacy-preserving aggregate telemetry for CLI operation outcomes."""

from __future__ import annotations

import math
import re
from typing import Any

from .base import EWMA, KnowledgeStore

_COMMAND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class OperationLayer:
    """Learn aggregate success and duration statistics per command name.

    This layer intentionally stores no argv, paths, prompts, file content,
    image identity, model response, or exception text. It is opt-in through
    :class:`LayerManager` and never influences command execution.
    """

    name = "operations"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state = store.layer(self.name)

    @property
    def observations(self) -> int:
        try:
            return max(0, int(self.state.get("observations", 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def record(self, command: str, exit_code: int, duration_seconds: float) -> None:
        """Record an aggregate outcome; callers gate this on layer enablement."""
        if not isinstance(command, str) or not _COMMAND.fullmatch(command):
            return
        try:
            code = int(exit_code)
            duration = float(duration_seconds)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(duration) or duration < 0:
            return

        commands = self.state.setdefault("commands", {})
        current = commands.setdefault(command, {"successes": 0, "failures": 0})
        outcome = "successes" if code == 0 else "failures"
        current[outcome] = max(0, int(current.get(outcome, 0))) + 1
        durations = self.state.setdefault("duration_ewma", {})
        ewma = EWMA.from_dict(durations.get(command, {}))
        ewma.update(min(duration, 24 * 60 * 60))
        durations[command] = ewma.to_dict()
        self.state["observations"] = self.observations + 1
        self.store.save()

    def summary(self) -> dict[str, Any]:
        """Return aggregate stats detached from mutable storage."""
        return {
            key: dict(value)
            for key, value in sorted(self.state.get("commands", {}).items())
            if isinstance(value, dict)
        }
