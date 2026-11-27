# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the replay acceptance manifest (tracker #510) - no network.

Run:  python -m pytest tests/test_replay_acceptance_status.py -v
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "replay_acceptance_status", ROOT / "scripts" / "replay_acceptance_status.py"
)
assert _spec is not None and _spec.loader is not None
ras = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ras)


def test_manifest_is_structurally_valid():
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    assert manifest["tracker"] == 510
    assert len(manifest["items"]) == 32


def test_manifest_matches_the_tree():
    """The whole point: a deleted evidence file fails CI, not a reviewer."""
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    problems = ras.verify(manifest, ROOT)
    assert problems == [], "доказательства разошлись с деревом: %s" % problems


def _project_version() -> str:
    """Read the real ``[project].version`` value from pyproject.toml.

    The table header is located by exact match: a plain substring search for
    ``[project]`` also hits the ``# [project].version ...`` comment at the top
    of the file, which would make this read the wrong block.
    """
    lines = (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == "[project]"),
        None,
    )
    assert start is not None, "pyproject.toml has no [project] table"
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("["):
            break
        match = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
        if match:
            return match.group(1)
    raise AssertionError("pyproject.toml has no [project].version")


def _semver(value: str):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(value))
    assert match, "not a semantic version: %r" % (value,)
    return tuple(int(part) for part in match.groups())


def test_slice_points_at_a_real_version():
    """The slice records an audited snapshot, so it trails the release bump.

    It must name a real, well-formed version that was actually released - not
    whatever version a release PR is bumping TO right now.  Comparing the two
    for equality made every "chore(release)" PR fail this gate, which is why
    1.96.1 and 1.97.0 could never merge.
    """
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    version = manifest["slice"]["version"]
    recorded = _semver(version)
    current = _semver(_project_version())
    assert recorded <= current, (
        "slice version %s is newer than the project version, so it cannot have "
        "been released yet" % version
    )


def test_counts_add_up():
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    counts = ras.summarise(manifest)
    assert counts["total"] == sum(
        counts[name] for name in ("verified", "partial", "not_started", "blocked")
    )


def test_every_item_has_a_known_block():
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    blocks = {item["block"] for item in manifest["items"]}
    assert blocks == {"A", "B", "C", "D", "E"}


def test_verified_requires_reproduction_record():
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    for item in manifest["items"]:
        if item["status"] == "verified":
            assert item.get("reproduced"), item["id"]


def test_markdown_contains_every_item():
    manifest = ras.load_manifest(ROOT / ras.MANIFEST_PATH)
    rendered = ras.render_markdown(manifest)
    for item in manifest["items"]:
        assert "| %s |" % item["id"] in rendered
    assert "Срез:" in rendered


def test_verify_flags_missing_evidence(tmp_path):
    manifest = {
        "schema": 1,
        "tracker": 510,
        "items": [
            {
                "id": "X1",
                "block": "A",
                "title": "t",
                "status": "verified",
                "evidence": ["nope/missing.py"],
                "reproduced": "2026-11-15",
            }
        ],
    }
    problems = ras.verify(manifest, tmp_path)
    assert any("nope/missing.py" in problem for problem in problems)


def test_verify_flags_not_started_with_evidence(tmp_path):
    manifest = {
        "schema": 1,
        "tracker": 510,
        "items": [
            {
                "id": "X2",
                "block": "A",
                "title": "t",
                "status": "not_started",
                "evidence": ["pyproject.toml"],
            }
        ],
    }
    problems = ras.verify(manifest, ROOT)
    assert any("X2" in problem for problem in problems)


def test_load_manifest_rejects_unknown_status(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "items": [
                    {
                        "id": "X3",
                        "block": "A",
                        "title": "t",
                        "status": "done",
                        "evidence": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        ras.load_manifest(path)
    except ras.ManifestError as exc:
        assert "статус" in str(exc)
    else:  # pragma: no cover - the manifest must be rejected
        raise AssertionError("неизвестный статус должен отклоняться")


def test_main_check_returns_zero_on_clean_manifest(capsys):
    assert ras.main(["--check", "--root", str(ROOT)]) == 0
    captured = capsys.readouterr()
    assert "расхождений нет" in captured.out
