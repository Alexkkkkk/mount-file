# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Тесты hook-обвязки слоёв в CLI и GUI.

Run:  python -m pytest tests/test_layer_hooks.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mountfile.layers import hooks


@pytest.fixture()
def layers_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "layers-root"
    monkeypatch.setenv(hooks.ENV_STORE, str(store))
    monkeypatch.delenv(hooks.ENV_DISABLE, raising=False)
    return store


def test_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hooks.ENV_DISABLE, "1")
    monkeypatch.setenv(hooks.ENV_STORE, str(tmp_path))
    assert hooks.stack_for(tmp_path / "x.img") is None


def test_stack_for_uses_env_dir(layers_env: Path, tmp_path: Path) -> None:
    stack = hooks.stack_for(tmp_path / "x.img")
    assert stack is not None
    assert stack.store.path == layers_env / ".mountfile" / "layers.json"


def test_stack_for_readonly_dir_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(hooks.ENV_STORE, raising=False)
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        if os.access(readonly, os.W_OK):
            pytest.skip("sandbox ignores read-only mode")
        assert hooks.stack_for(readonly / "x.img") is None
    finally:
        readonly.chmod(0o755)


def test_preflight_learned_persists_family_stats(
    layers_env: Path, tmp_path: Path
) -> None:
    from mountfile.layers.base import BetaLearner, KnowledgeStore
    from mountfile.layers.preflight_layer import PreflightLayer

    # Предобучаем слой: ntfs проходит всегда.
    store = KnowledgeStore(layers_env)
    layer = PreflightLayer(store)
    layer.families["ntfs"] = BetaLearner()
    for _ in range(4):
        layer.families["ntfs"].update(True)
    layer.state["families"] = {
        key: value.to_dict() for key, value in layer.families.items()
    }
    store.save()

    image = tmp_path / "probe.img"
    image.write_bytes(b"\x00" * 512)

    payload = hooks.preflight_learned(str(image), offset=0)
    assert isinstance(payload, dict)
    assert "volumes" in payload
    # Слой выжил и доступен повторно.
    assert hooks.stack_for(image) is not None


def test_auto_select_learned_falls_back_on_bad_image(
    layers_env: Path, tmp_path: Path
) -> None:
    from mountfile import autopick

    image = tmp_path / "junk.img"
    image.write_bytes(b"\x00" * 4096)
    best, candidates = hooks.auto_select_learned(str(image), verify=False)
    best_orig, cands_orig = autopick.auto_select(str(image), verify=False)
    assert (best is None) == (best_orig is None)
    assert len(candidates) == len(cands_orig)
