# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Обучаемый слой выбора тома поверх :mod:`mountfile.autopick`.

Слой вызывает оригинальный ``auto_select`` (скан сигнатур, валидация,
verify-монтирование) и переупорядочивает кандидатов обученным линейным
ранжиром. Признаки кандидата:

- нормированный оригинальный score;
- offset == 0 (том в начале образа);
- наличие метки/имени;
- наличие размера;
- успех verify-монтирования (backend is not None).

Пока модель не обучена (все веса 0), порядок совпадает с оригинальным.
Фидбек: пользователь принял предложенный том (label=1) или отклонил (label=0).

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

from typing import Any

from .base import KnowledgeStore, LogisticWeights

FEATURE_DIM = 5


class VolumeLayer:
    """Самообучающееся ранжирование кандидатов томов."""

    name = "volume"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state = store.layer(self.name)
        raw = self.state.get("ranker")
        if isinstance(raw, dict) and raw.get("dim") == FEATURE_DIM:
            self.ranker = LogisticWeights.from_dict(raw)
        else:
            self.ranker = LogisticWeights(FEATURE_DIM)

    @property
    def observations(self) -> int:
        """Число явных фидбеков по этому слою."""
        return int(self.state.get("observations", 0) or 0)

    # ---------------------------------------------------------------- фичи
    @staticmethod
    def features(cand: Any, max_score: float) -> list[float]:
        score_norm = (cand.score / max_score) if max_score else 0.0
        return [
            score_norm,
            1.0 if cand.offset == 0 else 0.0,
            1.0 if (cand.label or cand.name) else 0.0,
            1.0 if cand.size else 0.0,
            1.0 if cand.backend else 0.0,
        ]

    # ------------------------------------------------------------ предсказание
    def rerank(self, candidates: list[Any]) -> list[Any]:
        """Переупорядочить кандидатов знаниями; без знаний — как есть."""
        if not candidates:
            return candidates
        max_score = max((abs(c.score) for c in candidates), default=0.0)
        learned = sorted(
            candidates,
            key=lambda c: -self.ranker.score(self.features(c, max_score)),
        )
        if all(w == 0.0 for w in self.ranker.weights):
            return list(candidates)
        return learned

    def select(self, path: str, **kwargs: Any) -> tuple[Any, list[Any]]:
        """Оригинальный ``autopick.auto_select`` + обученное переупорядочивание.

        Возвращает ``(best, candidates)`` в том же контракте, что и оригинал.
        """
        from .. import autopick

        best, candidates = autopick.auto_select(path, **kwargs)
        ranked = self.rerank(candidates)
        for cand in ranked:
            if cand.fstype == "raw":
                continue
            if kwargs.get("verify", True) and not cand.backend:
                continue
            return cand, ranked
        return None, ranked

    # ---------------------------------------------------------------- фидбек
    def feedback(self, cand: Any, accepted: bool) -> None:
        """Явный фидбек: принят ли предложенный том пользователем/CLI."""
        max_score = max(abs(cand.score), 1.0)
        self.ranker.update(self.features(cand, max_score), 1.0 if accepted else 0.0)
        self.state["ranker"] = self.ranker.to_dict()
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self.store.save()
