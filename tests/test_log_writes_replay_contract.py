# SPDX-License-Identifier: MIT
"""Contract tests for the (draft, unverified) B8 log-writes gate."""

from pathlib import Path

from scripts.log_writes_replay_check import CLAIMS, SCHEMA, UNAVAILABLE_EXIT

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "b8-log-writes.yml"
SCRIPT = ROOT / "scripts" / "log_writes_replay_check.py"


def test_workflow_is_fail_closed_and_not_scheduled():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "LOG_WRITES_SHA256: ''" in text
    assert "pin the xfstests log-writes.py parser by SHA-256" in text
    assert "log-writes target unavailable" in text
    assert "/dev/loop-control is missing" in text
    # The gate must not run on a schedule, plain pushes or PRs until
    # verified: dispatch-only, fail-closed on the empty pin.
    assert "schedule:" not in text
    assert "branches: [main]" not in text
    assert "pull_request:" not in text
    assert "workflow_dispatch" in text
    for action in (
        "actions/checkout@",
        "actions/setup-python@",
        "actions/upload-artifact@",
    ):
        assert action in text


def test_script_is_typed_unavailable_and_never_claims_durability():
    text = SCRIPT.read_text(encoding="utf-8")
    assert SCHEMA in text
    assert str(UNAVAILABLE_EXIT) in text
    for claim in CLAIMS:
        assert claim in text
    # Draft honesty: the script must declare it never executed end to end.
    assert "UNVERIFIED" in text or "never executed" in text
    # No force flags anywhere: a draft must not carry destructive shortcuts.
    assert "--force" not in text
    assert "--resync" not in text
