#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Check the #510 tracker slice header mechanically instead of by hand.

``docs/replay-acceptance.json`` opens with a ``slice`` block naming the branch,
commit, version and date the acceptance checklist was last verified against.
That block is hand-written prose, so it drifts: it kept pointing at
``fix/issue-510-ntfs-post-crash-readback @ 57b3c77`` after that branch had been
absorbed into ``main``, and it still claimed ``v1.96.0`` after the tree had
moved to ``1.96.1``.  A reader cannot tell a stale header from a fresh one.

This script makes the header testable::

    python scripts/replay_slice_sync.py             # human report
    python scripts/replay_slice_sync.py --check     # exit 1 on a stale slice
    python scripts/replay_slice_sync.py --write     # rewrite the slice header
    python scripts/replay_slice_sync.py --write --branch main   # pin the branch
    python scripts/replay_slice_sync.py --json      # machine output

Scope stays deliberately narrow: item statuses are owned by
``scripts/replay_acceptance_status.py``.  The only question here is whether the
header still describes this tree, and every verdict is derived from the tree
itself - never from a claim written in the manifest.

Checks that cannot be answered without git refs (a merged branch name, a commit
that is not reachable in a shallow CI checkout) are reported as advisories and
never fail the default gate, so a shallow clone cannot invent a failure.
``--strict`` promotes the *existence* findings to blocking: a slice branch that
no longer exists and a slice commit this checkout cannot resolve both mean the
header names a tree that is not here.  Two advisories stay advisory even under
strict, because neither is a defect: a gitless checkout cannot answer the
question at all, and a slice commit behind HEAD is the normal state between two
sync points (the header records when it was last verified, not the current tip).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Manifest path relative to the repository root.
MANIFEST_PATH = Path("docs/replay-acceptance.json")

#: Where the canonical package version lives, in priority order.
PACKAGE_INIT = Path("mountfile") / "__init__.py"
PYPROJECT = Path("pyproject.toml")

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_PROJECT_VERSION_RE = re.compile(
    r"""^\[project\][^\[]*?^version\s*=\s*["']([^"']+)["']""",
    re.MULTILINE | re.DOTALL,
)

#: Exit code for a manifest or checkout that cannot be inspected at all.
EXIT_CONFIG_ERROR = 2


class SliceError(RuntimeError):
    """The manifest or the checkout cannot be read, so nothing can be judged."""


def _git(root: Path, *args: str) -> str | None:
    """Run git and return stripped stdout, or None when git says no."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _is_shallow(root: Path) -> bool:
    """True when the checkout is a shallow clone (partial object store)."""
    return _git(root, "rev-parse", "--is-shallow-repository") == "true"


def _git_ok(root: Path, *args: str) -> bool:
    """True when git exits 0, for commands whose success prints nothing."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def git_slice(root: Path) -> tuple[str | None, str | None]:
    """Return ``(branch, commit)`` of the working tree, ``(None, None)`` if gitless."""
    commit = _git(root, "rev-parse", "--short=7", "HEAD")
    if commit is None:
        return None, None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":  # detached checkout: no branch to claim
        branch = None
    return branch, commit


def git_branches(root: Path) -> list[str]:
    """Local and remote branch names, without the ``origin/`` prefix."""
    listing = _git(
        root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"
    )
    if listing is None:
        return []
    names = set()
    for line in listing.splitlines():
        name = line.strip()
        if not name or name.endswith("/HEAD"):
            continue
        names.add(name.split("/", 1)[1] if name.startswith("origin/") else name)
    return sorted(names)


