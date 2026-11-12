#!/usr/bin/env python3
"""Emit and validate conservative cross-platform benchmark provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

SCHEMA = "mountfile.platform-benchmark-provenance/v1"
OPERATIONS = ("list_dir", "stat", "read_range", "extract", "write")
RESOURCE_LIMITS = {
    "max_image_bytes": 2 * 1024 * 1024 * 1024,
    "max_paths": 256,
    "max_range_read_bytes_per_file": 1 << 20,
    "max_total_extract_bytes_per_iteration": 256 * 1024 * 1024,
    "max_repeats": 100,
    "max_warmups": 20,
}
GENERATED_WORKTREE_ARTIFACTS = {
    "benchmark-provenance.json",
    "capability-conformance.json",
    "checker-matrix.json",
    "coverage.xml",
    "native-read-evidence.json",
    "platform-benchmark-provenance-validation.json",
    "platform-benchmark-provenance.json",
    "test-results.xml",
}


def _command_version(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0].strip() if output else None


def _git_value(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _source_worktree_status(repo_root: Path) -> str | None:
    status = _git_value(repo_root, "status", "--porcelain", "--untracked-files=all")
    if status is None:
        return (
            ""
            if _git_value(repo_root, "rev-parse", "--is-inside-work-tree") == "true"
            else None
        )
    source_lines = []
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if Path(path).name not in GENERATED_WORKTREE_ARTIFACTS:
            source_lines.append(line)
    return "\n".join(source_lines)


def _fixture_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def build_report(
    *, repo_root: Path, junit_path: Path | None = None, fixture: Path | None = None
) -> dict[str, object]:
    dirty = _source_worktree_status(repo_root)
    operations = {
        operation: {
            "status": "not_claimed",
            "evidence": "not_run",
            "parity": "not_claimed",
        }
        for operation in OPERATIONS
    }
    if junit_path is not None and junit_path.is_file():
        operations["list_dir"] = {
            "status": "covered_by_test_suite",
            "evidence": "junit",
        }
        operations["stat"] = {"status": "covered_by_test_suite", "evidence": "junit"}
        operations["read_range"] = {
            "status": "covered_by_test_suite",
            "evidence": "junit",
        }
        operations["extract"] = {"status": "covered_by_test_suite", "evidence": "junit"}
        operations["write"] = {
            "status": "covered_by_test_suite",
            "evidence": "junit",
            "parity": "not_claimed",
        }

    report = {
        "schema": SCHEMA,
        "schema_version": 1,
        "source": {
            "revision": _git_value(repo_root, "rev-parse", "HEAD"),
            "worktree_dirty": bool(dirty),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
        },
        "runner": {
            "os": platform.system().lower(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "github_runner_os": os.environ.get("RUNNER_OS"),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        },
        "tools": {
            name: _command_version(name)
            for name in ("mke2fs", "debugfs", "fsck.fat", "fsck.exfat", "ntfsfix")
        },
        "fixture": {
            "path": str(fixture) if fixture else None,
            "size_bytes": (
                fixture.stat().st_size if fixture and fixture.is_file() else None
            ),
            "sha256": _fixture_hash(fixture),
        },
        "workload": {
            "operations": operations,
            "repeats": 1,
            "warmups": 0,
            "resource_limits": dict(RESOURCE_LIMITS),
        },
        "claims": {
            "evidence": "benchmark_observed_only",
            "parity": "not_claimed",
            "power_loss": "unsupported",
            "crash_recovery": "not_claimed",
        },
        "verdict": "pass" if dirty == "" else "blocked",
    }
    return report


def validate_report(report: dict) -> dict[str, object]:
    blockers: list[str] = []
    if report.get("schema") != SCHEMA or report.get("schema_version") != 1:
        blockers.append("schema")
    source = report.get("source", {})
    runner = report.get("runner", {})
    workload = report.get("workload", {})
    claims = report.get("claims", {})
    if not isinstance(source.get("revision"), str) or not source["revision"]:
        blockers.append("source.revision")
    if source.get("worktree_dirty") is not False:
        blockers.append("source.worktree_dirty")
    if not isinstance(runner.get("os"), str) or not runner["os"]:
        blockers.append("runner.os")
    if not isinstance(runner.get("python"), str) or not runner["python"]:
        blockers.append("runner.python")
    if workload.get("resource_limits") != RESOURCE_LIMITS:
        blockers.append("workload.resource_limits")
    operations = workload.get("operations")
    if not isinstance(operations, dict) or set(operations) != set(OPERATIONS):
        blockers.append("workload.operations")
    else:
        for operation in OPERATIONS:
            item = operations[operation]
            if (
                not isinstance(item, dict)
                or item.get("parity", "not_claimed") != "not_claimed"
            ):
                blockers.append(f"workload.operations.{operation}.parity")
    if claims.get("evidence") != "benchmark_observed_only":
        blockers.append("claims.evidence")
    if claims.get("parity") != "not_claimed":
        blockers.append("claims.parity")
    if claims.get("power_loss") != "unsupported":
        blockers.append("claims.power_loss")
    if claims.get("crash_recovery") != "not_claimed":
        blockers.append("claims.crash_recovery")
    return {
        "schema": "mountfile.platform-benchmark-provenance-validation/v1",
        "verdict": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "platform": runner.get("os"),
        "parity": "not_claimed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.validate:
        result = validate_report(json.loads(args.validate.read_text(encoding="utf-8")))
    else:
        result = build_report(
            repo_root=args.repo_root, junit_path=args.junitxml, fixture=args.fixture
        )
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if result.get("verdict") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
