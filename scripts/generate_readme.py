#!/usr/bin/env python3
"""Update only the generated status block in README.md."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

START = "<!-- MOUNTFILE-AUTO-STATUS:START -->"
END = "<!-- MOUNTFILE-AUTO-STATUS:END -->"


def _version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _support_rows(root: Path) -> list[str]:
    text = (root / "mountfile" / "capabilities.py").read_text(encoding="utf-8")
    rows = []
    for profile, filesystem in re.findall(
        r'profile_id\s*=\s*["\']([^"\']+)["\'].*?filesystem\s*=\s*["\']([^"\']+)["\']',
        text,
        re.DOTALL,
    ):
        rows.append(f"`{profile}` ({filesystem})")
    return rows[:12]


def render(root: Path, revision: str) -> str:
    features = [
        ("checker acceptance", root / "scripts" / "checker_acceptance.py"),
        ("recovery acceptance", root / "scripts" / "recovery_acceptance.py"),
        ("benchmark artifact validation", root / "scripts" / "benchmark_artifact.py"),
    ]
    enabled = [name for name, path in features if path.exists()]
    rows = _support_rows(root)
    rows_text = ", ".join(rows) if rows else "capability matrix available"
    features_text = ", ".join(enabled) if enabled else "core status contract"
    return "\n".join(
        [
            START,
            "## Generated project status",
            "",
            f"This block is synchronized automatically on every internal pull request. "
            f"Source revision: `{revision}`; package version: `{_version(root)}`.",
            "",
            f"Validated automation: {features_text}.",
            "",
            "Canonical status data: `docs/generated/capability-matrix.json`, "
            "`checker-matrix.json`, `recovery-matrix.json` and `product-status.json`.",
            "",
            f"Capability profiles currently declared: {rows_text}.",
            "",
            "This generated summary is informational: unsupported, dirty, ambiguous "
            "and recovery-required profiles remain fail-closed; benchmark and fixture "
            "evidence do not claim full filesystem parity or power-loss safety.",
            END,
        ]
    )


def update(readme: str, block: str) -> str:
    pattern = re.compile(rf"{re.escape(START)}.*?{re.escape(END)}", re.DOTALL)
    if pattern.search(readme):
        return pattern.sub(block, readme, count=1)
    anchor = "## What is supported today"
    if anchor not in readme:
        raise ValueError(f"README anchor not found: {anchor}")
    return readme.replace(anchor, block + "\n\n" + anchor, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision", default="working-tree")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    readme_path = root / "README.md"
    result = update(
        readme_path.read_text(encoding="utf-8"), render(root, args.revision)
    )
    destination = args.output or readme_path
    destination.write_text(result, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
