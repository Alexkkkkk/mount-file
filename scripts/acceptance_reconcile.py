#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Reconcile the #510 manifest claims against what the probes observed.

Two gates already exist for tracker #510 and neither one can see the other:

* ``scripts/replay_acceptance_status.py --check`` validates the manifest
  against the tree (does a cited file still exist);
* ``scripts/acceptance_blocks.py --check`` validates the probe artifact
  against the manifest *counts*.

Both of them compare a claim only with itself, so a manifest that says
``verified 23`` while the probes observed ``verified 15`` passes both.  This
script closes that specific gap: it joins the two views **per item id** and
fails when a claim is stronger than the evidence behind it.

    python scripts/acceptance_reconcile.py            # human report
    python scripts/acceptance_reconcile.py --check    # exit 1 on a contradiction
    python scripts/acceptance_reconcile.py --json     # machine output

Category mismatch, not a false claim: every probe in the ``structure`` tier
can only prove that a file exists, never that the property holds (see
``BlockCheck.run`` in ``mountfile/acceptance/blocks.py``), so ``partial`` is the
strongest thing such a probe can report.  Those ids are waived in
``docs/acceptance-claim-probe-drift.txt`` with a written reason each - a
separate file from ``docs/acceptance-known-drift.txt``, which waives *count*
drift for ``scripts/acceptance_blocks.py`` and must stay empty while the
counters agree.  Rules, identical to the other gate: a listed id must be a
structure probe, an id that stops drifting fails as stale, and anything
unlisted is a real contradiction.

    python scripts/acceptance_reconcile.py --baseline docs/acceptance-claim-probe-drift.txt

Exit codes: ``0`` no contradiction, ``1`` contradiction or stale baseline
entry, ``2`` the manifest or the probes could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MANIFEST_PATH = Path("docs/replay-acceptance.json")
BASELINE_PATH = Path("docs/acceptance-claim-probe-drift.txt")

#: Ordered strength of the statuses used by the manifest and by the probes.
#: ``not_exercised`` is the probe's word for "the code exists, nothing was run",
#: which is no stronger than ``not_started``.
STRENGTH: dict[str, int] = {
    "unavailable": 0,
    "not_started": 0,
    "not_exercised": 0,
    "blocked": 1,
    "partial": 2,
    "verified": 3,
}

#: Statuses a claim is allowed to use.
CLAIM_STATUSES = ("verified", "partial", "not_started", "blocked")

EXIT_CONFIG_ERROR = 2


class ReconcileError(RuntimeError):
    """The manifest or the probe artifact cannot be read."""


def strength(status: str) -> int:
    """Return the comparable strength of a status, -1 when unknown."""
    return STRENGTH.get(status, -1)


def reconcile(
    claims: dict[str, str], observed: dict[str, str]
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]], list[str]]:
    """Join the two views per id.

    Returns ``(contradictions, understated, unprobed)``:

    * ``contradictions`` - the claim is stronger than the probe observed;
    * ``understated``    - the probe is stronger than the claim, which is a
      stale manifest rather than a false claim;
    * ``unprobed``       - a claimed id the probes never returned.
    """
    contradictions: list[tuple[str, str, str]] = []
    understated: list[tuple[str, str, str]] = []
    unprobed: list[str] = []

    for ident in sorted(claims, key=_item_sort_key):
        claim = claims[ident]
        if ident not in observed:
            unprobed.append(ident)
            continue
        probe = observed[ident]
        if strength(claim) > strength(probe):
            contradictions.append((ident, claim, probe))
        elif strength(claim) < strength(probe):
            understated.append((ident, claim, probe))

    return contradictions, understated, unprobed


def load_baseline(path: Path) -> dict[str, str]:
    """Read ``<item id>  # <reason>`` lines - the same format as the gap gate."""
    from scripts.acceptance_blocks import load_baseline as _load

    return _load(path)


def _item_sort_key(ident: str) -> tuple[str, int]:
    """``A1, A2, ..., B10`` instead of ``A1, A10, A2``."""
    head = ident.rstrip("0123456789")
    tail = ident[len(head) :]
    return head, int(tail) if tail.isdigit() else 0


