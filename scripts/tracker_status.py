# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Generate the #510 status block from the manifest instead of editing it by hand.

The tracker thread is maintained manually and drifts from ``main``: counts,
evidence links and the "slice" line were repeatedly re-typed, and near-duplicate
comments had to be deleted afterwards. This script produces that block from
``docs/replay-acceptance.json`` so the only thing a human writes is the prose
that a generator cannot know.

    python scripts/tracker_status.py                     # human report
    python scripts/tracker_status.py --markdown          # ready-to-paste block
    python scripts/tracker_status.py --markdown --write docs/issue-510-status.md
    python scripts/tracker_status.py --json              # machine output
    python scripts/tracker_status.py --check             # exit 1 on drift
    python scripts/tracker_status.py --markdown \\
        --similarity last-comment.md                     # exit 3 if >90% same

``--similarity`` is the "did I already post this?" gate: point it at the last
comment on the issue (saved as a text/markdown file) and the script refuses to
hand back a block that repeats it.

Exit codes: ``0`` ok, ``1`` drift (``--check``), ``2`` manifest is malformed,
``3`` near-duplicate of the comparison file.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

MANIFEST_PATH = Path("docs/replay-acceptance.json")

STATUS_GLYPH = {
    "verified": "✅",
    "partial": "⚠️",
    "not_started": "⬜",
    "blocked": "🚫",
}

NEEDS_EVIDENCE = ("verified", "partial", "blocked")
FORBIDS_EVIDENCE = ("not_started",)

BLOCK_TITLES = {
    "A": "A. Общее и release-база",
    "B": "B. ext4 / JBD2 — полный transaction replay (#385)",
    "C": "C. NTFS / `$LogFile` — redo/undo replay (#386)",
    "D": "D. Общая acceptance-матрица (#387)",
    "E": "E. Паритет, платформы, статус",
}

#: A near-duplicate comment wastes a reviewer's time and hides the real delta.
SIMILARITY_LIMIT = 0.90


class ManifestError(RuntimeError):
    """The manifest itself is malformed (not merely out of date)."""


