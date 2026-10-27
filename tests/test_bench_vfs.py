# SPDX-License-Identifier: MIT
"""Tests for repeatable VFS benchmark aggregation."""

from benchmarks.bench_vfs import _benchmark, _summarize


def test_summarize_uses_median_and_median_absolute_deviation():
    samples = [
        {"elapsed_seconds": 1.0, "backend": "native"},
        {"elapsed_seconds": 3.0, "backend": "native"},
        {"elapsed_seconds": 100.0, "backend": "native"},
    ]

    result = _summarize(samples)

    assert result["elapsed_seconds"] == 3.0
    assert result["elapsed_seconds_median"] == 3.0
    assert result["elapsed_seconds_mad"] == 2.0
    assert result["elapsed_seconds_min"] == 1.0
    assert result["elapsed_seconds_max"] == 100.0
    assert result["sample_count"] == 3
    assert result["samples"] == samples
    assert result["backend"] == "native"


def test_benchmark_discards_warmups_and_aggregates_only_timed_runs(monkeypatch):
    calls = []

    def fake_run(factory, image, offset, volume_size, paths):
        calls.append((factory, image, offset, volume_size, paths))
        elapsed = float(len(calls))
        return {"elapsed_seconds": elapsed, "backend": "fake"}

    monkeypatch.setattr("benchmarks.bench_vfs._run_one", fake_run)
    factory = object()
    result = _benchmark(factory, "image", 17, 100, ["/a"], warmups=2, repeats=3)

    assert len(calls) == 5
    assert all(call == (factory, "image", 17, 100, ["/a"]) for call in calls)
    assert result["sample_count"] == 3
    assert [sample["elapsed_seconds"] for sample in result["samples"]] == [
        3.0,
        4.0,
        5.0,
    ]
    assert result["elapsed_seconds_median"] == 4.0
