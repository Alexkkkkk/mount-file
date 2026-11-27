# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Verify the replay acceptance manifest against the repository tree.

The tracker in #510 was maintained by hand and drifted from ``main``: a test
listed as missing had already been merged, a parser listed as absent existed,
and the "open PR" line pointed at a merged PR.  This script makes the drift
mechanical instead of editorial:

    python scripts/replay_acceptance_status.py            # human report
    python scripts/replay_acceptance_status.py --check     # exit 1 on drift
    python scripts/replay_acceptance_status.py --markdown  # tables for the issue
    python scripts/replay_acceptance_status.py --json      # machine output

``docs/replay-acceptance.json`` is the single source of truth.  Every item
declares the files that back it; an item cannot stay "verified" or "partial"
once one of those files disappears, and a "not_started" item must not cite
evidence it does not have.  That is the whole contract - it only checks what a
tree can prove, and never claims an independent reproduction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Manifest path relative to the repository root.
MANIFEST_PATH = Path("docs/replay-acceptance.json")

#: Statuses the tracker may use, with the glyph rendered in Markdown.
STATUS_GLYPH = {
    "verified": "✅",
    "partial": "⚠️",
    "not_started": "⬜",
    "blocked": "🚫",
}

#: Statuses that must cite at least one existing file.
NEEDS_EVIDENCE = ("verified", "partial", "blocked")

#: Statuses that must not cite evidence at all.
FORBIDS_EVIDENCE = ("not_started",)

#: Block titles rendered in Markdown output.
BLOCK_TITLES = {
    "A": "A. Общее и release-база",
    "B": "B. ext4 / JBD2 — полный transaction replay (#385)",
    "C": "C. NTFS / `$LogFile` — redo/undo replay (#386)",
    "D": "D. Общая acceptance-матрица (#387)",
    "E": "E. Паритет, платформы, статус",
}


