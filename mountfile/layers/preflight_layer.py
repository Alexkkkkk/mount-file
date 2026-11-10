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
        family = (
            getattr(report, "family", None)
            or getattr(report, "detected_family", None)
            or getattr(report, "filesystem", None)
        )
        return PreflightLayer.normalize_family(family)

    @staticmethod
    def normalize_family(family: Any) -> str | None:
        """Map display names (NTFS, FAT32, ext4) to stable learner keys."""
        if not isinstance(family, str):
            return None
        value = family.strip().lower()
        if value.startswith("fat"):
            return "fat"
        if value.startswith("exfat"):
            return "exfat"
        if value.startswith("ntfs"):
            return "ntfs"
        if value.startswith("ext"):
            return "ext"
        return None

    # ----------------------------------------------------------- подсказка
    def pass_hint(self, family: str | None) -> float | None:
        """Вероятность прохождения preflight для семейства или None.

        None — знаний ещё нет (меньше 3 фидбеков): слой фолбэчится,
        подсказка не выдается.
        """
        family = self.normalize_family(family)
        if family is None or family not in self.families:
            return None
        learner = self.families[family]
        if learner.observations < 3:
            return None
        return round(learner.beta(), 3)

    # --------------------------------------------------------------- прогон
    def observe(self, report: Any, passed: bool) -> None:
        """Зарегистрировать реальный результат preflight (явный фидбек)."""
        family = self.family_of(report)
        family = self.normalize_family(family)
        if family is None:
            return
        learner = self.families.setdefault(family, BetaLearner())
        learner.update(bool(passed))
        self.state["families"] = {
            key: value.to_dict() for key, value in sorted(self.families.items())
        }
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self.store.save()

    def observe_dict(self, volume: dict[str, Any]) -> None:
        """Фидбек из JSON-payload CLI-команды ``preflight``.

        Считает прогон пройденным, если том помечен ``eligible_for_read``
        (fail-closed остаётся у оригинала). Незнакомое семейство игнорирует.
        """
        family = self.normalize_family(
            volume.get("family") or volume.get("filesystem") or ""
        )
        if family is None:
            return
        learner = self.families.setdefault(family, BetaLearner())
        learner.update(volume.get("eligible_for_read") is True)
        self.state["families"] = {
            key: value.to_dict() for key, value in sorted(self.families.items())
        }
        self.state["observations"] = int(self.state.get("observations", 0)) + 1
        self.store.save()

    def preflight_image(self, image_path: str | os.PathLike[str], **kwargs: Any) -> Any:
        """Оригинальный ``preflight_image`` + обучение на его результате."""
        from .. import fs_preflight

        payload = fs_preflight.preflight_image(image_path, **kwargs)
        if isinstance(payload, dict):
            for volume in payload.get("volumes", []):
                if isinstance(volume, dict):
                    self.observe_dict(volume)
        return payload
