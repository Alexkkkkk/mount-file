"""Regression tests for the #510 slice-header guard (scripts/replay_slice_sync.py).

The tracker header in docs/replay-acceptance.json drifted twice by hand: a
branch that had already been merged, and a version older than the tree. These
tests pin the two behaviours that make the header checkable - a stale version
fails ``--check``, and ``--write`` makes the header describe the tree instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "replay_slice_sync.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("replay_slice_sync", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_module()


def _tree(root: Path, version: str = "1.96.1", *, use_pyproject: bool = False) -> Path:
    if use_pyproject:
        (root / "pyproject.toml").write_text(
            f'[build-system]\nrequires = ["setuptools>=68"]\n\n'
            f'[project]\nname = "mountfile"\nversion = "{version}"\n',
            encoding="utf-8",
        )
    else:
        (root / "mountfile").mkdir(parents=True, exist_ok=True)
        (root / "mountfile" / "__init__.py").write_text(
            f'# SPDX-License-Identifier: MIT\n__version__ = "{version}"\n',
            encoding="utf-8",
        )
    return root


def _manifest(
    root: Path,
    *,
    version: str = "1.96.1",
    branch: str = "fix/issue-510-ntfs-post-crash-readback",
    commit: str = "57b3c77",
    checked: str = "2026-11-17",
) -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "replay-acceptance.json"
    payload = {
        "schema": 1,
        "tracker": 510,
        "slice": {
            "branch": branch,
            "commit": commit,
            "version": version,
            "checked": checked,
        },
        "items": [{"id": "A1", "block": "A", "status": "verified", "evidence": []}],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_matching_slice_passes_check(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    manifest = _manifest(root)
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--check"]) == 0


def test_version_drift_fails_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, "1.97.0")
    manifest = _manifest(root, version="1.96.0")
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--check"]) == 1
    out = capsys.readouterr().out
    assert "slice.version" in out
    assert "1.96.0" in out and "1.97.0" in out


def test_write_rewrites_header_from_tree_and_then_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, "1.96.1")
    manifest = _manifest(root, version="1.95.0", commit="d31b4dc")
    argv = [
        "--root",
        str(root),
        "--manifest",
        str(manifest),
        "--write",
        "--checked",
        "2026-11-17",
    ]
    assert sync.main(argv) == 0

    header = _read(manifest)["slice"]
    assert header["version"] == "1.96.1"
    assert header["checked"] == "2026-11-17"
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--check"]) == 0


def test_write_preserves_items_and_key_order(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    manifest = _manifest(root, version="1.95.0")
    before = _read(manifest)
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--write"]) == 0
    after = _read(manifest)
    assert after["items"] == before["items"]
    assert list(after["slice"]) == ["branch", "commit", "version", "checked"]
    assert manifest.read_text(encoding="utf-8").endswith("}\n")


def test_missing_slice_is_a_configuration_error(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    manifest = root / "docs" / "replay-acceptance.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema": 1, "items": []}) + "\n", encoding="utf-8")
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--check"]) == 2


def test_version_falls_back_to_pyproject(tmp_path: Path) -> None:
    root = _tree(tmp_path, "1.96.1", use_pyproject=True)
    manifest = _manifest(root, version="1.90.0")
    assert sync.tree_version(root) == "1.96.1"
    assert sync.main(["--root", str(root), "--manifest", str(manifest), "--check"]) == 1


def test_unresolvable_commit_is_advisory_not_blocking(tmp_path: Path) -> None:
    """A shallow checkout must not turn an unreachable commit into a red gate."""
    root = _tree(tmp_path)
    manifest = _manifest(root, version="1.96.1")
    blocking, advisory = sync.slice_drift(_read(manifest), root)
    assert blocking == []
    assert any("git недоступен" in reason for reason in advisory)


def test_strict_promotes_unresolvable_git_refs_to_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict CI mode must fail on unresolved refs when git is available."""
    root = _tree(tmp_path)
    manifest = _manifest(root, version="1.96.1")
    monkeypatch.setattr(sync, "git_slice", lambda _root: ("main", "abcdef0"))
    monkeypatch.setattr(sync, "_git_ok", lambda *_args: False)
    monkeypatch.setattr(sync, "git_branches", lambda _root: ["main"])

    blocking, advisory = sync.slice_drift(_read(manifest), root, strict=True)

    assert any("slice.commit" in reason for reason in blocking)
    assert any("slice.branch" in reason for reason in blocking)
    assert advisory == []
