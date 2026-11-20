# SPDX-License-Identifier: MIT
"""#510: the tracker guards must actually run, and strict mode must bite.

Three guards live in the tree - ``scripts/replay_slice_sync.py``,
``scripts/replay_acceptance_status.py`` and ``scripts/acceptance_blocks.py`` -
and the slice header still drifted twice by hand, because nothing invoked them
in CI.  ``--strict`` existed nowhere either, so a manifest naming a branch that
does not exist was reported as a harmless advisory while the header was stale.

These tests pin the three things that make the gates real:

* ``--strict`` promotes the *existence* findings to a failure (a slice branch
  that does not exist, a slice commit this checkout cannot resolve) but never
  promotes "this checkout has no git" or "the slice commit trails the tip", so
  a sandbox cannot invent a red gate and no merge to `main` is punished for a
  header that records its last verified commit;
* ``.github/workflows/acceptance-gates.yml`` calls all three gates;
* the committed manifest describes ``main`` and cites the dedicated D3 hash
  matrix that proves per-step source preservation.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "acceptance-gates.yml"
MANIFEST = REPO_ROOT / "docs" / "replay-acceptance.json"
SYNC_PATH = REPO_ROOT / "scripts" / "replay_slice_sync.py"


def _load_sync():
    spec = importlib.util.spec_from_file_location("replay_slice_sync_gate", SYNC_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_sync()


def _git_repo(root: Path, version: str = "1.99.0") -> Path:
    (root / "mountfile").mkdir(parents=True, exist_ok=True)
    (root / "mountfile" / "__init__.py").write_text(
        f'# SPDX-License-Identifier: MIT\n__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "docs").mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=gate@example.invalid",
            "-c",
            "user.name=gate",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
    ):
        subprocess.run(command, cwd=str(root), check=True, capture_output=True)
    return root


def _manifest(
    root: Path,
    *,
    branch: str = "main",
    commit: str | None = None,
    version: str = "1.99.0",
) -> Path:
    if commit is None:
        commit = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    path = root / "docs" / "replay-acceptance.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "tracker": 510,
                "slice": {
                    "branch": branch,
                    "commit": commit,
                    "version": version,
                    "checked": "2026-11-20",
                },
                "items": [
                    {
                        "id": "A1",
                        "block": "A",
                        "title": "probe",
                        "status": "verified",
                        "evidence": ["mountfile/__init__.py"],
                        "reproduced": "2026-11-20: synthetic gate fixture",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_unknown_branch_is_advisory_without_strict(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    manifest = _manifest(root, branch="ghost-branch-that-never-existed")
    argv = ["--root", str(root), "--manifest", str(manifest), "--check"]
    assert sync.main(argv) == 0


def test_strict_promotes_unknown_branch_to_failure(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    manifest = _manifest(root, branch="ghost-branch-that-never-existed")
    argv = ["--root", str(root), "--manifest", str(manifest), "--check", "--strict"]
    assert sync.main(argv) == 1


def test_slice_behind_head_stays_advisory_even_under_strict(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    first = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (root / "extra.txt").write_text("drift\n", encoding="utf-8")
    # `commit -am` would skip the new untracked file and commit nothing, which
    # leaves HEAD on the claimed commit and makes this cell silently vacuous.
    for command in (
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=gate@example.invalid",
            "-c",
            "user.name=gate",
            "commit",
            "-q",
            "-m",
            "advance",
        ],
    ):
        subprocess.run(command, cwd=str(root), check=True, capture_output=True)
    manifest = _manifest(root, branch="main", commit=first)

    argv = ["--root", str(root), "--manifest", str(manifest), "--check"]
    assert sync.main(argv) == 0
    # The header records the commit the checklist was verified against, so it
    # trails the tip by design: every merge to main would otherwise redden.
    assert sync.main(argv + ["--strict"]) == 0
    blocking, advisory = sync.slice_drift(
        json.loads(manifest.read_text(encoding="utf-8")), root, strict=True
    )
    assert blocking == []
    assert any("отстаёт от" in reason for reason in advisory)


def test_strict_does_not_punish_a_gitless_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "mountfile").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mountfile" / "__init__.py").write_text(
        '__version__ = "1.99.0"\n', encoding="utf-8"
    )
    manifest = tmp_path / "docs" / "replay-acceptance.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "slice": {
                    "branch": "main",
                    "commit": "163ac5e",
                    "version": "1.99.0",
                    "checked": "2026-11-20",
                },
                "items": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    argv = ["--root", str(tmp_path), "--manifest", str(manifest), "--check", "--strict"]
    assert sync.main(argv) == 0
    assert "git недоступен" in capsys.readouterr().out


def test_ci_calls_every_tracker_gate() -> None:
    assert WORKFLOW.is_file(), "the acceptance-gates workflow is missing"
    text = WORKFLOW.read_text(encoding="utf-8")
    for command in (
        "scripts/replay_acceptance_status.py --check",
        "scripts/acceptance_blocks.py --check",
        "scripts/replay_slice_sync.py --check",
        "scripts/replay_slice_sync.py --check --strict",
    ):
        assert command in text, f"workflow does not run: {command}"
    # The strict gate is only meaningful with the history it has to resolve.
    assert "fetch-depth: 0" in text


def test_manifest_slice_describes_main_and_cites_the_d3_matrix() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    header = payload["slice"]
    assert header["branch"] == "main", header
    assert header["checked"] == "2026-11-20"

    items = {item["id"]: item for item in payload["items"]}
    assert "tests/test_hash_preservation_matrix.py" in items["D3"]["evidence"]
    for item in payload["items"]:
        assert item["status"] in {"verified", "partial", "not_started", "blocked"}


# --------------------------------------------------------------------------
# probe gate baseline (six structure-tier items)
# --------------------------------------------------------------------------
BASELINE = REPO_ROOT / "docs" / "acceptance-known-drift.txt"


def _load_blocks_script():
    spec = importlib.util.spec_from_file_location(
        "acceptance_blocks_gate", REPO_ROOT / "scripts" / "acceptance_blocks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


blocks_script = _load_blocks_script()


def test_baseline_parses_and_is_fully_explained() -> None:
    entries = blocks_script.load_baseline(BASELINE)
    assert set(entries) == {"B4", "B7", "B10", "C2", "C8", "E2"}
    for ident, reason in entries.items():
        assert reason, f"{ident} has no explanation"


def test_baseline_only_names_structure_probes() -> None:
    """A behavioural probe disagrees with the manifest only if something broke."""

    from mountfile.acceptance import blocks as block_defs

    tiers = {check.ident: check.tier for check in block_defs.BLOCK_CHECKS}
    for ident in blocks_script.load_baseline(BASELINE):
        assert tiers[ident] == block_defs.STRUCTURE, ident


def test_baseline_matches_the_drift_observed_today() -> None:
    """No stale ids: a fixed item must delete its entry, not inherit a waiver."""

    from mountfile.acceptance import artifact

    payload = artifact.build_artifact(REPO_ROOT, MANIFEST)
    known, new, stale = blocks_script.split_drift(
        payload["drift"], blocks_script.load_baseline(BASELINE)
    )
    assert new == [], f"new drift not explained by the baseline: {new}"
    assert stale == [], f"baseline entries that no longer drift: {stale}"
    assert len(known) == 6


def test_split_drift_separates_new_and_stale() -> None:
    baseline = {"B4": "explained"}
    drift = [
        "B4: manifest=verified probe=partial",
        "D3: manifest=partial probe=verified",
    ]
    known, new, stale = blocks_script.split_drift(drift, baseline)
    assert known == ["B4: manifest=verified probe=partial"]
    assert new == ["D3: manifest=partial probe=verified"]
    assert stale == []

    _, new, stale = blocks_script.split_drift([], baseline)
    assert new == []
    assert stale == ["B4"]


def test_baseline_file_rejects_a_duplicate_id(tmp_path: Path) -> None:
    path = tmp_path / "dup.txt"
    path.write_text("B4  # one\nB4  # two\n", encoding="utf-8")
    with pytest.raises(ValueError):
        blocks_script.load_baseline(path)


def test_workflow_uses_the_baseline() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--baseline docs/acceptance-known-drift.txt" in text
