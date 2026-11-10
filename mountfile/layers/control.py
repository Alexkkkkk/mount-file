# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Typed advisory decision and explicit-feedback lifecycle for learning layers.

Decisions are suggestions only: they cannot choose a provider, open a mount,
pass preflight, authorize writes, or change evidence. Pending state deliberately
excludes paths, candidate names, prompts, bytes, and arbitrary ``extras``.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .manager import LayerManager
from .pipeline import SelfLearningStack
from .volume_layer import VolumeLayer

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LayerContext:
    """Runtime-only, immutable context; path and extras are never persisted."""

    path: str = ""
    fs_type: str | None = None
    size_bytes: int | None = None
    candidates: Sequence[Any] = ()
    volume_hint: str | None = None
    operation: str = "inspect"
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayerDecision:
    """A display/ranking hint with provenance and a feedback correlation ID."""

    layer: str
    action: str
    value: Any
    confidence: float = 0.0
    rationale: str = ""
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    advisory_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Feedback:
    """An explicit, one-time user label for a prior advisory decision."""

    decision_id: str
    success: bool
    latency_ms: float = 0.0
    note: str = ""  # deliberately not persisted or consumed as policy


class Hooks:
    """Small event bus; hook failures are isolated from domain operations."""

    def __init__(self) -> None:
        self._before: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._after: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def before(self, event: str, callback: Callable[..., Any]) -> Callable[..., Any]:
        self._before[event].append(callback)
        return callback

    def after(self, event: str, callback: Callable[..., Any]) -> Callable[..., Any]:
        self._after[event].append(callback)
        return callback

    @staticmethod
    def _emit(callbacks: list[Callable[..., Any]], *args: Any) -> None:
        for callback in tuple(callbacks):
            try:
                callback(*args)
            except Exception:  # noqa: BLE001 - extension hooks are non-authoritative
                _LOG.debug("advisory layer hook failed", exc_info=True)
                continue

    def emit_before(self, event: str, *args: Any) -> None:
        self._emit(self._before[event], *args)

    def emit_after(self, event: str, *args: Any) -> None:
        self._emit(self._after[event], *args)


