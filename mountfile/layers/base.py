# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Self-learning layer primitives.

Все обучаемые слои Mount File строятся на этих примитивах:

- :class:`KnowledgeStore` — персистентное JSON-хранилище знаний
  (``.mountfile/layers.json``) с версионированием схемы;
- :class:`BetaLearner` — онлайн-оценка вероятности успеха (Beta-Bernoulli);
- :class:`LogisticWeights` — онлайн-линейный ранжир (SGD по логистической
  функции) поверх детерминированных признаков;
- :class:`EWMA` — экспоненциально взвешенное среднее для адаптивных TTL;
- :class:`Layer` — базовый класс слоя: предсказание с фолбэком на
  оригинальную логику и обучение по явному фидбеку.

Ни один слой не меняет поведение оригинала по умолчанию: пока знаний нет,
возвращается исходный результат без изменений.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

STORE_VERSION = 1
DEFAULT_STORE = ".mountfile/layers.json"


class KnowledgeStore:
    """Персистентное JSON-хранилище знаний слоёв.

    Атомарная запись (tmp + rename), защита от параллельных процессов
    threading.Lock, версионирование схемы. Повреждённый файл молча
    сбрасывается — слой просто возвращается к фолбэку.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else Path.cwd()
        self.path = self.root / DEFAULT_STORE
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data: dict[str, Any] = {"version": STORE_VERSION, "layers": {}}
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and raw.get("version") == STORE_VERSION:
                        data = raw
            except (OSError, ValueError):
                pass
            self._cache = data
            return data

    def layer(self, name: str) -> dict[str, Any]:
        data = self.load()
        layers = data.setdefault("layers", {})
        return layers.setdefault(name, {})

    def save(self) -> None:
        with self._lock:
            if self._cache is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)


class BetaLearner:
    """Beta-Bernoulli: вероятность успеха события по явным фидбекам.

    ``beta(a, b)`` возвращает среднее апостериорного распределения.
    При ``a + b <= prior`` знаний практически нет — слой фолбэчится.
    """

    __slots__ = ("a", "b")

    def __init__(self, a: float = 1.0, b: float = 1.0) -> None:
        self.a = max(float(a), 0.0)
        self.b = max(float(b), 0.0)

    def to_dict(self) -> dict[str, float]:
        return {"a": round(self.a, 4), "b": round(self.b, 4)}

    @classmethod
    def from_dict(cls, raw: Any) -> "BetaLearner":
        if not isinstance(raw, dict):
            return cls()
        return cls(_num(raw.get("a"), 1.0), _num(raw.get("b"), 1.0))

    def update(self, success: bool, weight: float = 1.0) -> None:
        if success:
            self.a += weight
        else:
            self.b += weight

    @property
    def observations(self) -> float:
        """Число фидбеков сверх приор-а."""
        return max(self.a + self.b - 2.0, 0.0)

    def beta(self) -> float:
        total = self.a + self.b
        return self.a / total if total > 0 else 0.5


class LogisticWeights:
    """Онлайн-линейный ранжир поверх детерминированных признаков.

    Дефолтные веса нулевые: пока модель не обучена, ``score`` для всех
    объектов одинаков (0.0) и слой возвращает оригинальный порядок.
    """

    def __init__(self, dim: int, weights: list[float] | None = None) -> None:
        self.dim = int(dim)
        self.weights = list(weights) if weights is not None else [0.0] * self.dim

    def to_dict(self) -> dict[str, Any]:
        return {"dim": self.dim, "w": [round(w, 5) for w in self.weights]}

    @classmethod
    def from_dict(cls, raw: Any) -> "LogisticWeights":
        if not isinstance(raw, dict):
            return cls(1)
        dim = int(_num(raw.get("dim"), 1))
        w = [_num(x, 0.0) for x in raw.get("w", [])][:dim]
        return cls(dim, w + [0.0] * (dim - len(w)))

    def score(self, features: list[float]) -> float:
        total = 0.0
        for w, x in zip(self.weights, features):
            total += w * x
        return total

    def update(self, features: list[float], label: float, lr: float = 0.05) -> float:
        """SGD-шаг по логистической функции. Возвращает loss до шага."""
        pred = _sigmoid(self.score(features))
        loss = -(label * math.log(pred + 1e-9) + (1 - label) * math.log(1 - pred + 1e-9))
        err = label - pred
        for i, x in enumerate(features):
            self.weights[i] += lr * err * x
        return loss


class EWMA:
    """Экспоненциально взвешенное среднее (адаптивные TTL, метрики скорости)."""

    __slots__ = ("value", "alpha", "n")

    def __init__(self, alpha: float = 0.2, value: float | None = None) -> None:
        self.alpha = min(max(float(alpha), 0.01), 1.0)
        self.value = value
        self.n = 0

    def to_dict(self) -> dict[str, Any]:
        out = {"alpha": self.alpha, "n": self.n}
        if self.value is not None:
            out["value"] = round(self.value, 6)
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "EWMA":
        if not isinstance(raw, dict):
            return cls()
        value = raw.get("value")
        ewma = cls(_num(raw.get("alpha"), 0.2), None if value is None else _num(value, 0.0))
        ewma.n = int(_num(raw.get("n"), 0))
        return ewma

    def update(self, x: float) -> float:
        self.value = x if self.value is None else (1 - self.alpha) * self.value + self.alpha * x
        self.n += 1
        return self.value


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class Layer:
    """Базовый самообучающийся слой.

    Состояние хранится в ``store.layer(name)``. Подклассы реализуют
    ``_predict`` (поверх оригинальной логики) и ``_feedback`` (обучение).
    """

    name = "layer"

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.state: dict[str, Any] = store.layer(self.name)

    @property
    def observations(self) -> int:
        return int(_num(self.state.get("observations"), 0))

    def _touch(self) -> None:
        self.state["observations"] = self.observations + 1
        self.state["updated_at"] = time.time()
        self.store.save()

    @staticmethod
    def with_fallback(
        learned: Callable[[], Any] | None, original: Callable[[], Any]
    ) -> Any:
        """Попытаться применить знания; при любой ошибке — оригинальная логика."""
        if learned is not None and learned() is not None:
            try:
                result = learned()
                if result is not None:
                    return result
            except Exception:
                pass
        return original()