def tree_version(root: Path) -> str | None:
    """Canonical version of this tree: package ``__version__``, else pyproject."""
    init = root / PACKAGE_INIT
    if init.is_file():
        match = _VERSION_RE.search(init.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    pyproject = root / PYPROJECT
    if pyproject.is_file():
        match = _PROJECT_VERSION_RE.search(pyproject.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return None


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the manifest, raising ``SliceError`` when it is missing or broken."""
    if not path.is_file():
        raise SliceError(f"манифест не найден: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SliceError(f"манифест не читается: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SliceError(f"манифест должен быть JSON-объектом: {path}")
    return data


def slice_drift(
    manifest: dict[str, Any],
    root: Path,
    *,
    git_state: tuple[str | None, str | None] | None = None,
    strict: bool = False,
) -> tuple[list[str], list[str]]:
    """Return ``(blocking, advisory)`` reasons the slice header is stale.

    With ``strict=True`` the existence findings become blocking, so a header
    naming a branch or commit this checkout cannot resolve fails the gate.
    A missing ``.git`` and a slice commit behind HEAD stay advisory: the first
    is a property of the checkout, the second is the header's intended shape.
    """
    blocking: list[str] = []
    advisory: list[str] = []

    def _record(reason: str) -> None:
        """Keep a git-derived existence finding, blocking under strict mode."""
        (blocking if strict else advisory).append(reason)

    slice_block = manifest.get("slice")
    if not isinstance(slice_block, dict):
        return ["в манифесте нет секции slice"], advisory

    version = tree_version(root)
    claimed_version = slice_block.get("version")
    if version is None:
        advisory.append(
            "версию дерева определить не удалось: slice.version не проверен"
        )
    elif claimed_version != version:
        blocking.append(f"slice.version = {claimed_version!r}, а в дереве {version!r}")

    branch, commit = git_state if git_state is not None else git_slice(root)
    if commit is None:
        advisory.append("git недоступен: привязку среза к коммиту проверить нечем")
        return blocking, advisory

    claimed_commit = slice_block.get("commit")
    if claimed_commit and not _git_ok(
        root, "cat-file", "-e", f"{claimed_commit}^{{commit}}"
    ):
        if _is_shallow(root):
            # Shallow-ness is a property of the checkout, not of the manifest:
            # a partial clone cannot resolve old commits even when they are
            # real and reachable on the remote. Downgrade to advisory so a
            # shallow CI checkout does not turn a green gate red.
            advisory.append(
                f"slice.commit = {claimed_commit!r} не разрешается "
                "в shallow-checkout: объект недоступен локально, дрейф не подтверждён"
            )
        else:
            _record(f"slice.commit = {claimed_commit!r} в этом checkout не разрешается")

    claimed_branch = slice_block.get("branch")
    if claimed_branch:
        known = git_branches(root)
        if known and claimed_branch not in known:
            _record(
                f"slice.branch = {claimed_branch!r} среди веток репозитория не существует"
            )

    if claimed_branch and branch and claimed_branch == branch and claimed_commit:
        if claimed_commit != commit:
            # Deliberately advisory in every mode: the slice records the
            # commit the checklist was last verified against, so it trails the
            # tip by design and must not redden every merge to main.
            advisory.append(
                f"slice.commit = {claimed_commit!r} отстаёт от {commit!r} на ветке {branch!r}"
            )

    return blocking, advisory


def sync_slice(
    manifest: dict[str, Any],
    root: Path,
    *,
    checked: str | None = None,
    git_state: tuple[str | None, str | None] | None = None,
) -> dict[str, Any]:
    """Return the manifest with its slice header rewritten from the live tree."""
    slice_block = manifest.get("slice")
    if not isinstance(slice_block, dict):
        raise SliceError("в манифесте нет секции slice")
    version = tree_version(root)
    if version is None:
        raise SliceError(
            "версия дерева не найдена: нужен mountfile/__init__.py или pyproject.toml"
        )
    branch, commit = git_state if git_state is not None else git_slice(root)

    updated = dict(slice_block)  # keeps the author's key order
    if branch:
        updated["branch"] = branch
    elif commit:
        updated.setdefault("branch", slice_block.get("branch"))
    if commit:
        updated["commit"] = commit
    updated["version"] = version
    updated["checked"] = checked or dt.date.today().isoformat()
    manifest["slice"] = updated
    return manifest


def dump_manifest(manifest: dict[str, Any], path: Path) -> None:
    """Write the manifest back with the repository's 2-space JSON style."""
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверить секцию slice манифеста #510 против живого дерева."
    )
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--root", default=".")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1, если срез устарел")
    mode.add_argument("--write", action="store_true", help="переписать секцию slice")
    parser.add_argument(
        "--branch",
        default=None,
        help=(
            "ветка, которую должен называть срез при --write "
            "(по умолчанию - ветка текущего checkout)"
        ),
    )
    parser.add_argument(
        "--commit",
        default=None,
        help=(
            "коммит, который должен называть срез при --write "
            "(по умолчанию - HEAD; в релизном PR передаётся merge-base с main, "
            "потому что HEAD релизной ветки исчезает при слиянии)"
        ),
    )
    parser.add_argument(
        "--checked", default=None, help="дата сверки для --write (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="считать неразрешимые ветку/коммит блокирующими (гейт main)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (root / manifest_path).resolve()

    try:
        manifest = load_manifest(manifest_path)
        if not isinstance(manifest.get("slice"), dict):
            raise SliceError(f"в манифесте нет секции slice: {manifest_path}")
        if args.write:
            # ``--branch`` pins the branch the header names.  A release branch
            # is cut from main, so the automation must keep the header pointing
            # at main: the gate test asserts slice.branch == "main" on every
            # branch, and the slice documents the tree main carries.
            git_state = None
            if args.branch or args.commit:
                current_branch, current_commit = git_slice(root)
                git_state = (
                    args.branch or current_branch,
                    args.commit or current_commit,
                )
            manifest = sync_slice(
                manifest, root, checked=args.checked, git_state=git_state
            )
            dump_manifest(manifest, manifest_path)
        blocking, advisory = slice_drift(manifest, root, strict=args.strict)
    except SliceError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    slice_block = manifest.get("slice") or {}
    if args.json:
        print(
            json.dumps(
                {"slice": slice_block, "blocking": blocking, "advisory": advisory},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"slice | {slice_block.get('branch')} @ {slice_block.get('commit')} "
            f"({slice_block.get('version')}) | сверка {slice_block.get('checked')}"
        )
        for reason in advisory:
            print(f"примечание: {reason}")
        for reason in blocking:
            print(f"расхождение: {reason}")
        if not blocking:
            print("срез соответствует дереву")

    if args.check and blocking:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
