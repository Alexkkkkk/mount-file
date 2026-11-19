# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Truthfulness rules for ``docs/replay-acceptance.json``.

The #510 tracker is prose with a machine-readable mirror.  This module owns the
part of the contract a tree can actually prove: every declared status must be
consistent with the files it cites, and the manifest must be structurally
sound.  It never decides whether a filesystem feature works - that is
:mod:`mountfile.acceptance.blocks` - it only decides whether the tracker is
allowed to *claim* something.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Statuses the tracker may use.
STATUSES = ("verified", "partial", "not_started", "blocked")

#: Statuses that must cite at least one existing file.
NEEDS_EVIDENCE = ("verified", "partial", "blocked")

#: Statuses that must not cite evidence at all.
FORBIDS_EVIDENCE = ("not_started",)

#: Blocks defined by the #510 checklist.
BLOCKS = ("A", "B", "C", "D", "E")


class ManifestError(RuntimeError):
    """The manifest is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class ManifestItem:
    """One acceptance item as declared by the tracker."""

    ident: str
    block: str
    title: str
    status: str
    evidence: tuple[str, ...]
    note: str = ""
    reproduced: str = ""


@dataclass(frozen=True)
class Manifest:
    """A parsed tracker manifest."""

    schema: int
    slice: dict[str, Any]
    closure_rule: str
    items: tuple[ManifestItem, ...]

    def by_id(self, ident: str) -> ManifestItem:
        for item in self.items:
            if item.ident == ident:
                return item
        raise KeyError(ident)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.ident for item in self.items)

    def claimed_counts(self) -> dict[str, int]:
        counts = {name: 0 for name in STATUSES}
        for item in self.items:
            counts[item.status] += 1
        counts["total"] = len(self.items)
        return counts


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def parse_manifest(data: Any) -> Manifest:
    """Validate and convert a decoded manifest payload."""

    _require(isinstance(data, dict), "манифест должен быть JSON-объектом")
    schema = data.get("schema")
    _require(schema == 1, f"поддерживается только schema = 1, получено {schema!r}")

    slice_block = data.get("slice")
    _require(isinstance(slice_block, dict), "в манифесте нет секции slice")

    raw_items = data.get("items")
    _require(
        isinstance(raw_items, list) and raw_items, "items должен быть непустым списком"
    )

    seen: set[str] = set()
    items: list[ManifestItem] = []
    for index, raw in enumerate(raw_items):
        _require(isinstance(raw, dict), f"items[{index}] должен быть объектом")
        ident = raw.get("id")
        _require(isinstance(ident, str) and ident.strip(), f"items[{index}]: пустой id")
        _require(ident not in seen, f"повторяющийся id: {ident}")
        seen.add(ident)

        block = raw.get("block")
        _require(block in BLOCKS, f"{ident}: неизвестный блок {block!r}")
        _require(
            ident.startswith(str(block)), f"{ident}: id не совпадает с блоком {block}"
        )

        status = raw.get("status")
        _require(status in STATUSES, f"{ident}: неизвестный статус {status!r}")

        title = raw.get("title")
        _require(isinstance(title, str) and title.strip(), f"{ident}: пустой title")

        evidence = raw.get("evidence")
        _require(isinstance(evidence, list), f"{ident}: evidence должен быть списком")
        _require(
            all(isinstance(entry, str) and entry.strip() for entry in evidence),
            f"{ident}: evidence содержит нестроковое значение",
        )
        if status in NEEDS_EVIDENCE:
            _require(
                bool(evidence), f"{ident}: статус {status} требует хотя бы одного файла"
            )
        if status in FORBIDS_EVIDENCE:
            _require(
                not evidence, f"{ident}: статус {status} не может ссылаться на файлы"
            )

        reproduced = raw.get("reproduced")
        if status == "verified":
            _require(
                isinstance(reproduced, str) and reproduced.strip(),
                f"{ident}: статус verified требует поля reproduced",
            )

        items.append(
            ManifestItem(
                ident=ident,
                block=block,
                title=title,
                status=status,
                evidence=tuple(evidence),
                note=str(raw.get("note") or ""),
                reproduced=reproduced.strip() if isinstance(reproduced, str) else "",
            )
        )

    return Manifest(
        schema=schema,
        slice=dict(slice_block),
        closure_rule=str(data.get("closure_rule") or ""),
        items=tuple(items),
    )


def load_manifest(path: str | Path) -> Manifest:
    """Read ``path`` and return a validated manifest."""

    location = Path(path)
    try:
        text = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"манифест не читается: {location}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"некорректный JSON в {location}: {exc}") from exc
    return parse_manifest(data)


def missing_evidence(manifest: Manifest, root: str | Path) -> list[str]:
    """Return ``"ITEM: path"`` for every cited file that does not exist."""

    base = Path(root)
    problems: list[str] = []
    for item in manifest.items:
        for entry in item.evidence:
            if not (base / entry).exists():
                problems.append(f"{item.ident}: нет доказательства {entry}")
    return problems


def evidence_paths(manifest: Manifest) -> Iterable[str]:
    """Every distinct file cited by the manifest, in declaration order."""

    seen: set[str] = set()
    for item in manifest.items:
        for entry in item.evidence:
            if entry not in seen:
                seen.add(entry)
                yield entry