class ManifestError(RuntimeError):
    """The manifest itself is malformed (not merely out of date)."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Read and structurally validate the manifest.  Raises ManifestError."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("не удалось прочитать %s: %s" % (path, exc)) from exc
    except ValueError as exc:
        raise ManifestError("некорректный JSON в %s: %s" % (path, exc)) from exc

    if not isinstance(data, dict):
        raise ManifestError("манифест должен быть объектом")
    if int(data.get("schema") or 0) != 1:
        raise ManifestError("поддерживается только schema = 1")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ManifestError("поле items должно быть непустым списком")

    seen: set = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ManifestError("items[%d] должен быть объектом" % index)
        ident = item.get("id")
        if not isinstance(ident, str) or not ident.strip():
            raise ManifestError("items[%d]: пустой id" % index)
        if ident in seen:
            raise ManifestError("повторяющийся id: %s" % ident)
        seen.add(ident)
        if item.get("status") not in STATUS_GLYPH:
            raise ManifestError(
                "%s: неизвестный статус %r" % (ident, item.get("status"))
            )
        if item.get("block") not in BLOCK_TITLES:
            raise ManifestError("%s: неизвестный блок %r" % (ident, item.get("block")))
        if not isinstance(item.get("evidence"), list):
            raise ManifestError("%s: evidence должен быть списком" % ident)
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ManifestError("%s: пустой title" % ident)
    return data


def verify(manifest: dict[str, Any], root: Path) -> list[str]:
    """Return a list of problems; an empty list means the manifest is truthful."""

    problems: list[str] = []
    root = Path(root)
    for item in manifest["items"]:
        ident = item["id"]
        status = item["status"]
        evidence = [str(entry) for entry in item.get("evidence") or ()]

        missing = [entry for entry in evidence if not (root / entry).exists()]
        for entry in missing:
            problems.append("%s: нет доказательства %s" % (ident, entry))

        if status in NEEDS_EVIDENCE and not evidence:
            problems.append(
                "%s: статус %s требует хотя бы одного файла" % (ident, status)
            )
        if status in FORBIDS_EVIDENCE and evidence:
            problems.append(
                "%s: статус %s не может ссылаться на доказательства (%s)"
                % (ident, status, ", ".join(evidence))
            )

        if status == "verified" and not item.get("reproduced"):
            problems.append(
                "%s: статус verified требует поля reproduced (дата и способ проверки)"
                % ident
            )
    return problems


def summarise(manifest: dict[str, Any]) -> dict[str, int]:
    """Count items per status, plus the total."""

    counts = {name: 0 for name in STATUS_GLYPH}
    for item in manifest["items"]:
        counts[item["status"]] += 1
    counts["total"] = len(manifest["items"])
    return counts


def _ordered_blocks(items: Iterable[dict[str, Any]]) -> list[str]:
    present = {item["block"] for item in items}
    return [name for name in BLOCK_TITLES if name in present]


def render_markdown(manifest: dict[str, Any]) -> str:
    """Render the whole tracker as Markdown tables ready to paste into #510."""

    items = manifest["items"]
    counts = summarise(manifest)
    slice_info = manifest.get("slice") or {}
    lines = [
        "**Срез:** `%s` @ `%s` (v%s). Последняя сверка: %s."
        % (
            slice_info.get("branch", "main"),
            slice_info.get("commit", "?"),
            slice_info.get("version", "?"),
            slice_info.get("checked", "?"),
        ),
        "",
        "✅ проверено — **%d** · ⚠️ частично — **%d** · ⬜ не начато — **%d** · "
        "🚫 отказ/ожидание — **%d** · всего пунктов — **%d**"
        % (
            counts["verified"],
            counts["partial"],
            counts["not_started"],
            counts["blocked"],
            counts["total"],
        ),
        "",
        "_Таблица генерируется `python scripts/replay_acceptance_status.py --markdown` "
        "из `docs/replay-acceptance.json`; CI падает, если доказательство исчезло._",
        "",
    ]
    for block in _ordered_blocks(items):
        lines.append("### %s" % BLOCK_TITLES[block])
        lines.append("")
        lines.append("| № | Пункт | Состояние | Доказательство |")
        lines.append("|---|---|---|---|")
        for item in items:
            if item["block"] != block:
                continue
            evidence = item.get("evidence") or []
            shown = ", ".join("`%s`" % entry for entry in evidence) or "—"
            note = (item.get("note") or "").strip()
            cell = (
                "%s %s" % (STATUS_GLYPH[item["status"]], note)
                if note
                else STATUS_GLYPH[item["status"]]
            )
            lines.append(
                "| %s | %s | %s | %s |" % (item["id"], item["title"], cell, shown)
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_text(manifest: dict[str, Any], problems: list[str]) -> str:
    """Compact terminal report."""

    counts = summarise(manifest)
    slice_info = manifest.get("slice") or {}
    out = [
        "tracker #%s | %s @ %s (v%s) | сверка %s"
        % (
            manifest.get("tracker", "?"),
            slice_info.get("branch", "main"),
            slice_info.get("commit", "?"),
            slice_info.get("version", "?"),
            slice_info.get("checked", "?"),
        ),
        "verified %d | partial %d | not_started %d | blocked %d | total %d"
        % (
            counts["verified"],
            counts["partial"],
            counts["not_started"],
            counts["blocked"],
            counts["total"],
        ),
    ]
    if problems:
        out.append("")
        out.append("РАСХОЖДЕНИЯ (%d):" % len(problems))
        out.extend("  - %s" % problem for problem in problems)
    else:
        out.append("расхождений нет: каждое доказательство на месте")
    return "\n".join(out)


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the manifest drifted"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--markdown", action="store_true", help="render tables for the issue body"
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(Path(args.manifest))
    except ManifestError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    problems = verify(manifest, Path(args.root))

    if args.markdown:
        print(render_markdown(manifest), end="")
    elif args.json:
        payload = {
            "slice": manifest.get("slice"),
            "counts": summarise(manifest),
            "problems": problems,
            "items": manifest["items"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(manifest, problems))

    if problems and (args.check or not (args.markdown or args.json)):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
