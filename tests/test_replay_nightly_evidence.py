# SPDX-License-Identifier: MIT
"""#387 / #510 D2+D6: offline tests for the scheduled-nightly evidence query."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_nightly_evidence.py"
_spec = importlib.util.spec_from_file_location("replay_nightly_evidence", _SCRIPT)
module = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(module)


def _run(idx, *, status="completed", conclusion="success", event="schedule"):
    return {
        "id": idx,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "created_at": f"2026-11-{10 + idx:02d}T01:00:00Z",
        "head_sha": f"sha{idx}",
        "html_url": f"https://example.invalid/{idx}",
    }


def test_two_consecutive_successful_scheduled_runs_satisfy_the_criterion():
    report = module.evaluate([_run(1), _run(2)], required=2)
    assert report["consecutive_successes"] == 2
    assert report["criterion_satisfied"] is True
    assert report["outcome"] == module.OUTCOME_SATISFIED
    # Reporting scheduling evidence never promotes the checklist item.
    assert report["acceptance"] == "not_claimed"


def test_a_single_nightly_run_is_not_enough():
    report = module.evaluate([_run(1)], required=2)
    assert report["consecutive_successes"] == 1
    assert report["criterion_satisfied"] is False
    assert report["outcome"] == module.OUTCOME_UNSATISFIED


def test_latest_failure_breaks_the_streak_regardless_of_history():
    report = module.evaluate(
        [_run(1), _run(2), _run(3, conclusion="failure")], required=2
    )
    assert report["consecutive_successes"] == 0
    assert report["criterion_satisfied"] is False


def test_running_or_pull_request_runs_are_ignored():
    runs = [
        _run(1),
        _run(2, conclusion="success"),
        _run(3, status="in_progress", conclusion=None),
        _run(4, event="pull_request"),
        _run(5, event="workflow_dispatch"),
    ]
    report = module.evaluate(runs, required=2)
    assert report["completed_scheduled_runs"] == 2
    assert report["consecutive_successes"] == 2
    assert {entry["id"] for entry in report["evidence"]} == {1, 2}


def test_missing_token_is_a_typed_unavailable_outcome(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    report = module.build_report("owner/name", transport=lambda url, token: {})
    assert report["outcome"] == module.OUTCOME_UNAVAILABLE
    assert report["criterion_satisfied"] is False
    assert "token" in report["reason"]
    assert report["acceptance"] == "not_claimed"


def test_malformed_payload_never_turns_green():
    report = module.build_report(
        "owner/name",
        token="t0ken",
        transport=lambda url, token: {"workflow_runs": "not-a-list"},
    )
    assert report["outcome"] == module.OUTCOME_UNAVAILABLE
    assert report["criterion_satisfied"] is False


def test_transport_payload_is_normalised_and_queried_with_schedule_filter():
    seen = {}

    def transport(url, token):
        seen["url"] = url
        seen["token"] = token
        return {"workflow_runs": [{"id": 9, "status": "completed", "conclusion": "success"}]}

    runs = module.fetch_scheduled_runs(
        "owner/name", token="t0ken", transport=transport
    )
    assert "event=schedule" in seen["url"]
    assert "/repos/owner/name/actions/workflows/replay-nightly.yml/runs" in seen["url"]
    assert seen["token"] == "t0ken"
    assert runs[0]["event"] == "schedule"
    assert runs[0]["html_url"] is None


def test_cli_fails_closed_without_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    out = tmp_path / "nightly.json"
    code = module.main(
        [
            "--repo",
            "owner/name",
            "--json-out",
            str(out),
            "--fail-on-unsatisfied",
        ]
    )
    assert code == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["outcome"] == "unavailable"
    assert payload["criterion_satisfied"] is False
