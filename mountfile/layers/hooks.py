# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Точки подключения самообучающихся слоёв в реальный код Mount File.

Используется в CLI (``--auto`` и команда ``autopick``) и в GUI
(автоподбор тома). Поведение по умолчанию:

- знания хранятся в пользовательском каталоге либо в ``MOUNTFILE_LAYERS_DIR``;
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
    if os.environ.get(ENV_DISABLE, "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    root = os.environ.get(ENV_STORE)
    if not root:
        # Keep mutable personalization state out of the evidence directory.
        home = Path.home()
        if os.access(home, os.W_OK):
            root = str(home)
    if not root:
        return None
    try:
        store_root = Path(root)
        writable_parent = store_root if store_root.exists() else store_root.parent
        if not os.access(writable_parent, os.W_OK):
            return None
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
    if stack is None or not stack.is_enabled("volume"):
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
    if stack is not None and stack.is_enabled("preflight") and isinstance(payload, dict):
        try:
            for volume in payload.get("volumes", []):
                if isinstance(volume, dict):
                    stack.preflight.observe_dict(volume)
        except Exception:  # noqa: BLE001 - знания не ломают preflight
            _LOG.debug("preflight layer feedback failed", exc_info=True)
    return payload


def record_cli_execution(command: str, exit_code: int, duration_seconds: float) -> None:
    """Record command name, outcome and duration only when telemetry is enabled."""
    stack = stack_for(Path.home())
    if stack is None or not stack.is_enabled("operations"):
        return
    try:
        stack.operations.record(command, exit_code, duration_seconds)
    except Exception:  # noqa: BLE001 - telemetry must never change command results
        _LOG.debug("CLI operation telemetry failed", exc_info=True)
