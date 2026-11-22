# SPDX-License-Identifier: MIT
"""#510: ``source_untouched`` must be proven, never asserted.

The oracle in :mod:`tests.ntfs_oracle` used to compute

    "source_untouched": _sha256(fixture.image) == _sha256(ours) or True

which is unconditionally ``True``: the ``or True`` made every claim about the
source image unfalsifiable, so every acceptance statement that leaned on it
(D3, C6) was unsupported.  These tests keep the invariant honest:

* the comparison helper is exercised on real hashes (unit, no external tools);
* a deliberately mutated source must be reported as ``source_untouched=False``
  and must downgrade the verdict to ``divergence``;
* a static guard fails if the tautology ever reappears in an evidence module.
"""

from __future__ import annotations

import ast
import hashlib
import shutil
from pathlib import Path

import pytest

from tests.ntfs_oracle import SCENARIOS, build_dirty_fixture, run_oracle, source_integrity

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfsfix") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfsfix/ntfscat) are unavailable",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Modules that emit acceptance evidence.  A tautological ``or True`` there
#: silently converts "we did not check" into "it is fine".
EVIDENCE_MODULES = (
    Path("tests") / "ntfs_oracle.py",
    Path("scripts") / "ntfs_oracle_evidence.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_source_integrity_accepts_identical_hashes():
    digest = _sha256(Path(__file__))
    assert source_integrity(digest, digest) is True


def test_source_integrity_rejects_a_changed_hash():
    before = _sha256(Path(__file__))
    assert source_integrity(before, before[:-1] + ("0" if before[-1] != "0" else "1")) is False


def test_source_integrity_rejects_an_empty_hash():
    assert source_integrity("", "") is False
    assert source_integrity("", _sha256(Path(__file__))) is False


@needs_ntfs
def test_source_image_is_byte_identical_after_replay_for_every_scenario(tmp_path):
    for scenario in sorted(SCENARIOS):
        work = tmp_path / scenario
        work.mkdir(parents=True, exist_ok=True)
        fixture = build_dirty_fixture(work, scenario)
        before = _sha256(fixture.image)
        result = run_oracle(fixture, work)
        assert result["source_untouched"] is True, result
        assert result["source_sha256"] == before, result
        assert _sha256(fixture.image) == before, scenario


@needs_ntfs
def test_mutated_source_is_reported_as_divergence(tmp_path, monkeypatch):
    """Negative control: if the source digest changes, the oracle must notice.

    The replay works on copies, so a real "corrupted source" cannot be produced
    by simply editing the image (the replay would leave that edit in place and
    the digest comparison would legitimately agree).  Instead the digest of the
    *second* observation is perturbed: this fails loudly if ``source_untouched``
    is ever hard-wired to ``True`` again.
    """
    from tests import ntfs_oracle

    fixture = build_dirty_fixture(tmp_path, "resident-commit")
    real_sha256 = ntfs_oracle._sha256
    calls = {"count": 0}

    def fake_sha256(path: Path) -> str:
        calls["count"] += 1
        digest = real_sha256(path)
        if calls["count"] == 1:
            return digest
        return "0" * 64 if digest != "0" * 64 else "1" * 64

    monkeypatch.setattr(ntfs_oracle, "_sha256", fake_sha256)
    work = tmp_path / "mutated"
    work.mkdir()
    result = run_oracle(fixture, work)

    assert calls["count"] >= 2, "source digest was never re-measured after replay"
    assert result["source_untouched"] is False, result
    assert result["verdict"] == "divergence", result


def _tautological_boolean_nodes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        if any(
            isinstance(value, ast.Constant) and value.value is True
            for value in node.values
        ):
            offenders.append(f"{path}:{node.lineno}")
    return offenders


def test_evidence_modules_contain_no_tautological_boolean():
    offenders: list[str] = []
    for relative in EVIDENCE_MODULES:
        path = REPO_ROOT / relative
        assert path.exists(), f"evidence module disappeared: {relative}"
        offenders.extend(_tautological_boolean_nodes(path))
    assert offenders == [], f"tautological `or True` in evidence code: {offenders}"
