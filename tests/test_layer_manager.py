# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Tests for the centrally managed advisory/self-learning layers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mountfile.layers.base import KnowledgeStore
from mountfile.layers.manager import LayerManager
from mountfile.layers.operation_layer import OperationLayer
from mountfile.layers.registry import LAYER_NAMES, cli_commands, discover


def test_manager_reports_every_layer_and_every_cli_command(tmp_path: Path) -> None:
    status = LayerManager(tmp_path).status()
    assert set(status["layers"]) == set(LAYER_NAMES)
    assert set(status["cli"]["layer_routing"]) == set(cli_commands())
    assert set(status["cli"]["layer_routing"].values()) <= set(LAYER_NAMES)
    assert status["cli"]["layer_routing"]["autopick"] == "volume"
    assert status["cli"]["layer_routing"]["preflight"] == "preflight"
    assert status["cli"]["layer_routing"]["put"] == "operations"


def test_layer_enable_disable_persists(tmp_path: Path) -> None:
    manager = LayerManager(tmp_path)
    assert manager.enabled("operations") is False
    manager.set_enabled("operations", True)
    assert LayerManager(tmp_path).enabled("operations") is True
    manager.set_enabled("cache", False)
    assert LayerManager(tmp_path).enabled("cache") is False


def test_reset_clears_learning_but_preserves_enabled_flag(tmp_path: Path) -> None:
    manager = LayerManager(tmp_path)
    manager.set_enabled("advisor", False)
    state = manager.store.layer("advisor")
    state.update({"observations": 9, "formats": {"ntfs": {"a": 5, "b": 1}}})
    manager.store.save()

    manager.reset("advisor")
    status = LayerManager(tmp_path).status()["layers"]["advisor"]
    assert status == {"enabled": False, "observations": 0}
    assert LayerManager(tmp_path).store.layer("advisor") == {"enabled": False}


def test_unknown_layer_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown layer"):
        LayerManager(tmp_path).set_enabled("not-a-layer", True)


def test_operations_layer_stores_only_aggregate_command_outcomes(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path)
    layer = OperationLayer(store)
    layer.record("get", 0, 1.5)
    layer.record("put", 1, 3.0)

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["layers"]["operations"]["commands"] == {
        "get": {"successes": 1, "failures": 0},
        "put": {"successes": 0, "failures": 1},
    }
    assert "path" not in json.dumps(raw).lower()
    assert layer.observations == 2


def test_registry_has_no_unmapped_or_unknown_default_layer() -> None:
    routing = discover()["layer_routing"]
    assert "ln" in routing
    assert routing["ln"] == "operations"
    assert all(
        name != "advisor" or command.startswith("ai-")
        for command, name in routing.items()
    )
