# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Regression checks for the CI gates introduced for tracker #510.

The workflow is intentionally checked as text: this keeps the test independent
from a YAML parser while still preventing a future edit from silently dropping
one of the manifest, probe, or strict slice checks.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "acceptance-gates.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_acceptance_workflow_is_present_and_has_full_trigger_set() -> None:
    text = _workflow_text()
    assert "name: Acceptance gates" in text
    assert "push:" in text
    assert "pull_request:" in text
    assert "workflow_dispatch:" in text
    assert "fetch-depth: 0" in text


def test_acceptance_workflow_runs_all_tracker_gates() -> None:
    text = _workflow_text()
    required_commands = (
        "python scripts/replay_acceptance_status.py --check",
        "python scripts/acceptance_blocks.py --check",
        "python scripts/replay_slice_sync.py --check --strict",
    )
    for command in required_commands:
        assert command in text, command


def test_acceptance_workflow_validates_artifact_and_regression_suite() -> None:
    text = _workflow_text()
    assert "python scripts/failure_matrix_artifact.py" in text
    assert "--validate acceptance-probe.json" in text
    for test_file in (
        "tests/test_acceptance_blocks.py",
        "tests/test_acceptance_cells_typed.py",
        "tests/test_failure_matrix_artifact.py",
        "tests/test_replay_acceptance_status.py",
        "tests/test_replay_slice_sync.py",
        "tests/test_replay_nightly_evidence.py",
    ):
        assert test_file in text, test_file
