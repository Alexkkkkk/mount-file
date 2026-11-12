#!/usr/bin/env python3
"""Emit conservative provenance for cross-platform benchmark evidence.

This manifest deliberately does not turn a platform test run into filesystem
performance parity. A platform reports ``executed`` only when a validated
benchmark artifact is supplied; otherwise it reports ``not_collected`` with a
machine-readable reason. The manifest is safe to generate on every CI runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

SCHEMA = "mountfile.benchmark-provenance/v1"
DEFAULT_RESOURCE_LIMITS = {
    "max_image_bytes": 2 * 1024 * 1024 * 1024,
    "max_paths": 256,
    "max_total_extract_bytes_per_iteration": 256 * 1024 * 1024,
    "max_range_read_bytes_per_file": 1 << 20,
    "max_repeats": 100,
    "max_warmups": 20,
}


def _source_revision(root: Path) -> tuple[str | None, bool | None]:
    try:
        revision = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or None
        )
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return revision, dirty


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    root: Path,
    *,
    benchmark_report: Path | None = None,
    benchmark_validation: Path | None = None,
    fixture: Path | None = None,
) -> dict:
    revision, dirty = _source_revision(root)
    report_exists = benchmark_report is not None and benchmark_report.is_file()
    validation_exists = (
        benchmark_validation is not None and benchmark_validation.is_file()
    )
    if report_exists and validation_exists:
        status = "executed"
        reason = "validated benchmark artifact supplied"
    else:
        status = "not_collected"
        reason = "platform benchmark artifact was not collected on this runner"

    manifest = {
        "schema": SCHEMA,
        "runner": {
            "platform": platform.system().lower(),
            "platform_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "source": {
            "revision": revision,
            "worktree_dirty": dirty,
        },
        "benchmark": {
            "status": status,
            "reason": reason,
            "evidence": "benchmark_observed_only" if status == "executed" else "none",
            "parity": "not_claimed",
            "write_operations": False,
            "report_sha256": (
                _file_sha256(benchmark_report) if report_exists else None
            ),
            "validation_sha256": (
                _file_sha256(benchmark_validation) if validation_exists else None
            ),
        },
        "fixture": {
            "status": "declared" if fixture is not None else "not_declared",
            "path": str(fixture) if fixture is not None else None,
            "sha256": (
                _file_sha256(fixture)
                if fixture is not None and fixture.is_file()
                else None
            ),
        },
        "resource_limits": dict(DEFAULT_RESOURCE_LIMITS),
        "limitations": [
            "Provenance is not a performance result.",
            "parity remains not_claimed unless an independent checker artifact says otherwise.",
            "Missing platform tools or fixtures are reported, never converted to pass.",
        ],
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--benchmark-report", type=Path)
    parser.add_argument("--benchmark-validation", type=Path)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    manifest = build_manifest(
        root,
        benchmark_report=args.benchmark_report,
        benchmark_validation=args.benchmark_validation,
        fixture=args.fixture,
    )
    args.json_out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
