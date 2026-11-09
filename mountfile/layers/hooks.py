# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Точки подключения самообучающихся слоёв в реальный код Mount File.

Используется в CLI (``--auto`` и команда ``autopick``) и в GUI
(автоподбор тома). Поведение по умолчанию:

- знания хранятся рядом с образом (в каталоге образа, если он доступен
  для записи) либо в ``MOUNTFILE_LAYERS_DIR``;
- ``MOUNTFILE_DISABLE_LAYERS=1`` полностью выключает слои — работает
  оригинальный :func:`mountfile.autopick.auto_select`;
- любой сбой пути со знаниями => фолбэк на оригинал.

Run the tests:  python -m pytest tests/test_selflearning_layers.py -v
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .pipeline import SelfLearningStack

_LOG = logging.getLogger(__name__)

ENV_DISABLE = "MOUNTFILE_DISABLE_LAYERS"
ENV_STORE = "MOUNTFILE_LAYERS_DIR"


def stack_for(path: str | os.PathLike[str]) -> SelfLearningStack | None:
    """Стек слоёв для образа или None, если слои выключены/недоступны."""
    if os.environ.get(ENV_DISABLE):
        return None
    root = os.environ.get(ENV_STORE)
    if not root:
        parent = Path(path).parent
        if os.access(parent, os.W_OK):
            root = str(parent)
    if not root:
        return None
    try:
        return SelfLearningStack(root)
    except Exception:  # noqa: BLE001 - знания не должны ломать подбор
        return None


def auto_select_learned(path: str, **kwargs: Any) -> tuple[Any, list[Any]]:
    """Обёртка :func:`mountfile.autopick.auto_select` со слоем знаний.

    Контракт совпадает с оригиналом: ``(best, candidates)``. Успешный
    verify-монтирование лучшего кандидата пишется в слой как явный
    фидбек (accepted=True).
    """
    from .. import autopick

    stack = stack_for(path)
    if stack is None:
        return autopick.auto_select(path, **kwargs)
    best, ranked = stack.volume.select(path, **kwargs)
    if best is not None and getattr(best, "backend", None):
        stack.volume.feedback(best, True)
    return best, ranked


def preflight_learned(image: str, **kwargs: Any) -> Any:
    """Обёртка :func:`mountfile.fs_preflight.preflight_image` со слоем.

    Оригинальный fail-closed payload возвращается без изменений; слой
    лишь накапливает статистику проходимости по семействам ФС.
    """
    from .. import fs_preflight

    payload = fs_preflight.preflight_image(image, **kwargs)
    stack = stack_for(image)
    if stack is not None and isinstance(payload, dict):
        try:
            for volume in payload.get("volumes", []):
                if isinstance(volume, dict):
                    stack.preflight.observe_dict(volume)
        except Exception:  # noqa: BLE001 - знания не ломают preflight
            _LOG.debug("preflight layer feedback failed", exc_info=True)
    return payload
