# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Обучаемый слой AI-советника поверх :mod:`mountfile.ai_format_advisor_v2`.

Оригинальный advisor v2 (гипотезы по сигнатурам + LLM-бэкенд) остаётся
без изменений. Слой калибрует его гипотезы по накопленному фидбеку:
для пары «гипотеза → формат подтверждён» ведётся Beta-Bernoulli, и
уверенность гипотезы масштабируется выученной вероятностью. Пока знаний
меньше 3 фидбеков на формат, уверенности возвращаются как есть.

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

from typing import Any

from .base import BetaLearner, KnowledgeStore

MIN_FEEDBACK = 3


class AdvisorLayer:
    """Самообучающаяся калибровка гипотез о формате файла."""

    name = "advisor"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state = store.layer(self.name)
        formats = self.state.get("formats")
        self.formats: dict[str, BetaLearner] = {}
        if isinstance(formats, dict):
            for key, raw in formats.items():
                if isinstance(key, str):
                    self.formats[key] = BetaLearner.from_dict(raw)

    # --------------------------------------------------------------- фидбек
    def feedback(self, fmt: str, correct: bool) -> None:
        """Явный фидбек: подтвердилась ли гипотеза о формате."""
        key = str(fmt).strip().lower()
        if not key:
            return
        learner = self.formats.setdefault(key, BetaLearner())
        learner.update(bool(correct))
        self.state["formats"] = {
            k: v.to_dict() for k, v in sorted(self.formats.items())
        }
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self.store.save()

    # ------------------------------------------------------------ калибровка
    def _reliability(self, fmt: str) -> float | None:
        learner = self.formats.get(str(fmt).strip().lower())
        if learner is None or learner.observations < MIN_FEEDBACK:
            return None
        return learner.beta()

    def calibrate(self, hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Перекалибровать список гипотез advisor v2.

        Гипотезы — словари вида ``{"format": str, "confidence": float}``
        (совместимо с выводом :class:`HypothesisEngine`). Возвращает новый
        список, отсортированный по скорректированной уверенности, либо
        None, если знаний пока нет (фолбэк на исходный порядок).
        """
        if not hypotheses:
            return None
        adjusted = []
        learned_any = False
        for hyp in hypotheses:
            out = dict(hyp)
            # Оба формата гипотез: advisor v2 (name/score) и LLM-слой
            # (format/confidence).
            fmt = str(hyp.get("format") or hyp.get("name") or "")
            reliability = self._reliability(fmt)
            if reliability is not None:
                learned_any = True
                conf = float(hyp.get("confidence", hyp.get("score", 0.0)) or 0.0)
                value = round(conf * reliability, 4)
                if "confidence" in hyp:
                    out["confidence"] = value
                else:
                    out["score"] = value
                out["learned_reliability"] = round(reliability, 3)
            adjusted.append(out)
        if not learned_any:
            return None
        adjusted.sort(
            key=lambda h: -float(h.get("confidence", h.get("score", 0.0)) or 0.0)
        )
        return adjusted

    def best_format(self, hypotheses: list[dict[str, Any]]) -> str | None:
        """Формат с максимальной калиброванной уверенностью или None."""
        calibrated = self.calibrate(hypotheses)
        if not calibrated:
            return None
        top = calibrated[0]
        return str(top.get("format") or top.get("name") or "") or None
