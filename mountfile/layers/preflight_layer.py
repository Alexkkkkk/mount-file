# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Обучаемый слой preflight поверх :mod:`mountfile.fs_preflight`.

Оригинальный preflight остаётся единственным источником истины: слой ничего
не разрешает и не запрещает. Он только накапливает по семействам ФС
(FAT/exFAT/NTFS/ext) вероятность того, что preflight вернёт «можно открыть»,
и выдает её как подсказку приоритизации (``pass_hint``). Оценка —
Beta-Bernoulli по явным фидбекам реальных прогонов.

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

import os
from typing import Any

from .base import BetaLearner, KnowledgeStore


class PreflightLayer:
    """Самообучающаяся оценка проходимости preflight по семействам ФС."""

    name = "preflight"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state = store.layer(self.name)
        families = self.state.get("families")
        self.families: dict[str, BetaLearner] = {}
        if isinstance(families, dict):
            for key, raw in families.items():
                if isinstance(key, str) and key in {"fat", "exfat", "ntfs", "ext"}:
                    self.families[key] = BetaLearner.from_dict(raw)

    @property
    def observations(self) -> int:
        """Число явных фидбеков (реальных прогонов preflight)."""
        return int(self.state.get("observations", 0) or 0)

    # ----------------------------------------------------------- семейство
    @staticmethod
    def family_of(report: Any) -> str | None:
        """Семейство ФС из отчёта preflight (``_detect_family``-совместимо)."""
        family = getattr(report, "family", None)
        if isinstance(family, str) and family:
            return family
        detected = getattr(report, "detected_family", None)
        return detected if isinstance(detected, str) and detected else None

    # ----------------------------------------------------------- подсказка
    def pass_hint(self, family: str | None) -> float | None:
        """Вероятность прохождения preflight для семейства или None.

        None — знаний ещё нет (меньше 3 фидбеков): слой фолбэчится,
        подсказка не выдается.
        """
        if family not in self.families:
            return None
        learner = self.families[family]
        if learner.observations < 3:
            return None
        return round(learner.beta(), 3)

    # --------------------------------------------------------------- прогон
    def observe(self, report: Any, passed: bool) -> None:
        """Зарегистрировать реальный результат preflight (явный фидбек)."""
        family = self.family_of(report)
        if family not in {"fat", "exfat", "ntfs", "ext"}:
            return
        learner = self.families.setdefault(family, BetaLearner())
        learner.update(bool(passed))
        self.state["families"] = {
            key: value.to_dict() for key, value in sorted(self.families.items())
        }
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self.store.save()

    def preflight_image(self, image_path: str | os.PathLike[str], **kwargs: Any) -> Any:
        """Оригинальный ``preflight_image`` + обучение на его результате."""
        from .. import fs_preflight

        report = fs_preflight.preflight_image(image_path, **kwargs)
        passed = not getattr(report, "refusal_code", None) and not getattr(
            report, "refusals", None
        )
        self.observe(report, bool(passed))
        return report