def load_claims(manifest_path: Path) -> dict[str, str]:
    """Read ``{id: status}`` from the manifest, raising ``ReconcileError``."""
    from mountfile.acceptance import contract as _contract

    if not manifest_path.is_file():
        raise ReconcileError(f"манифест не найден: {manifest_path}")
    try:
        manifest = _contract.load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 - the loader raises its own type
        raise ReconcileError(f"манифест не читается: {exc}") from exc
    raw_items = manifest.items
    items: Iterable[Any] = raw_items() if callable(raw_items) else raw_items
    claims: dict[str, str] = {}
    for item in items:
        ident = getattr(item, "ident", None)
        status = getattr(item, "status", None)
        if not ident:
            raise ReconcileError("у пункта манифеста нет id")
        if status not in CLAIM_STATUSES:
            raise ReconcileError(f"{ident}: неизвестный статус {status!r}")
        claims[str(ident)] = str(status)
    return claims


def load_observed(root: Path, manifest_path: Path) -> dict[str, str]:
    """Run the probes and return ``{id: observed status}``."""
    try:
        from mountfile.acceptance.blocks import run_all
    except Exception as exc:  # noqa: BLE001
        raise ReconcileError(f"пробы недоступны: {exc}") from exc
    try:
        result = run_all(root, manifest_path if manifest_path.is_file() else None)
    except Exception as exc:  # noqa: BLE001
        raise ReconcileError(f"пробы упали: {exc}") from exc
    observed: dict[str, str] = {}
    for row in result.get("items") or []:
        ident = row.get("id")
        if ident:
            observed[str(ident)] = str(row.get("status") or "")
    return observed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Сверить утверждения манифеста #510 с наблюдениями проб."
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument(
        "--check", action="store_true", help="exit 1, если утверждение сильнее пробы"
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=f"файл известных расхождений категории (по умолчанию {BASELINE_PATH})",
    )
    parser.add_argument("--json", action="store_true", help="machine output")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (root / manifest_path).resolve()

    baseline: dict[str, str] = {}
    baseline_path = Path(args.baseline) if args.baseline else root / BASELINE_PATH
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path
    if baseline_path.is_file():
        try:
            baseline = load_baseline(baseline_path)
        except (OSError, ValueError) as exc:
            print(f"ошибка конфигурации: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    try:
        claims = load_claims(manifest_path)
        observed = load_observed(root, manifest_path)
    except ReconcileError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    contradictions, understated, unprobed = reconcile(claims, observed)

    known: list[tuple[str, str, str]] = []
    novel = list(contradictions)
    stale: list[str] = []
    if baseline is not None:
        known = [c for c in contradictions if c[0] in baseline]
        novel = [c for c in contradictions if c[0] not in baseline]
        drifted = {c[0] for c in contradictions}
        stale = sorted(
            (ident for ident in baseline if ident not in drifted), key=_item_sort_key
        )

    payload = {
        "schema": "mountfile.acceptance-reconcile/v1",
        "claimed": len(claims),
        "observed": len(observed),
        "contradictions": [{"id": i, "claim": c, "probe": p} for i, c, p in novel],
        "known_category_mismatch": [
            {"id": i, "claim": c, "probe": p, "reason": baseline[i]}
            for i, c, p in known
        ],
        "stale_baseline": stale,
        "understated": [{"id": i, "claim": c, "probe": p} for i, c, p in understated],
        "unprobed": unprobed,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"утверждений: {len(claims)} | наблюдений: {len(observed)}")
        for ident, claim, probe in novel:
            print(f"утверждение сильнее пробы: {ident}: {claim} > {probe}")
        for ident, claim, probe in known:
            print(
                f"известное расхождение категории: {ident}: {claim} > {probe} "
                f"({baseline[ident]})"
            )
        for ident in stale:
            print(f"устаревшая запись baseline: {ident} больше не расходится")
        for ident, claim, probe in understated:
            print(f"устаревшее утверждение:    {ident}: {claim} < {probe}")
        for ident in unprobed:
            print(f"без пробы:                 {ident}")
        if not novel and not unprobed and not stale:
            print("утверждения не сильнее доказательств")

    if args.check and (novel or unprobed or stale):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
