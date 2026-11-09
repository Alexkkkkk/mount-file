# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Advisory control-plane and explicit-feedback regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mountfile.layers.control import (
    DecisionOrchestrator,
    Feedback,
    Hooks,
    LayerContext,
)
from mountfile.layers.manager import LayerManager
from mountfile.layers.pipeline import SelfLearningStack


def test_preflight_decision_is_a_hint_and_feedback_is_one_time(tmp_path: Path) -> None:
    stack = SelfLearningStack(str(tmp_path))
    for result in (True, True, True):
        stack.preflight.observe_dict(
            {"filesystem": "NTFS", "eligible_for_read": result}
        )

    hooks = Hooks()
    events = []
    hooks.before("decide", lambda _context: events.append("before"))
    hooks.after("decide", lambda _context, _decisions: events.append("after"))
    orchestrator = DecisionOrchestrator(str(tmp_path), hooks=hooks)
    decisions = orchestrator.decide(
        LayerContext(
            path="/evidence/private-case.img",
            fs_type="NTFS",
            operation="preflight",
            extras={"secret": "must-not-persist"},
        )
    )

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.advisory_only is True
    assert decision.action == "historical_pass_hint"
    assert decision.value > 0.5
    assert events == ["before", "after"]
    serialized = orchestrator.store.path.read_text(encoding="utf-8")
    assert "/evidence/private-case.img" not in serialized
    assert "must-not-persist" not in serialized

    assert orchestrator.feedback(Feedback(decision.decision_id, False)) is True
    assert orchestrator.feedback(Feedback(decision.decision_id, True)) is False
    assert orchestrator.stack.preflight.observations == 4


def test_volume_decision_does_not_persist_candidate_names_or_paths(tmp_path: Path) -> None:
    candidates = [
        SimpleNamespace(
            score=4.0,
            offset=4096,
            size=8192,
            fstype="ext4",
            label="sensitive-volume-label",
            name="/dev/private-device",
            backend=object(),
        )
    ]
    orchestrator = DecisionOrchestrator(str(tmp_path))
    decisions = orchestrator.decide(
        LayerContext(
            path="/evidence/case.img",
            fs_type="ext4",
            operation="autopick",
            candidates=candidates,
        )
    )
    assert decisions[0].advisory_only
    assert decisions[0].action == "rank_order_hint"
    assert "name" not in decisions[0].value

    persisted = orchestrator.store.path.read_text(encoding="utf-8")
    assert "/evidence/case.img" not in persisted
    assert "sensitive-volume-label" not in persisted
    assert "/dev/private-device" not in persisted
    assert orchestrator.feedback(Feedback(decisions[0].decision_id, True))
    assert orchestrator.stack.volume.observations == 1


def test_decision_requires_supported_operation_and_respects_layer_switch(tmp_path: Path) -> None:
    manager = LayerManager(tmp_path)
    manager.set_enabled("preflight", False)
    orchestrator = DecisionOrchestrator(str(tmp_path))
    assert orchestrator.decide(LayerContext(fs_type="ext4", operation="preflight")) == []
    with pytest.raises(ValueError, match="unsupported advisory operation"):
        orchestrator.decide(LayerContext(operation="write"))


def test_reset_layer_invalidates_pending_feedback(tmp_path: Path) -> None:
    stack = SelfLearningStack(str(tmp_path))
    for _ in range(3):
        stack.preflight.observe_dict(
            {"filesystem": "ext4", "eligible_for_read": True}
        )
    orchestrator = DecisionOrchestrator(str(tmp_path))
    decisions = orchestrator.decide(LayerContext(fs_type="ext4", operation="preflight"))
    assert decisions

    LayerManager(tmp_path).reset("preflight")
    assert not DecisionOrchestrator(str(tmp_path)).feedback(
        Feedback(decisions[0].decision_id, True)
    )


def test_bad_feedback_is_rejected(tmp_path: Path) -> None:
    orchestrator = DecisionOrchestrator(str(tmp_path))
    assert not orchestrator.feedback(Feedback("not-a-uuid", True))
    assert not orchestrator.feedback(Feedback("0" * 32, True, latency_ms=float("nan")))


def test_layers_decide_and_feedback_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    from mountfile import cli

    monkeypatch.setenv("MOUNTFILE_DISABLE_LAYERS", "1")
    stack = SelfLearningStack(str(tmp_path))
    for _ in range(3):
        stack.preflight.observe_dict(
            {"filesystem": "ntfs", "eligible_for_read": True}
        )
    assert cli.main(
        [
            "layers",
            "decide",
            "/private/img.raw",
            "--operation",
            "preflight",
            "--fs",
            "NTFS",
            "--root",
            str(tmp_path),
            "--json",
        ]
    ) == cli.OK
    decisions = json.loads(capsys.readouterr().out)
    assert decisions[0]["advisory_only"] is True

    assert cli.main(
        [
            "layers",
            "feedback",
            decisions[0]["decision_id"],
            "--failure",
            "--root",
            str(tmp_path),
        ]
    ) == cli.OK
    assert "recorded" in capsys.readouterr().out


def test_code_repair_toggle_disables_olma_suggestions(tmp_path: Path, monkeypatch, capsys) -> None:
    from mountfile.layers.manager import LayerManager
    from mountfile.olma.cli import olma_main

    monkeypatch.setenv("MOUNTFILE_LAYERS_DIR", str(tmp_path))
    LayerManager().set_enabled("code_repair", False)
    assert olma_main(["suggest", "--root", str(tmp_path)]) == 0
    assert "suggestions are disabled" in capsys.readouterr().out
