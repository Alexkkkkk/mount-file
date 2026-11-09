# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Реестр возможностей: рантайм-интроспекция всего функционала.

Никаких статических списков, которые устареют: команды CLI читаются из
живого argparse-парсера (:func:`mountfile.cli.build_parser`), модули
пакета — через :mod:`pkgutil`, GUI-действия — через AST-разбор
``tc_window.py`` (импортировать Qt не требуется). Каждая возможность
маршрутизируется на обучаемый слой, который её «ведёт».

Run the tests:  python -m pytest tests/test_layer_registry.py -v
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path
from typing import Any

# Маршрутизация CLI-команд по предметным слоям. Непокрытые команды получают
# только агрегированную телеметрию в отдельном opt-in operations-слое.
LAYER_ROUTING: dict[str, str] = {
    "autopick": "volume",
    "ls": "volume",
    "stat": "volume",
    "get": "volume",
    "read-range": "volume",
    "preflight": "preflight",
    "ai-guard": "advisor",
    "ai-schemas": "advisor",
    "ai-analyze": "advisor",
    # The following are explicit operation telemetry only; they do not map to
    # an AI policy or claim that a learned result controls their semantics.
    "volumes": "operations",
    "info": "operations",
    "capabilities": "operations",
    "status": "operations",
    "put": "operations",
    "write-range": "operations",
    "append": "operations",
    "truncate": "operations",
    "mkdir": "operations",
    "mv": "operations",
    "rm": "operations",
    "ln": "operations",
    "decode": "operations",
    "split": "operations",
    "grep": "operations",
    "hash": "operations",
    "evidence-manifest": "operations",
    "timeline": "operations",
    "timeline-export": "operations",
    "timestomp": "operations",
    "yara": "operations",
    "unallocated-scan": "operations",
    "slack": "operations",
    "timeline-build": "operations",
    "timeline-query": "operations",
    "search": "operations",
    "yara-scan": "operations",
    "timestomp-scan": "operations",
    "slack-extract": "operations",
    "live-diff": "operations",
    "layers": "operations",
}
DEFAULT_LAYER = "operations"
LAYER_NAMES = (
    "volume",
    "preflight",
    "cache",
    "advisor",
    "code_repair",
    "operations",
)


def cli_commands() -> list[str]:
    """Все подкоманды CLI из живого argparse-парсера."""
    from .. import cli

    parser = cli.build_parser()
    for action in parser._actions:  # noqa: SLF001 - публичный API argparse
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and choices:
            return sorted(choices)
    return []


def package_modules() -> list[str]:
    """Все модули пакета ``mountfile`` (один уровень, без импорта)."""
    pkg = importlib.import_module(__package__.split(".", 1)[0])
    return sorted(info.name for info in pkgutil.iter_modules(pkg.__path__))


def gui_actions() -> list[str]:
    """Имена GUI-действий из AST ``tc_window.py`` (без импорта Qt)."""
    pkg = importlib.import_module(__package__.split(".", 1)[0])
    source = (Path(pkg.__path__[0]) / "tc_window.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith(("_on_", "action_")):
            names.append(node.name)
    return sorted(names)


def discover() -> dict[str, Any]:
    """Полная карта возможностей + маршрут на обучаемые слои."""
    try:
        commands = cli_commands()
    except Exception:  # noqa: BLE001 - без CLI остаётся AST/pkgutil
        commands = []
    try:
        mods = package_modules()
    except Exception:  # noqa: BLE001
        mods = []
    try:
        actions = gui_actions()
    except Exception:  # noqa: BLE001
        actions = []
    routing = {command: LAYER_ROUTING.get(command, DEFAULT_LAYER) for command in commands}
    return {
        "version": 1,
        "cli_commands": commands,
        "modules": mods,
        "gui_actions": actions,
        "layer_routing": routing,
    }


def coverage() -> dict[str, list[str]]:
    """Какие команды ведёт каждый слой (для GUI, CLI и тестов полноты)."""
    commands = discover()["cli_commands"]
    result: dict[str, list[str]] = {name: [] for name in LAYER_NAMES}
    for command in commands:
        result[LAYER_ROUTING.get(command, DEFAULT_LAYER)].append(command)
    return result
