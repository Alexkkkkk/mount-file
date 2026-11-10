# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Offline security contracts for the legacy multi-agent executor."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_legacy_module():
    spec = importlib.util.spec_from_file_location(
        "mountfile_ai_executor_test", ROOT / "mountfile_ai.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_executor_pins_tool_path_to_current_image(monkeypatch, tmp_path):
    # Config creates its workspace at import time; keep that side effect in the
    # test's temporary directory rather than in the checkout.
    monkeypatch.chdir(tmp_path)
    module = _load_legacy_module()

    selected_image = tmp_path / "selected.img"
    other_image = tmp_path / "other-allowlisted.img"
    selected_image.write_bytes(b"selected evidence")
    other_image.write_bytes(b"other evidence")
    captured = []

    def fake_dispatch(tool, args):
        captured.append((tool, dict(args)))
        return module.ExecutionRecord(
            execution_id="exec_0001",
            tool=tool,
            args=dict(args),
            ok=True,
            result=[],
        )

    monkeypatch.setattr(module, "dispatch", fake_dispatch)
    plan = module.Plan(
        steps=[
            module.ToolCall(
                tool="list_partitions",
                args={"image_path": str(other_image)},
            )
        ]
    )

    records = module.Executor().run(plan, selected_image)

    assert captured[0][0] == "list_partitions"
    assert captured[0][1]["image_path"] == str(selected_image)
    assert records[0].ok is True
