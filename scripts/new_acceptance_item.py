# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Scaffold one acceptance item: a test skeleton plus its manifest entry.

Every tracker item in #510 is the same shape — a test file, a status, and a list
of files that back the claim. Re-typing the previous item by hand is how ids get
declared twice and how "verified" ends up citing a file nobody created. This
generator refuses those cases up front.

    python scripts/new_acceptance_item.py --list-blocks
    python scripts/new_acceptance_item.py --id C6 --block C \\
        --title "NTFS: diff classifier" --test-tests tests/test_ntfs_diff.py --dry-run
    python scripts/new_acceptance_item.py --id C6 --block C \\
        --title "NTFS: diff classifier" --test-tests tests/test_ntfs_diff.py

The generated test is ``skip``-marked on purpose: an item is not evidence until a
human writes the assertion, and a silently passing placeholder would be worse
than a skip. Pass ``--status not_started`` (default) when only the skeleton is
wanted, or ``--existing-test`` when the test already exists and only the manifest
entry is missing.

Exit codes: ``0`` ok, ``1`` refused (duplicate id, unknown block, missing
evidence), ``2`` the manifest itself is malformed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path("docs/replay-acceptance.json")

BLOCK_TITLES = {
    "A": "Общее и release-база",
    "B": "ext4 / JBD2 — полный transaction replay (#385)",
    "C": "NTFS / $LogFile — redo/undo replay (#386)",
    "D": "Общая acceptance-матрица (#387)",
    "E": "Паритет, платформы, статус",
}

STATUSES = ("verified", "partial", "not_started", "blocked")

#: Statuses that must cite at least one existing file.
NEEDS_EVIDENCE = ("verified", "partial", "blocked")

TEST_TEMPLATE = '''# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Acceptance item {item_id} ({block}): {title}

Tracker: https://github.com/Alexkkkkk/mount-file/issues/{issue}

This file is a skeleton produced by ``scripts/new_acceptance_item.py``. It is
skipped until it asserts something real: an item is evidence only when the test
can fail. Do not flip the manifest status before that.
"""

import pytest

pytestmark = pytest.mark.skip(reason="acceptance item {item_id}: assertion not written yet")


def test_{slug}_pending():
    """Replace with the assertion that proves {item_id}."""
    ...
'''


class Refused(RuntimeError):
    """The request would produce a manifest that lies about the tree."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return slug or "item"


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Refused(f"{path} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise Refused(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise Refused(f"{path} has no 'items' list")
    return data


def validate(manifest: dict[str, Any], repo_root: Path) -> list[str]:
    problems: list[str] = []
    for item in manifest["items"]:
        if not isinstance(item, dict):
            problems.append("an item is not an object")
            continue
        status = item.get("status")
        if status not in STATUSES:
            problems.append(f"{item.get('id')}: unknown status {status!r}")
            continue
        evidence = item.get("evidence") or []
        if status in NEEDS_EVIDENCE:
            if not evidence:
                problems.append(f"{item.get('id')}: status {status} without evidence")
            for relative in evidence:
                if not (repo_root / relative).exists():
                    problems.append(
                        f"{item.get('id')}: missing evidence file {relative}"
                    )
        elif evidence:
            problems.append(f"{item.get('id')}: status {status} must not cite evidence")
    return problems


def build_item(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
    item_id = args.id.strip()
    if not re.fullmatch(r"[A-Z]\d{1,3}", item_id):
        raise Refused(
            f"--id must look like 'C6' (block letter + number), got {item_id!r}"
        )
    if args.block not in BLOCK_TITLES:
        raise Refused(f"--block must be one of {', '.join(sorted(BLOCK_TITLES))}")
    if item_id[0] != args.block:
        raise Refused(f"--id {item_id} does not match --block {args.block}")
    if not args.title.strip():
        raise Refused("--title is required")

    evidence = [*args.test_tests]
    if args.status in NEEDS_EVIDENCE:
        for relative in evidence:
            if not (repo_root / relative).exists():
                raise Refused(
                    f"evidence file does not exist: {relative} (create it first)"
                )

    return {
        "id": item_id,
        "block": args.block,
        "title": args.title.strip(),
        "status": args.status,
        "evidence": evidence,
        "reproduced": args.reproduced or "",
        "note": args.note or "",
    }


def write_test(path: Path, item: dict[str, Any], issue: int) -> None:
    if path.exists():
        raise Refused(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        TEST_TEMPLATE.format(
            item_id=item["id"],
            block=item["block"],
            title=item["title"],
            issue=issue,
            slug=slugify(item["id"]),
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--issue", type=int, default=510)
    parser.add_argument("--id", dest="id")
    parser.add_argument("--block", choices=sorted(BLOCK_TITLES))
    parser.add_argument("--title")
    parser.add_argument("--status", choices=STATUSES, default="not_started")
    parser.add_argument(
        "--test-tests",
        action="append",
        default=[],
        metavar="PATH",
        help="evidence file (repeatable); created as a skeleton when it does not exist",
    )
    parser.add_argument("--reproduced", help="free-text proof line for the manifest")
    parser.add_argument("--note")
    parser.add_argument(
        "--existing-test", action="store_true", help="evidence already exists"
    )
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--list-blocks", action="store_true")
    args = parser.parse_args(argv)

    if args.list_blocks:
        for block, title in BLOCK_TITLES.items():
            print(f"{block} — {title}")
        return 0

    repo_root = Path(args.repo_root).resolve()
    if not args.id or not args.block or not args.title:
        parser.error("--id, --block and --title are required")

    try:
        manifest = load_manifest(repo_root / args.manifest)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    existing = [item for item in manifest["items"] if isinstance(item, dict)]
    if any(item.get("id") == args.id for item in existing):
        print(
            f"refused: item {args.id} is already declared in {args.manifest}",
            file=sys.stderr,
        )
        return 1

    # A skeleton test cannot back a "verified" claim; force the honest status.
    # With --existing-test the caller asserts the file is already there, so a
    # missing file is a refusal from build_item() instead of a silent downgrade.
    new_tests = (
        []
        if args.existing_test
        else [p for p in args.test_tests if not (repo_root / p).exists()]
    )
    if new_tests and args.status in NEEDS_EVIDENCE:
        args.status = "not_started"
        print(
            f"note: {', '.join(new_tests)} will be created as a skipped skeleton, "
            "so the status is downgraded to not_started",
            file=sys.stderr,
        )

    try:
        item = build_item(args, repo_root)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    candidate = dict(manifest)
    candidate["items"] = [*existing, item]
    problems = validate(candidate, repo_root)
    # Only the new item's own missing files are expected at this point.
    problems = [
        p for p in problems if not p.startswith(f"{item['id']}: missing evidence file")
    ]

    if args.dry_run:
        print(json.dumps(item, indent=2, ensure_ascii=False))
        for relative in new_tests:
            print(f"would create: {relative}")
        print(f"would insert into: {args.manifest}")
        return 0

    for relative in new_tests:
        try:
            write_test(repo_root / relative, item, args.issue)
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"created: {relative}")

    candidate_manifest = json.loads(
        (repo_root / args.manifest).read_text(encoding="utf-8")
    )
    candidate_manifest["items"].append(item)
    (repo_root / args.manifest).write_text(
        json.dumps(candidate_manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"inserted {item['id']} into {args.manifest} (status: {item['status']})")

    if problems:
        print("warning: manifest still reports:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    print("next: write the assertion, then run scripts/tracker_status.py --check")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
