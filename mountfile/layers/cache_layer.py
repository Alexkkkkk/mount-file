# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Обучаемый слой кэша поверх :class:`mountfile.turbo.CachedFS`.

Слой следит за частотой промахов (EWMA) и адаптирует TTL листинга: чем
чаще промахи, тем дольше держится кэш (в пределах базового и максимального
TTL). Пока метрики нет, используется оригинальный TTL обёртки.

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

from typing import Any

from .base import EWMA, KnowledgeStore

MIN_TTL_FACTOR = 0.5
MAX_TTL_FACTOR = 4.0
# Дефолт CachedFS.__init__ (mountfile.turbo) — отдельной константы нет.
DEFAULT_LISTING_TTL = 20.0


class CacheLayer:
    """Самообучающийся адаптивный TTL для CachedFS."""

    name = "cache"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state = store.layer(self.name)
        raw = self.state.get("miss_rate")
        self.miss_rate = EWMA.from_dict(raw) if isinstance(raw, dict) else EWMA(0.2)
        self._pending = 0

    # ------------------------------------------------------------- метрика
    def observe_hit(self, hit: bool, *, persist: bool = True) -> None:
        """Зарегистрировать попадание/промах кэша (явный фидбек)."""
        self.miss_rate.update(0.0 if hit else 1.0)
        self.state["miss_rate"] = self.miss_rate.to_dict()
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self._pending += 1
        # The hot path batches writes; direct/user feedback remains durable.
        if persist or self.miss_rate.n % 16 == 0:
            self.flush()

    def flush(self) -> None:
        """Persist pending cache metrics on a batch boundary or clean shutdown."""
        if self._pending:
            self.store.save()
            self._pending = 0

    @property
    def ttl_factor(self) -> float | None:
        """Множитель TTL по частоте промахов или None (нет знаний)."""
        if self.miss_rate.value is None or self.miss_rate.n < 5:
            return None
        # промахи 0..1 → фактор 0.5..4.0: больше промахов — дольше кэш
        factor = MIN_TTL_FACTOR + self.miss_rate.value * (
            MAX_TTL_FACTOR - MIN_TTL_FACTOR
        )
        return round(min(max(factor, MIN_TTL_FACTOR), MAX_TTL_FACTOR), 3)

    # -------------------------------------------------------------- обёртка
    def wrap(self, fs: Any, listing_ttl: float | None = None) -> Any:
        """CachedFS с обученным TTL; без знаний — оригинальный TTL."""
        from .. import turbo

        factor = self.ttl_factor
        base_ttl = DEFAULT_LISTING_TTL if listing_ttl is None else listing_ttl
        ttl = base_ttl if factor is None else round(base_ttl * factor, 3)
        learner = self

        class ObservedCachedFS(turbo.CachedFS):
            """CachedFS-compatible wrapper that observes counters, not content."""

            def list_dir(self, path: str) -> list[dict[str, Any]]:
                before = self._listing.stats()
                try:
                    return super().list_dir(path)
                finally:
                    after = self._listing.stats()
                    if after["hits"] > before["hits"]:
                        learner.observe_hit(True, persist=False)
                    elif after["misses"] > before["misses"]:
                        learner.observe_hit(False, persist=False)

        return ObservedCachedFS(fs, ttl)
