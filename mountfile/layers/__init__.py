# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Self-learning AI layers for Mount File.

Каждый слой оборачивает ровно один существующий модуль: наблюдает входы,
предсказывает онлайн-алгоритмом, при отсутствии знаний фолбэчится на
оригинальную детерминированную логику и улучшается по явному фидбеку.

Слои
----
- :class:`mountfile.layers.volume_layer.VolumeLayer` — ранжирование
  кандидатов томов поверх :mod:`mountfile.autopick`;
- :class:`mountfile.layers.preflight_layer.PreflightLayer` — вероятность
  прохождения preflight по семействам ФС поверх
  :mod:`mountfile.fs_preflight`;
- :class:`mountfile.layers.cache_layer.CacheLayer` — адаптивные TTL
  поверх :class:`mountfile.turbo.CachedFS`;
- :class:`mountfile.layers.advisor_layer.AdvisorLayer` — калибровка
  сигнатура→формат поверх :mod:`mountfile.ai_format_advisor_v2`.

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

from .base import BetaLearner, EWMA, KnowledgeStore, Layer, LogisticWeights
from .pipeline import SelfLearningStack

__all__ = [
    "BetaLearner",
    "EWMA",
    "KnowledgeStore",
    "Layer",
    "LogisticWeights",
    "SelfLearningStack",
]
