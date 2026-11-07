#!/usr/bin/env python3
"""Generate the repository's canonical machine-readable status artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.capabilities import get_native_capability_matrix
from scripts.checker_matrix import build_report as build_checker_report
from scripts.product_status_contract import build_report as build_product_report
from scripts.recovery_matrix import build_report as build_recovery_report


def build_status(root: Path) -> dict[str, dict]:
    return {
        "capability-matrix.json": get_native_capability_matrix(),
        "checker-matrix.json": build_checker_report(root / "tests"),
        "recovery-matrix.json": build_recovery_report(),
        "product-status.json": build_product_report(root),
    }


def sync(root: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, report in build_status(root).items():
        path = output_dir / name
        encoded = (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if not path.exists() or path.read_text(encoding="utf-8") != encoded:
            path.write_text(encoded, encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("docs") / "generated")
    args = parser.parse_args()
    for path in sync(args.repo_root.resolve(), args.output_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
