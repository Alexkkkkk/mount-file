#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Issue #510: every open acceptance item must name its remaining gap.

``docs/replay-acceptance.json`` records what the tree actually proves.  It
deliberately keeps several items at ``partial`` and one at ``blocked``, and a
reader of the tracker cannot tell a *deliberate* partial from a forgotten one:
the manifest may say ``partial`` forever without anyone writing down what is
missing, what would promote the item, and which blocker issue owns it.

``docs/replay-acceptance-gaps.json`` closes that hole.  For every item whose
status is not ``verified`` it records

* ``blocker`` -- why the property cannot be claimed today: a fact about the
  code or the environment, never a plan or a promise;
* ``closes_when`` -- the observable result that would promote the item, stated
  so that a reviewer can decide "yes, that happened" without asking anybody;
* ``owner_issue`` -- the blocker/issue that must close first;
* ``evidence`` -- the files the gap text is talking about.

The two files are then checked against each other and against the tree::

    python scripts/replay_acceptance_gaps.py            # human report
    python scripts/replay_acceptance_gaps.py --check    # exit 1 on drift
    python scripts/replay_acceptance_gaps.py --json     # machine output
    python scripts/replay_acceptance_gaps.py --markdown # tracker block

The checks run in both directions on purpose.  A missing entry fails, because a
silent ``partial`` is exactly the drift this file exists to prevent.  An entry
for a ``verified`` item fails too, because a stale waiver would silently cover
the same regression if it came back.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "replay-acceptance.json"
DEFAULT_GAPS = REPO_ROOT / "docs" / "replay-acceptance-gaps.json"

#: Statuses that owe the reader an explanation.
OPEN_STATUSES = frozenset({"partial", "not_started", "blocked"})

#: A one-word reason is not a reason.  Both prose fields must be specific
#: enough that a reviewer can tell what is missing without reading the code.
MIN_REASON_CHARS = 24


@dataclass(frozen=True)
class Problem:
    """One disagreement between the manifest, the gap file and the tree."""

    kind: str
    ident: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "id": self.ident, "message": self.message}


def _load(path: Path, label: str) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{label}: файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label}: некорректный JSON в {path}: {exc}") from exc


def load_manifest(path: Path = DEFAULT_MANIFEST) -> List[Dict[str, Any]]:
    document = _load(path, "manifest")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(f"manifest: {path} не содержит непустого списка items")
    return items


def load_gaps(path: Path = DEFAULT_GAPS) -> List[Dict[str, Any]]:
    document = _load(path, "gaps")
    gaps = document.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        raise SystemExit(f"gaps: {path} не содержит непустого списка gaps")
    return gaps


def _existing_evidence(root: Path, evidence: Iterable[str]) -> List[str]:
    return [entry for entry in evidence if not (root / entry).exists()]


def evaluate(
    items: Sequence[Dict[str, Any]],
    gaps: Sequence[Dict[str, Any]],
    root: Path = REPO_ROOT,
) -> List[Problem]:
    """Return every disagreement; an empty list means the contract holds."""

    problems: List[Problem] = []
    by_item = {item["id"]: item for item in items}
    seen: Dict[str, int] = {}

    for gap in gaps:
        ident = str(gap.get("id", "<без id>"))
        seen[ident] = seen.get(ident, 0) + 1
        if ident not in by_item:
            problems.append(
                Problem("unknown_item", ident, "пункта нет в docs/replay-acceptance.json")
            )
            continue
        item = by_item[ident]
        status = item.get("status")
        if status not in OPEN_STATUSES:
            problems.append(
                Problem(
                    "stale_gap",
                    ident,
                    f"пункт уже '{status}', запись о пробеле надо удалить",
                )
            )
        declared = gap.get("status")
        if declared != status:
            problems.append(
                Problem(
                    "status_mismatch",
                    ident,
                    f"gaps: '{declared}', manifest: '{status}'",
                )
            )
        for field in ("blocker", "closes_when"):
            text = gap.get(field)
            if not isinstance(text, str) or len(text.strip()) < MIN_REASON_CHARS:
                problems.append(
                    Problem(
                        "vague_reason",
                        ident,
                        f"поле {field} пустое или короче {MIN_REASON_CHARS} символов",
                    )
                )
        owner = gap.get("owner_issue")
        if not isinstance(owner, int):
            problems.append(
                Problem("vague_reason", ident, "owner_issue должен быть номером issue")
            )
        evidence = gap.get("evidence") or []
        if not evidence:
            problems.append(
                Problem("no_evidence", ident, "не указаны файлы, к которым относится пробел")
            )
        missing = _existing_evidence(root, evidence)
        for entry in missing:
            problems.append(
                Problem("missing_evidence", ident, f"файла нет в дереве: {entry}")
            )

    duplicates = sorted(ident for ident, count in seen.items() if count > 1)
    for ident in duplicates:
        problems.append(Problem("duplicate", ident, "пункт описан более одного раза"))

    for item in items:
        ident = item["id"]
        if item.get("status") in OPEN_STATUSES and ident not in seen:
            problems.append(
                Problem(
                    "missing_gap",
                    ident,
                    "статус не verified, но пробел не описан в replay-acceptance-gaps.json",
                )
            )

    return problems


