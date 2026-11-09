# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Стек самообучающихся слоёв Mount File.

Один :class:`KnowledgeStore` на все слои, один файл знаний
``.mountfile/layers.json``. Использование::

    from mountfile.layers import KnowledgeStore, SelfLearningStack

    stack = SelfLearningStack(KnowledgeStore("/path/to/project"))
    best, candidates = stack.volume.select(image_path)
    stack.volume.feedback(best, accepted=True)

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

from .advisor_layer import AdvisorLayer
from .base import KnowledgeStore
from .cache_layer import CacheLayer
from .operation_layer import OperationLayer
from .preflight_layer import PreflightLayer
from .volume_layer import VolumeLayer


class SelfLearningStack:
    """Все обучаемые слои над одним хранилищем знаний."""

    def __init__(self, root: str | KnowledgeStore | None = None) -> None:
        self.store = root if isinstance(root, KnowledgeStore) else KnowledgeStore(root)
        self.volume = VolumeLayer(self.store)
        self.preflight = PreflightLayer(self.store)
        self.cache = CacheLayer(self.store)
        self.advisor = AdvisorLayer(self.store)
        self.operations = OperationLayer(self.store)

    def is_enabled(self, name: str) -> bool:
        """Return the centrally configured state of one layer."""
        from .manager import LayerManager

        return LayerManager(self.store.root).enabled(name)

    def summary(self) -> dict[str, int]:
        """Наблюдений по слоям — для GUI/CLI и issue-отчётов."""
        return {
            "volume": self.volume.observations,
            "preflight": self.preflight.observations,
            "cache": self.cache.miss_rate.n,
            "advisor": self.advisor.state.get("observations", 0),
            "operations": self.operations.observations,
        }
