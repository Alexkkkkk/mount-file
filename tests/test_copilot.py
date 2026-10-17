# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Focused tests for the metadata-only forensic copilot summary."""

from concurrent.futures import Future

from mountfile.ai.copilot import (SECTION_NAMES, CopilotSummaryBuilder,
                                  build_summary, build_summary_background)


def test_empty_inputs_keep_all_sections_without_running_analysis():
    calls = []
    builder = CopilotSummaryBuilder(
        image_provider=lambda: calls.append("image"),
        timeline_provider=lambda: calls.append("timeline"),
        anomalies_provider=lambda: calls.append("anomalies"),
    )

    # Construction is side-effect free; explicit empty values also bypass providers.
    assert calls == []
    summary = builder.build(None, [], [])

    assert tuple(summary) == SECTION_NAMES
    assert all(summary[name]["status"] == "empty" for name in SECTION_NAMES)
    assert calls == []


def test_partial_provider_failure_does_not_hide_other_sections():
    summary = build_summary(
        image_provider=lambda: {"filename": "image.raw", "sha256": "abc"},
        timeline_provider=lambda: (_ for _ in ()).throw(RuntimeError("raw bytes")),
        anomalies_provider=lambda: [{"type": "timestomp", "severity": "high"}],
    )

    assert summary["image"]["status"] == "ok"
    assert summary["image"]["fingerprint"]["filename"] == "image.raw"
    assert summary["timeline"]["status"] == "error"
    assert summary["timeline"]["events"] == []
    assert "RuntimeError" in summary["timeline"]["error"]
    assert summary["anomalies"]["status"] == "ok"
    assert summary["anomalies"]["findings"][0]["type"] == "timestomp"


def test_output_is_deterministic_bounded_and_never_contains_raw_bytes():
    provider = lambda: {
        "size": 12,
        "head": b"secret file bytes",
        "sha256": "deadbeef",
        "nested": {"b": 2, "a": 1},
    }
    first = build_summary(image_provider=provider, max_items=1)
    second = build_summary(image_provider=provider, max_items=1)

    assert first == second
    assert first["image"]["fingerprint"]["head"] == "<redacted bytes>"
    assert "secret file bytes" not in repr(first)

    events = build_summary(timeline_events=(item for item in range(3)), max_items=2)
    assert events["timeline"]["events"] == [0, 1]
    assert events["timeline"]["truncated"] is True


def test_background_entrypoint_returns_future_and_runs_only_when_submitted():
    future = build_summary_background(
        image_provider=lambda: {"filename": "case.dd"},
        timeline_events=[{"path": "/a"}],
        anomaly_findings=[],
    )

    assert isinstance(future, Future)
    result = future.result(timeout=2)
    assert result["image"]["fingerprint"]["filename"] == "case.dd"
    assert result["timeline"]["events"] == [{"path": "/a"}]
    assert result["anomalies"]["status"] == "empty"