def load_manifest(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ManifestError(f"{path} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ManifestError(f"{path} has no 'items' list")
    return data


def git_slice(repo_root: Path, branch: str = "main") -> Dict[str, str]:
    """Current HEAD, so the block never cites a stale commit."""

    def run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    sha = run("rev-parse", "--short", "HEAD")
    subject = run("log", "-1", "--pretty=%s")
    ahead = (
        run("rev-list", "--count", f"origin/{branch}..HEAD")
        if run("rev-parse", "--verify", f"origin/{branch}")
        else ""
    )
    return {"branch": branch, "commit": sha, "subject": subject, "ahead": ahead}


def collect_drift(manifest: Dict[str, Any], repo_root: Path) -> List[str]:
    """Every mismatch a tree can prove: missing evidence, bad status, no id."""

    problems: List[str] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(manifest["items"]):
        if not isinstance(item, dict):
            problems.append(f"item #{index} is not an object")
            continue
        item_id = item.get("id")
        if not item_id:
            problems.append(f"item #{index} has no 'id'")
            continue
        seen[item_id] = seen.get(item_id, 0) + 1
        status = item.get("status")
        if status not in STATUS_GLYPH:
            problems.append(f"{item_id}: unknown status {status!r}")
            continue
        evidence = item.get("evidence") or []
        if not isinstance(evidence, list):
            problems.append(f"{item_id}: 'evidence' must be a list")
            continue
        if status in NEEDS_EVIDENCE and not evidence:
            problems.append(f"{item_id}: status {status} without evidence")
        if status in FORBIDS_EVIDENCE and evidence:
            problems.append(f"{item_id}: status {status} must not cite evidence")
        for relative in evidence:
            if not (repo_root / relative).exists():
                problems.append(f"{item_id}: evidence file missing: {relative}")
    for item_id, count in sorted(seen.items()):
        if count > 1:
            problems.append(f"{item_id}: declared {count} times")
    return problems


def counts_by_status(manifest: Dict[str, Any]) -> Dict[str, int]:
    counts = {status: 0 for status in STATUS_GLYPH}
    for item in manifest["items"]:
        status = item.get("status") if isinstance(item, dict) else None
        if status in counts:
            counts[status] += 1
    counts["total"] = len(manifest["items"])
    return counts


def render_markdown(
    manifest: Dict[str, Any],
    drift: Iterable[str],
    slice_info: Dict[str, str],
) -> str:
    counts = counts_by_status(manifest)
    declared = manifest.get("slice", {}) or {}
    commit = slice_info.get("commit") or declared.get("commit", "?")
    version = declared.get("version", "?")
    checked = declared.get("checked", "?")

    lines: List[str] = []
    lines.append("## Сводка трекера — сгенерировано `scripts/tracker_status.py`")
    lines.append("")
    lines.append(
        f"Срез манифеста: `{commit}` (v{version}), сверка `{checked}`. "
        f"HEAD: `{slice_info.get('commit') or '?'}` — "
        f"{slice_info.get('subject') or 'no git metadata'}."
    )
    if slice_info.get("ahead"):
        lines.append("")
        lines.append(
            f"⚠️ Ветка опережает `origin/{slice_info.get('branch')}` на "
            f"{slice_info['ahead']} коммит(ов): манифест может отставать."
        )
    lines.append("")
    lines.append(
        f"✅ {counts['verified']} · ⚠️ {counts['partial']} · "
        f"⬜ {counts['not_started']} · 🚫 {counts['blocked']} · "
        f"всего {counts['total']}"
    )
    lines.append("")
    lines.append(
        "> ✅ код и доказательство · ⚠️ код есть, независимое доказательство "
        "не воспроизведено · ⬜ не начато · 🚫 ждёт мержа или отказ по проекту"
    )
    lines.append("")

    by_block: Dict[str, List[Dict[str, Any]]] = {}
    for item in manifest["items"]:
        if isinstance(item, dict):
            by_block.setdefault(str(item.get("block", "?")), []).append(item)

    for block in sorted(by_block):
        lines.append(f"### {BLOCK_TITLES.get(block, block)}")
        lines.append("")
        lines.append("| № | Пункт | Состояние | Доказательство |")
        lines.append("|---|---|---|---|")
        for item in by_block[block]:
            glyph = STATUS_GLYPH.get(str(item.get("status")), "?")
            evidence = item.get("evidence") or []
            shown = ", ".join(f"`{e}`" for e in evidence) if evidence else "—"
            title = str(item.get("title", "")).replace("|", "\\|")
            lines.append(f"| {item.get('id')} | {title} | {glyph} | {shown} |")
        lines.append("")

    closure = manifest.get("closure_rule")
    if closure:
        lines.append(f"**Критерий закрытия:** {closure}")
        lines.append("")

    drift = list(drift)
    if drift:
        lines.append("### Расхождения (манифест ↔ дерево)")
        lines.append("")
        for problem in drift:
            lines.append(f"- {problem}")
        lines.append("")
    else:
        lines.append("Расхождений манифеста с деревом нет.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def similarity(left: str, right: str) -> float:
    """Ratio of two normalized texts; 1.0 means "byte-identical modulo spacing"."""

    return difflib.SequenceMatcher(None, normalize(left), normalize(right)).ratio()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the tree drifts"
    )
    parser.add_argument("--markdown", action="store_true", help="print the issue block")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--write",
        metavar="FILE",
        help="записать markdown-блок в файл (канонический текст для трекера)",
    )
    parser.add_argument(
        "--similarity",
        metavar="FILE",
        help="previous comment text; exit 3 when the new block repeats it",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=SIMILARITY_LIMIT,
        help=f"similarity limit for --similarity (default {SIMILARITY_LIMIT})",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    try:
        manifest = load_manifest(repo_root / args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    drift = collect_drift(manifest, repo_root)
    slice_info = git_slice(repo_root)
    markdown = render_markdown(manifest, drift, slice_info)
    counts = counts_by_status(manifest)

    if args.similarity:
        previous = Path(args.similarity)
        if not previous.is_file():
            print(f"error: {previous} does not exist", file=sys.stderr)
            return 2
        ratio = similarity(previous.read_text(encoding="utf-8"), markdown)
        if ratio >= args.threshold:
            print(
                f"posting suppressed: {ratio:.3f} similarity to {previous} "
                f"(limit {args.threshold:.2f}) — nothing new to report",
                file=sys.stderr,
            )
            return 3
        print(
            f"similarity to {previous}: {ratio:.3f} (limit {args.threshold:.2f})",
            file=sys.stderr,
        )

    if args.write:
        target = Path(args.write)
        if not target.is_absolute():
            target = repo_root / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "mountfile.tracker-status/v1",
                    "counts": counts,
                    "slice": slice_info,
                    "manifest_slice": manifest.get("slice", {}),
                    "drift": drift,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.markdown:
        print(markdown, end="")
    else:
        print(f"manifest: {repo_root / args.manifest}")
        print(
            f"HEAD:     {slice_info.get('commit') or '?'} {slice_info.get('subject') or ''}"
        )
        print(
            f"counts:   ✅ {counts['verified']}  ⚠️ {counts['partial']}  "
            f"⬜ {counts['not_started']}  🚫 {counts['blocked']}  "
            f"/ {counts['total']}"
        )
        if drift:
            print(f"drift:    {len(drift)}")
            for problem in drift:
                print(f"  - {problem}")
        else:
            print("drift:    none")
        print("hint:     --markdown for the issue block, --check for a gate")

    if args.check and drift:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
