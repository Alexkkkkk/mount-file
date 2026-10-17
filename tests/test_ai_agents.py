# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Focused tests for the deterministic investigation orchestrator."""

from mountfile.ai.agents import Claim, InvestigationOrchestrator, ToolCall


def test_workflow_order_and_deterministic_execution_ids():
    events = []

    def planner(query):
        events.append(("planner", query))
        return [ToolCall("signature", {"path": "image.raw"}), ToolCall(name="hash")]

    def executor(call):
        events.append(("executor", call.tool))
        return {"tool": call.tool}

    def analyst(query, executions):
        events.append(("analyst", query))
        assert [item.execution_id for item in executions] == ["exec_0001", "exec_0002"]
        return [Claim("signature found", executions[0].execution_id)]

    def skeptic(claim, executions):
        events.append(("skeptic", claim.execution_id))
        assert executions[0].execution_id == claim.execution_id
        return True

    result = InvestigationOrchestrator(planner, executor, analyst, skeptic).run("find FS")

    assert events == [
        ("planner", "find FS"),
        ("executor", "signature"),
        ("executor", "hash"),
        ("analyst", "find FS"),
        ("skeptic", "exec_0001"),
    ]
    assert [item.execution_id for item in result.executions] == ["exec_0001", "exec_0002"]
    assert [item.statement for item in result.accepted_claims] == ["signature found"]


def test_claim_without_known_execution_is_rejected_before_skeptic():
    skeptic_calls = []

    def analyst(_executions):
        return [Claim("invented evidence", "exec_9999")]

    def skeptic(claim, _executions):
        skeptic_calls.append(claim)
        return True

    result = InvestigationOrchestrator(
        lambda _query: [ToolCall("inspect")],
        lambda _call: {"present": True},
        analyst,
        skeptic,
    ).investigate("check")

    assert result.accepted_claims == []
    assert [claim.statement for claim in result.rejected_claims] == ["invented evidence"]
    assert skeptic_calls == []


def test_supported_claims_can_be_accepted_or_rejected_by_skeptic():
    def analyst(_query, executions):
        return [
            Claim("keep", executions[0].execution_id),
            Claim("discard", executions[0].execution_id),
            Claim("missing", "exec_missing"),
        ]

    result = InvestigationOrchestrator(
        lambda _query: [ToolCall("inspect")],
        lambda _call: "evidence",
        analyst,
        lambda claim, _executions: claim.statement == "keep",
    ).run("check")

    assert [claim.statement for claim in result.accepted_claims] == ["keep"]
    assert [claim.statement for claim in result.rejected_claims] == ["discard", "missing"]
