# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Combined machine-readable acceptance artifact for tracker #510.

The artifact is deliberately additive: it does not overwrite
``docs/generated/recovery-acceptance.json`` (owned by
``scripts/recovery_acceptance.py``) and it never rewrites the tracker statuses.
It records what the probes actually observed, so a reviewer can compare a claim
with a measurement instead of re-reading prose.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from mountfile.acceptance.blocks import BLOCK_CHECKS, run_all
from mountfile.acceptance.checkers import probe_checkers, summarise

#: Schema identifier of the emitted payload.
SCHEMA = "mountfile.acceptance-blocks/v1"

#: Where the combined artifact is written relative to the repository root.
DEFAULT_OUTPUT = Path("docs/generated/acceptance-blocks.json")

#: The only disclaimer this project is allowed to make about parity.
PARITY_NOTE = (
    "Probe states describe this tree only. replay and native NTFS writes remain "
    "unsupported, and no probe claims filesystem parity or power-loss safety."
)


def generated_timestamp(root: str | Path) -> str:
    """Return a reproducible UTC timestamp for an artifact.

    ``SOURCE_DATE_EPOCH`` is the explicit build input. In a checked-out
    repository the HEAD commit timestamp is the next stable source. A
    non-git temporary tree gets the Unix epoch rather than the wall clock.
    """
    source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_epoch is not None:
        try:
            epoch = int(source_epoch)
            if epoch < 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "SOURCE_DATE_EPOCH must be a non-negative integer"
            ) from exc
    else:
        try:
            result = subprocess.run(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                cwd=str(Path(root)),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            epoch = int(result.stdout.strip()) if result.returncode == 0 else 0
        except (OSError, ValueError, subprocess.SubprocessError):
            epoch = 0
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def build_artifact(root: str | Path, manifest_path: str | Path | None = None) -> dict:
    """Run every probe and return the combined artifact payload."""

    repo = Path(root)
    if manifest_path is None:
        candidate = repo / "docs/replay-acceptance.json"
        manifest_path = candidate if candidate.is_file() else None
    result = run_all(repo, manifest_path)
    checkers = probe_checkers()
    return {
        "schema": SCHEMA,
        "generated": generated_timestamp(repo),
        "declared_checks": len(BLOCK_CHECKS),
        "counts": result["counts"],
        "drift": result["drift"],
        "checkers": {name: outcome.as_dict() for name, outcome in checkers.items()},
        "checker_counts": summarise(checkers),
        "items": result["items"],
        "parity": "not_claimed",
        "note": PARITY_NOTE,
    }


def write_artifact(
    root: str | Path,
    output: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> Path:
    """Write the artifact to ``output`` and return the path written."""

    repo = Path(root)
    target = Path(output) if output is not None else repo / DEFAULT_OUTPUT
    if not target.is_absolute():
        target = repo / target
    payload = build_artifact(repo, manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return target


def summarise_artifact(payload: dict) -> str:
    """Render a compact human summary of ``payload``."""

    counts = payload.get("counts") or {}
    drift = payload.get("drift") or []
    lines = [
        "блоки #510: {}".format(
            ", ".join(f"{key} {counts[key]}" for key in sorted(counts))
        ),
        "чекеры: {}".format(
            ", ".join(
                f"{state} {value}"
                for state, value in (payload.get("checker_counts") or {}).items()
            )
        ),
        f"расхождений с манифестом: {len(drift)}",
    ]
    lines.extend(f"  {entry}" for entry in drift)
    return "\n".join(lines)
