# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Тесты реестра возможностей и его связи со слоями.

Run:  python -m pytest tests/test_layer_registry.py -v
"""

from __future__ import annotations

import pytest

from mountfile.layers.registry import (
    DEFAULT_LAYER,
    LAYER_NAMES,
    LAYER_ROUTING,
    cli_commands,
    coverage,
    discover,
    gui_actions,
    package_modules,
)


def test_cli_commands_discovered_from_live_parser() -> None:
    commands = cli_commands()
    assert len(commands) >= 30
    for known in ("autopick", "preflight", "ls", "get", "put", "yara", "timeline"):
        assert known in commands


def test_package_modules_discovered() -> None:
    mods = package_modules()
    for known in ("cli", "autopick", "fs_preflight", "turbo", "layers"):
        assert known in mods
    assert "layers" in mods


def test_gui_actions_from_ast_without_qt() -> None:
    actions = gui_actions()
    assert all(name.startswith(("_on_", "action_")) for name in actions)


def test_routing_covers_every_command() -> None:
    commands = cli_commands()
    routed = coverage()
    assert set(routed) == set(LAYER_NAMES)
    flat = [cmd for cmds in routed.values() for cmd in cmds]
    assert sorted(flat) == sorted(commands)


def test_routing_targets_are_layers() -> None:
    assert set(LAYER_ROUTING.values()) <= set(LAYER_NAMES)
    assert DEFAULT_LAYER in LAYER_NAMES


def test_discover_shape() -> None:
    data = discover()
    assert data["version"] == 1
    assert data["cli_commands"] == cli_commands()
    assert data["modules"] == package_modules()
    assert data["gui_actions"] == gui_actions()
    assert set(data["layer_routing"]) == set(data["cli_commands"])


def test_layers_import_registry_without_qt() -> None:
    from mountfile.layers import hooks, pipeline  # noqa: F401
    from mountfile.layers.registry import discover as d

    assert d()["layer_routing"]["autopick"] == "volume"


@pytest.mark.parametrize("command,layer", [("autopick", "volume"), ("preflight", "preflight"), ("put", "cache"), ("yara", "advisor")])
def test_known_routes(command: str, layer: str) -> None:
    data = discover()
    assert data["layer_routing"][command] == layer
