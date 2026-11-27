#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Run the #510 acceptance probes and emit the combined artifact.

    python scripts/acceptance_blocks.py              # human summary
    python scripts/acceptance_blocks.py --write      # write docs/generated/acceptance-blocks.json
    python scripts/acceptance_blocks.py --check      # exit 1 when a probe contradicts the manifest
    python scripts/acceptance_blocks.py --check --baseline docs/acceptance-known-drift.txt
    python scripts/acceptance_blocks.py --json       # machine output

``--baseline`` separates the drift that is already known and explained from
drift introduced by the change under review.  A baselined id that stops
drifting is a failure too: the entry has to be deleted, otherwise it would
silently tolerate the same regression if it came back.

The probes execute production code (journal gate, publication primitive,
checksum helpers, refusal paths) and report what they observed.  A status of
``not_exercised`` or ``unavailable`` is a valid, honest answer; only ``verified``
backs a claim, and ``--check`` fails on drift between the manifest and reality.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mountfile.acceptance import artifact  # noqa: E402


def load_baseline(path: Path) -> dict[str, str]:
    """Read ``<item id>  # <reason>`` lines, ignoring blanks and comments."""

    entries: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, reason = line.partition("#")
        ident = ident.strip()
        if not ident:
            raise ValueError(f"{path}:{lineno}: пустой идентификатор")
        if ident in entries:
            raise ValueError(f"{path}:{lineno}: дубликат {ident}")
        entries[ident] = reason.strip()
    return entries


def split_drift(
    drift: list[str], baseline: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(known, new, stale)`` drift entries for ``baseline``."""

    known: list[str] = []
    new: list[str] = []
    seen = set()
    for entry in drift:
        ident = entry.split(":", 1)[0].strip()
        seen.add(ident)
        (known if ident in baseline else new).append(entry)
    stale = sorted(ident for ident in baseline if ident not in seen)
    return known, new, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the #510 acceptance blocks.")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    parser.add_argument(
        "--write", action="store_true", help="write the generated artifact"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 on manifest drift")
    parser.add_argument(
        "--json", action="store_true", help="print the artifact as JSON"
    )
    parser.add_argument("--output", default=None, help="artifact path override")
    parser.add_argument(
        "--baseline",
        default=None,
        help="файл известных расхождений; --check падает только на новых",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    manifest = root / "docs" / "replay-acceptance.json"

    if args.write:
        target = artifact.write_artifact(root, args.output)
        print(
            f"записано: {target.relative_to(root) if target.is_relative_to(root) else target}"
        )

    payload = artifact.build_artifact(root, manifest if manifest.is_file() else None)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(artifact.summarise_artifact(payload))

    if not args.check or not manifest.is_file():
        return 0

    if args.baseline is None:
        if payload["drift"]:
            for entry in payload["drift"]:
                print(f"расхождение: {entry}", file=sys.stderr)
            return 1
        return 0

    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path
    try:
        baseline = load_baseline(baseline_path)
    except (OSError, ValueError) as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    known, new, stale = split_drift(payload["drift"], baseline)
    for entry in known:
        ident = entry.split(":", 1)[0].strip()
        print(f"известное расхождение: {entry} ({baseline[ident]})")
    for entry in new:
        print(f"НОВОЕ расхождение: {entry}", file=sys.stderr)
    for ident in stale:
        print(
            f"устаревшая запись baseline: {ident} больше не расходится - удалите её",
            file=sys.stderr,
        )
    return 1 if (new or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