class DecisionOrchestrator:
    """Coordinate current package layers and correlate explicit feedback.

    ``autopick`` only reranks the caller-supplied deterministic candidates;
    preflight and cache outputs are historical hints. No decision is applied.
    """

    PENDING_KEY = "pending_decisions"
    MAX_PENDING = 512
    OPERATIONS = {"inspect", "autopick", "preflight", "browse", "advise"}

    def __init__(self, root: str | None = None, hooks: Hooks | None = None) -> None:
        self.manager = LayerManager(root)
        self.stack = SelfLearningStack(self.manager.store)
        self.store = self.stack.store
        self.hooks = hooks or Hooks()

    @staticmethod
    def _family(context: LayerContext) -> str | None:
        from .preflight_layer import PreflightLayer

        return PreflightLayer.normalize_family(context.fs_type)

    @staticmethod
    def _candidate_features(candidate: Any, candidates: list[Any]) -> list[float]:
        scores = []
        for item in candidates:
            try:
                score = float(getattr(item, "score", 0.0))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(score):
                scores.append(abs(score))
        max_score = max(scores, default=0.0)
        return [float(value) for value in VolumeLayer.features(candidate, max_score)]

    @staticmethod
    def _candidate_summary(candidate: Any) -> dict[str, Any]:
        # Never include name/path/label or image-derived bytes in persisted state.
        result: dict[str, Any] = {}
        for key in ("fstype", "offset", "size", "score"):
            value = getattr(candidate, key, None)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                result[key] = value
        return result

    def decide(self, context: LayerContext) -> list[LayerDecision]:
        if (
            not isinstance(context.operation, str)
            or context.operation not in self.OPERATIONS
        ):
            raise ValueError("unsupported advisory operation")
        self.hooks.emit_before("decide", context)
        decisions: list[LayerDecision] = []
        control = self.store.layer("__control__")
        pending = control.get(self.PENDING_KEY)
        if not isinstance(pending, dict):
            pending = {}
            control[self.PENDING_KEY] = pending

        if (
            context.operation == "autopick"
            and context.candidates
            and self.manager.enabled("volume")
        ):
            candidates = list(context.candidates)
            ranked = self.stack.volume.rerank(candidates)
            candidate = ranked[0] if ranked else None
            if candidate is not None:
                features = self._candidate_features(candidate, candidates)
                decision = LayerDecision(
                    layer="volume",
                    action="rank_order_hint",
                    value=self._candidate_summary(candidate),
                    rationale="Advisory ranking only; production verification remains authoritative.",
                )
                pending[decision.decision_id] = {
                    "layer": "volume",
                    "features": features,
                }
                decisions.append(decision)

        if context.operation == "preflight" and self.manager.enabled("preflight"):
            family = self._family(context)
            hint = self.stack.preflight.pass_hint(family)
            if hint is not None:
                decision = LayerDecision(
                    layer="preflight",
                    action="historical_pass_hint",
                    value=hint,
                    confidence=0.0,
                    rationale="Historical telemetry only; does not change the current preflight result.",
                )
                pending[decision.decision_id] = {
                    "layer": "preflight",
                    "family": family,
                }
                decisions.append(decision)

        if context.operation == "browse" and self.manager.enabled("cache"):
            factor = self.stack.cache.ttl_factor
            if factor is not None:
                decision = LayerDecision(
                    layer="cache",
                    action="ttl_hint",
                    value=factor,
                    confidence=0.0,
                    rationale="Bounded performance hint only; cache validity checks are unchanged.",
                )
                pending[decision.decision_id] = {"layer": "cache"}
                decisions.append(decision)

        # The context is intentionally not serialized: no path, candidate
        # label, prompt, file content, or arbitrary extras enter the store.
        if len(pending) > self.MAX_PENDING:
            for decision_id in list(pending)[: len(pending) - self.MAX_PENDING]:
                pending.pop(decision_id, None)
        try:
            self.store.save()
        except OSError:
            _LOG.debug("could not persist advisory decision", exc_info=True)
        self.hooks.emit_after("decide", context, tuple(decisions))
        return decisions

    def feedback(self, feedback: Feedback) -> bool:
        if (
            not isinstance(feedback, Feedback)
            or not isinstance(feedback.decision_id, str)
            or not isinstance(feedback.success, bool)
        ):
            return False
        try:
            latency_ms = float(feedback.latency_ms)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(latency_ms) or latency_ms < 0:
            return False
        control = self.store.layer("__control__")
        pending = control.get(self.PENDING_KEY)
        if not isinstance(pending, dict):
            return False
        if len(feedback.decision_id) != 32 or any(
            char not in "0123456789abcdef" for char in feedback.decision_id.lower()
        ):
            return False
        raw = pending.pop(feedback.decision_id, None)
        if not isinstance(raw, dict):
            return False

        layer = raw.get("layer")
        if layer == "volume" and self.manager.enabled("volume"):
            features = raw.get("features")
            if (
                isinstance(features, list)
                and len(features) == len(self.stack.volume.ranker.weights)
                and all(
                    isinstance(value, (int, float)) and math.isfinite(value)
                    for value in features
                )
            ):
                self.stack.volume.ranker.update(
                    [float(value) for value in features],
                    1.0 if feedback.success else 0.0,
                )
                self.stack.volume.state["ranker"] = self.stack.volume.ranker.to_dict()
                self.stack.volume.state["observations"] = (
                    self.stack.volume.observations + 1
                )
        elif layer == "preflight" and self.manager.enabled("preflight"):
            family = raw.get("family")
            if isinstance(family, str):
                self.stack.preflight.observe_dict(
                    {
                        "filesystem": family,
                        "eligible_for_read": feedback.success,
                    }
                )
        elif layer == "cache" and self.manager.enabled("cache"):
            self.stack.cache.observe_hit(feedback.success, persist=False)
            self.stack.cache.flush()

        try:
            feedback_observations = max(
                0, int(control.get("feedback_observations", 0) or 0)
            )
        except (TypeError, ValueError, OverflowError):
            feedback_observations = 0
        control["feedback_observations"] = feedback_observations + 1
        try:
            self.store.save()
        except OSError:
            _LOG.debug("could not persist advisory feedback", exc_info=True)
            return False
        self.hooks.emit_after("feedback", feedback, layer)
        return True

    def flush(self) -> None:
        self.stack.cache.flush()
        self.store.save()
