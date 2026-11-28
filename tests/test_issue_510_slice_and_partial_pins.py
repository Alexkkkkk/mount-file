# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Issue #510: pins for the two failure modes the tracker kept reopening on.

The tracker body recorded both of these as live defects rather than as
unfinished features:

* the published slice header (``docs/replay-acceptance.json``) pointed at an
  older commit than ``main`` while the tracker text claimed a fresh audit, and
  every gate treated "slice trails the tip" as a non-blocking advisory;
* the eight ``partial`` items (C3, C5, C6, D1, D3, D5, D6, E1) were described in
  prose, so a silent promotion to ``verified`` -- or evidence that no longer
  contains an executable test -- would not have turned anything red.

Both are cheap to pin and neither duplicates an existing module:
``tests/test_replay_acceptance_status.py`` checks that the counts add up,
``tests/test_acceptance_gates.py`` checks that the gate scripts are invoked and
that strict mode bites on *existence* findings.  Nothing pinned the identity of
the partial set, nothing checked that each partial item still cites a test with
real test functions in it, and nothing checked the slice commit against the
tree at all.

None of these tests claim a gap is closed.  ``docs/replay-acceptance.json``
stays authoritative; these pins only make the honest status fail loudly if it
rot.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.replay_acceptance_status import load_manifest, summarise

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "docs" / "replay-acceptance.json"

#: The gaps #510 is still open for, as of slice 2.0.2.  Closing a gap means
#: editing this set in the same commit that upgrades the manifest -- the point
#: is that it cannot happen by accident.
EXPECTED_PARTIAL = frozenset({"C3", "C5", "C6", "D1", "D3", "D5", "D6", "E1"})

#: A refusal that stays a refusal: ``$ATTRIBUTE_LIST`` replay is out of scope.
EXPECTED_BLOCKED = frozenset({"C4"})


def _git(root: Path, *args: str) -> str | None:
    """Run git, returning stdout or ``None`` when it cannot answer."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _by_status(manifest: dict, status: str) -> set[str]:
    return {item["id"] for item in manifest["items"] if item["status"] == status}


def test_partial_set_is_exactly_the_eight_open_gaps():
    """A promotion to ``verified`` must be a reviewed edit, not a side effect."""

    manifest = load_manifest(MANIFEST)
    assert _by_status(manifest, "partial") == EXPECTED_PARTIAL


def test_blocked_set_stays_the_declared_out_of_scope_item():
    manifest = load_manifest(MANIFEST)
    assert _by_status(manifest, "blocked") == EXPECTED_BLOCKED


def test_summary_agrees_with_the_pinned_sets():
    """The gate's own accounting must match the item-level pins."""

    manifest = load_manifest(MANIFEST)
    counts = summarise(manifest)
    assert counts["partial"] == len(EXPECTED_PARTIAL)
    assert counts["blocked"] == len(EXPECTED_BLOCKED)
    assert counts["not_started"] == 0
    assert counts["verified"] + counts["partial"] + counts["blocked"] == counts["total"]


def test_partial_items_still_cite_an_executable_test():
    """Evidence rot is the quiet way a ``partial`` item pretends to be covered.

    Every partial item names at least one file under ``tests/``, and that file
    must still contain test functions.  A renamed or gutted evidence module
    fails here instead of being discovered at the next manual audit.
    """

    manifest = load_manifest(MANIFEST)
    offenders: list[str] = []
    for item in manifest["items"]:
        if item["status"] != "partial":
            continue
        test_files = [
            entry for entry in item.get("evidence", []) if entry.startswith("tests/")
        ]
        if not test_files:
            offenders.append(f"{item['id']}: no test evidence")
            continue
        executable = False
        for entry in test_files:
            path = REPO_ROOT / entry
            if not path.is_file():
                offenders.append(f"{item['id']}: missing evidence file {entry}")
                continue
            if "def test_" in path.read_text(encoding="utf-8", errors="replace"):
                executable = True
        if not executable:
            offenders.append(f"{item['id']}: no test function in {test_files}")
    assert offenders == []


def test_slice_commit_is_an_ancestor_of_head():
    """The published slice must describe history, not an unrelated commit.

    A slice commit that this checkout cannot resolve (shallow clone, no ``.git``
    at all) is reported as a typed skip: absence of history is not evidence of
    a stale header, and inventing a red gate from it would punish every
    ``fetch-depth: 1`` CI leg.
    """

    manifest = load_manifest(MANIFEST)
    commit = manifest["slice"]["commit"]

    if _git(REPO_ROOT, "rev-parse", "--git-dir") is None:
        pytest.skip("not proven (unavailable): checkout carries no git directory")
    if _git(REPO_ROOT, "cat-file", "-e", f"{commit}^{{commit}}") is None:
        pytest.skip(
            f"not proven (not_proven): shallow checkout cannot resolve {commit}"
        )

    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"slice.commit {commit} is not an ancestor of HEAD; "
        "the published slice describes an unrelated commit"
    )


def test_slice_names_the_branch_it_is_published_for():
    manifest = load_manifest(MANIFEST)
    assert manifest["slice"]["branch"] == "main"


def test_manifest_version_matches_the_package_version():
    """A version bump without a slice re-check is the drift the tracker logged."""

    manifest = load_manifest(MANIFEST)
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = None
    for line in pyproject.splitlines():
        if line.startswith("version = "):
            declared = line.split("=", 1)[1].strip().strip('"')
            break
    assert declared is not None
    assert manifest["slice"]["version"] == declared

    init_text = (REPO_ROOT / "mountfile" / "__init__.py").read_text(encoding="utf-8")
    for line in init_text.splitlines():
        if line.startswith("__version__"):
            package = line.split("=", 1)[1].strip().strip('"').strip("'")
            assert package == declared
            break
    else:  # pragma: no cover - the package always declares __version__
        pytest.fail("mountfile/__init__.py does not declare __version__")


def test_every_item_is_json_serialisable_after_a_round_trip(tmp_path: Path):
    """The manifest is published verbatim; keep it byte-stable on a round trip."""

    original = json.loads(MANIFEST.read_text(encoding="utf-8"))
    copy = tmp_path / "manifest.json"
    copy.write_text(
        json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert json.loads(copy.read_text(encoding="utf-8")) == original
