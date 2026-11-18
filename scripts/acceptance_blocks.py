#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Run the #510 acceptance probes and emit the combined artifact.

    python scripts/acceptance_blocks.py              # human summary
    python scripts/acceptance_blocks.py --write      # write docs/generated/acceptance-blocks.json
    python scripts/acceptance_blocks.py --check      # exit 1 when a probe contradicts the manifest
    python scripts/acceptance_blocks.py --json       # machine output

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

    if args.check and payload["drift"]:
        for entry in payload["drift"]:
            print(f"расхождение: {entry}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