def summarise(items: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        status = str(item.get("status"))
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(items)
    return counts


def render_markdown(
    items: Sequence[Dict[str, Any]], gaps: Sequence[Dict[str, Any]]
) -> str:
    """Render the tracker block: every open item with its stated gap."""

    order = {item["id"]: index for index, item in enumerate(items)}
    ordered = sorted(
        (gap for gap in gaps if gap.get("id") in order),
        key=lambda gap: order[gap["id"]],
    )
    lines = [
        "### Открытые пункты и их пробелы",
        "",
        "_Таблица генерируется `python scripts/replay_acceptance_gaps.py --markdown` "
        "из `docs/replay-acceptance-gaps.json`; гейт падает, если у открытого пункта "
        "нет описанного пробела._",
        "",
        "| № | Состояние | Блокер | Закрывается, когда | Issue |",
        "|---|---|---|---|---|",
    ]
    for gap in ordered:
        blocker = str(gap.get("blocker", "")).replace("|", "\\|")
        closes = str(gap.get("closes_when", "")).replace("|", "\\|")
        lines.append(
            f"| {gap['id']} | {gap.get('status')} | {blocker} | {closes} "
            f"| #{gap.get('owner_issue')} |"
        )
    return "\n".join(lines) + "\n"


def _print_report(
    items: Sequence[Dict[str, Any]],
    gaps: Sequence[Dict[str, Any]],
    problems: Sequence[Problem],
) -> None:
    counts = summarise(items)
    print(
        "gaps #510 | {total} пунктов, открытых с пробелом {open}, "
        "verified {verified}".format(
            total=counts.get("total", 0),
            open=len(gaps),
            verified=counts.get("verified", 0),
        )
    )
    if not problems:
        print("расхождений нет: у каждого открытого пункта описан пробел")
        return
    print(f"расхождений: {len(problems)}")
    for problem in problems:
        print(f"  [{problem.kind}] {problem.ident}: {problem.message}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сверить открытые пункты #510 с описанными пробелами."
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--gaps", default=str(DEFAULT_GAPS))
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--check", action="store_true", help="exit 1 при расхождении")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--markdown", action="store_true", help="блок для трекера")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    gaps_path = Path(args.gaps)
    root = Path(args.root)
    items = load_manifest(manifest_path)
    gaps = load_gaps(gaps_path)
    problems = evaluate(items, gaps, root)

    if args.json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "tracker": 510,
                    "manifest": str(manifest_path),
                    "gaps": str(gaps_path),
                    "counts": summarise(items),
                    "open_items": len(gaps),
                    "problems": [problem.as_dict() for problem in problems],
                    "verdict": "pass" if not problems else "drift",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    elif args.markdown:
        print(render_markdown(items, gaps), end="")
    else:
        _print_report(items, gaps, problems)

    if args.check and problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
