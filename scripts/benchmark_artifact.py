#!/usr/bin/env python3
"""Validate a saved read-only VFS benchmark artifact conservatively."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PATHS = 256
MAX_TOTAL_EXTRACT_BYTES = 256 * 1024 * 1024
MAX_REPEATS = 100
MAX_WARMUPS = 20
RANGE_READ_LIMIT_BYTES = 1 << 20
REQUIRED_OPERATIONS = {"list_dir", "stat", "read_range", "extract"}
REQUIRED_BACKENDS = {"native", "pytsk3"}


def validate_report(report: dict) -> dict[str, object]:
    blockers: list[str] = []
    if report.get("schema_version") != 2:
        blockers.append("schema_version")
    extent = report.get("extent", {})
    image = report.get("image", {})
    workload = report.get("workload", {})
    capability = report.get("capability_contract", {})
    runtime = report.get("environment", {}).get("runtime_versions", {})
    tool_versions = report.get("environment", {}).get("tool_versions", {})
    if not isinstance(extent.get("offset_bytes"), int) or extent["offset_bytes"] < 0:
        blockers.append("extent.offset_bytes")
    if (
        not isinstance(extent.get("volume_size_bytes"), int)
        or extent["volume_size_bytes"] <= 0
    ):
        blockers.append("extent.volume_size_bytes")
    if (
        not isinstance(image.get("size_bytes"), int)
        or not 0 < image["size_bytes"] <= MAX_IMAGE_BYTES
    ):
        blockers.append("image.size_bytes")
    if (
        isinstance(extent.get("offset_bytes"), int)
        and isinstance(extent.get("volume_size_bytes"), int)
        and isinstance(image.get("size_bytes"), int)
        and extent["offset_bytes"] + extent["volume_size_bytes"] > image["size_bytes"]
    ):
        blockers.append("extent.outside_image")
    repeats = workload.get("repeats")
    warmups = workload.get("warmups")
    if not isinstance(repeats, int) or not 1 <= repeats <= MAX_REPEATS:
        blockers.append("workload.repeats")
    if not isinstance(warmups, int) or not 0 <= warmups <= MAX_WARMUPS:
        blockers.append("workload.warmups")
    if workload.get("range_read_limit_bytes_per_file") != RANGE_READ_LIMIT_BYTES:
        blockers.append("workload.range_read_limit_bytes_per_file")
    expected_limits = {
        "max_image_bytes": MAX_IMAGE_BYTES,
        "max_paths": MAX_PATHS,
        "max_total_extract_bytes_per_iteration": MAX_TOTAL_EXTRACT_BYTES,
        "max_range_read_bytes_per_file": RANGE_READ_LIMIT_BYTES,
        "max_repeats": MAX_REPEATS,
        "max_warmups": MAX_WARMUPS,
    }
    if workload.get("resource_limits") != expected_limits:
        blockers.append("workload.resource_limits")
    paths = workload.get("paths")
    paths_valid = (
        isinstance(paths, list)
        and 1 <= len(paths) <= MAX_PATHS
        and all(
            isinstance(path, str)
            and path.startswith("/")
            and ".." not in path.split("/")
            for path in paths
        )
    )
    if not paths_valid:
        blockers.append("workload.paths")
    if not isinstance(tool_versions, dict) or not (
        isinstance(tool_versions.get("mke2fs"), str) and tool_versions["mke2fs"].strip()
    ):
        blockers.append("environment.tool_versions.mke2fs")
    if (
        paths_valid
        and isinstance(paths, list)
        and any(path.endswith("large_10m.dat") for path in paths)
        and not (
            isinstance(tool_versions, dict)
            and isinstance(tool_versions.get("debugfs"), str)
            and tool_versions["debugfs"].strip()
        )
    ):
        blockers.append("environment.tool_versions.debugfs")
    if capability.get("evidence") != "benchmark_observed_only":
        blockers.append("capability_contract.evidence")
    if capability.get("parity") != "not_claimed":
        blockers.append("capability_contract.parity")
    if runtime.get("source_worktree_dirty") is not False:
        blockers.append("environment.runtime_versions.source_worktree_dirty")
    comparison = report.get("comparison", {})
    if comparison.get("content_equivalent") is not True:
        blockers.append("comparison.content_equivalent")
    if comparison.get("content_mismatches") != []:
        blockers.append("comparison.content_mismatches")
    backends = report.get("backends", {})
    observed = []
    for name, backend in backends.items():
        if name.endswith("_error"):
            continue
        observed.append(name)
        if backend.get("capability_status", {}).get("parity") != "not_claimed":
            blockers.append(f"backend.{name}.parity")
        operations = backend.get("operations", {})
        if set(operations) != REQUIRED_OPERATIONS:
            blockers.append(f"backend.{name}.operations")
        if len(backend.get("iterations", [])) != repeats:
            blockers.append(f"backend.{name}.iterations")
        for index, iteration in enumerate(backend.get("iterations", [])):
            extracted_bytes = iteration.get("extracted_bytes")
            if (
                type(extracted_bytes) is not int
                or extracted_bytes < 0
                or extracted_bytes > MAX_TOTAL_EXTRACT_BYTES
            ):
                blockers.append(f"backend.{name}.iterations[{index}].extracted_bytes")
    if not REQUIRED_BACKENDS.issubset(backends):
        blockers.append("backends.required")
    return {
        "schema": 1,
        "report": "mountfile-benchmark-artifact-validation",
        "verdict": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "observed_backends": observed,
        "parity": "not_claimed",
        "write_operations": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = validate_report(json.loads(args.artifact.read_text(encoding="utf-8")))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
