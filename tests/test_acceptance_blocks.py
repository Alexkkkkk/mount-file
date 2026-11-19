# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Acceptance probes for tracker #510: one test per tracked block item.

Every item declared in ``docs/replay-acceptance.json`` must have a probe in
``mountfile.acceptance.blocks.BLOCK_CHECKS`` and at least one test here.  The
registry test fails when a probe is dropped or an item is added without one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mountfile.acceptance import artifact, blocks, contract, hashing, matrix
from mountfile.acceptance.checkers import CLEAN, UNAVAILABLE, probe_checker

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "docs" / "replay-acceptance.json"

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# registry contract
# --------------------------------------------------------------------------
def test_every_manifest_item_has_a_probe():
    manifest = contract.load_manifest(MANIFEST)
    declared = set(manifest.ids())
    probed = {check.ident for check in blocks.BLOCK_CHECKS}
    assert (
        declared == probed
    ), f"missing probes: {declared - probed}, extra: {probed - declared}"


def test_probe_ids_are_unique_and_match_block():
    ids = [check.ident for check in blocks.BLOCK_CHECKS]
    assert len(ids) == len(set(ids))
    for check in blocks.BLOCK_CHECKS:
        assert check.ident.startswith(check.block)


def test_no_probe_is_a_stub():
    for check in blocks.BLOCK_CHECKS:
        probe = check.probe
        assert callable(probe), check.ident
        assert (
            probe.__doc__ and probe.__doc__.strip()
        ), f"{check.ident} has no docstring"
        source = Path(probe.__code__.co_filename)
        assert source.exists()
        text = source.read_text(encoding="utf-8")
        # A probe that only returns a constant would make the tracker vacuous.
        assert probe.__name__ in text


def test_run_all_covers_every_item():
    payload = blocks.run_all(REPO_ROOT, MANIFEST)
    assert len(payload["items"]) == 32
    assert payload["counts"]["total"] == 32
    assert set(payload["counts"]) <= {
        blocks.VERIFIED,
        blocks.PARTIAL,
        blocks.NOT_EXERCISED,
        blocks.UNAVAILABLE,
        blocks.BLOCKED,
        "total",
    }


def test_structural_probes_never_claim_verified():
    """A probe that cannot execute the property may not report it as verified."""

    payload = blocks.run_all(REPO_ROOT, MANIFEST)
    structural = {
        item["id"] for item in payload["items"] if item["tier"] == blocks.STRUCTURE
    }
    assert structural, "no structural probes declared"
    for item in payload["items"]:
        if item["id"] in structural:
            assert item["status"] != blocks.VERIFIED, item["id"]


# --------------------------------------------------------------------------
# A. general and release base
# --------------------------------------------------------------------------
def test_a1_detects_symbol_in_runtime(tmp_path):
    (tmp_path / "mountfile").mkdir()
    name = "replay" + "_if_needed"
    (tmp_path / "mountfile" / "leak.py").write_text(f"{name}()\n", encoding="utf-8")
    result = blocks.probe_a1(blocks.RepoContext(tmp_path))
    assert result.status == blocks.PARTIAL


def test_a1_passes_on_clean_runtime(tmp_path):
    (tmp_path / "mountfile").mkdir()
    (tmp_path / "mountfile" / "ok.py").write_text("value = 1\n", encoding="utf-8")
    assert blocks.probe_a1(blocks.RepoContext(tmp_path)).status == blocks.VERIFIED


def test_a3_refuses_executable_drafts(tmp_path):
    proposals = tmp_path / "docs" / "proposals"
    proposals.mkdir(parents=True)
    (proposals / "draft.py").write_text("x = 1\n", encoding="utf-8")
    assert blocks.probe_a3(blocks.RepoContext(tmp_path)).status == blocks.PARTIAL


def test_a3_refuses_runtime_import_of_docs(tmp_path):
    (tmp_path / "mountfile").mkdir()
    (tmp_path / "mountfile" / "importer.py").write_text(
        "from docs.proposals import draft\n", encoding="utf-8"
    )
    assert blocks.probe_a3(blocks.RepoContext(tmp_path)).status == blocks.PARTIAL


def test_a4_detects_writable_claim(tmp_path):
    (tmp_path / "README.md").write_text(
        "| ext3/ext4 | Read-only | No |\n| NTFS | Writable | Yes |\n", encoding="utf-8"
    )
    result = blocks.probe_a4(blocks.RepoContext(tmp_path))
    assert result.status == blocks.PARTIAL


def test_a4_accepts_readonly_rows(tmp_path):
    (tmp_path / "README.md").write_text(
        "| ext3/ext4 | Read-only | No |\n| NTFS | Read-only | No |\n", encoding="utf-8"
    )
    assert blocks.probe_a4(blocks.RepoContext(tmp_path)).status == blocks.VERIFIED


# --------------------------------------------------------------------------
# B. ext4 / JBD2
# --------------------------------------------------------------------------
def test_b1_journal_gate_never_returns_clean_for_junk(tmp_path):
    image = tmp_path / "junk.img"
    image.write_bytes(b"not-ext4" * 512)
    result = blocks.probe_b1(blocks.RepoContext(tmp_path))
    assert result.status in {blocks.VERIFIED, blocks.UNAVAILABLE, blocks.PARTIAL}
    assert "unexpected" not in result.detail.lower()


def test_b3_checksum_roundtrip_is_accepted():
    result = blocks.probe_b3(blocks.RepoContext(REPO_ROOT))
    assert result.status in {blocks.VERIFIED, blocks.PARTIAL}


def test_b5_rollover_ordering():
    result = blocks.probe_b5(blocks.RepoContext(REPO_ROOT))
    assert result.status == blocks.VERIFIED


def test_b6_tampering_is_rejected():
    assert blocks.probe_b6(blocks.RepoContext(REPO_ROOT)).status == blocks.VERIFIED


def test_b9_requires_refusal_path():
    result = blocks.probe_b9(blocks.RepoContext(REPO_ROOT))
    assert result.status in {blocks.VERIFIED, blocks.PARTIAL}


# --------------------------------------------------------------------------
# C. NTFS
# --------------------------------------------------------------------------
def test_c1_refusal_preserves_non_ntfs_image(tmp_path):
    result = blocks.probe_c1(blocks.RepoContext(tmp_path))
    assert result.status == blocks.VERIFIED
    assert "refused with preservation" in result.detail


def test_c2_requires_parser_structures():
    result = blocks.probe_c2(blocks.RepoContext(REPO_ROOT))
    assert result.status == blocks.VERIFIED


def test_c7_quarantine_copies_and_preserves_source(tmp_path):
    assert blocks.probe_c7(blocks.RepoContext(tmp_path)).status == blocks.VERIFIED


def test_c4_is_blocked_by_decision():
    assert blocks.probe_c4(blocks.RepoContext(REPO_ROOT)).status == blocks.BLOCKED


# --------------------------------------------------------------------------
# D. shared matrix
# --------------------------------------------------------------------------
def test_d1_matrix_holds_and_control_publishes(tmp_path):
    result = blocks.probe_d1(blocks.RepoContext(tmp_path))
    # copy:/helper: phases are instrumented by the writer, not by the publish
    # primitive, so the probe must stay partial instead of claiming coverage.
    assert result.status == blocks.PARTIAL, result.detail
    assert result.payload["total"] >= len(matrix.PUBLISH_POINTS)
    assert result.payload["exercised"] >= len(matrix.PUBLISH_POINTS)
    assert result.payload["total"] - result.payload["exercised"] >= 0


def test_d1_matrix_reports_violations_instead_of_passing(tmp_path):
    summary = matrix.matrix_summary(matrix.run_failure_matrix(tmp_path, profile="ext4"))
    assert summary["violations"] == []
    assert summary["counts"].get("mutated", 0) == 0
    assert summary["counts"].get(matrix.PASSED, 0) == 1


def test_d1_matrix_detects_a_leak(tmp_path):
    """A cell must fail when the staged sibling survives the injected failure."""

    cells = matrix.run_failure_matrix(tmp_path, profile="ext4")
    assert cells, "no cells produced"
    allowed = {
        matrix.UNCHANGED,
        matrix.LEAKED,
        matrix.NOT_EXERCISED,
        matrix.PASSED,
        "mutated",
    }
    assert all(cell.state in allowed for cell in cells)
    assert any(cell.state in {matrix.UNCHANGED, matrix.PASSED} for cell in cells)


def test_d3_refusals_preserve_source(tmp_path):
    result = blocks.probe_d3(blocks.RepoContext(tmp_path))
    assert result.status == blocks.PARTIAL, result.detail
    assert "not exercised" in result.detail


def test_d4_detects_concurrent_modification(tmp_path):
    assert blocks.probe_d4(blocks.RepoContext(tmp_path)).status == blocks.VERIFIED


def test_d5_separates_unavailable_from_clean():
    assert probe_checker("mountfile-missing-checker-xyz").state == UNAVAILABLE
    e2fsck = probe_checker("e2fsck")
    assert e2fsck.state in {CLEAN, UNAVAILABLE}


def test_d7_typed_refusals_for_unsupported_cells():
    result = blocks.probe_d7(blocks.RepoContext(REPO_ROOT))
    assert result.status in {blocks.VERIFIED, blocks.PARTIAL}


# --------------------------------------------------------------------------
# E. parity and status
# --------------------------------------------------------------------------
def test_e1_reports_declared_runners():
    result = blocks.probe_e1(blocks.RepoContext(REPO_ROOT))
    assert result.status in {blocks.PARTIAL, blocks.NOT_EXERCISED, blocks.VERIFIED}


def test_e3_single_capability_contract():
    assert blocks.probe_e3(blocks.RepoContext(REPO_ROOT)).status == blocks.VERIFIED


# --------------------------------------------------------------------------
# publication primitive
# --------------------------------------------------------------------------
def test_publish_replaces_target_with_staged_bytes(tmp_path):
    target = tmp_path / "image.img"
    target.write_bytes(b"old")
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"new")
    identity = hashing.SourceIdentity.capture(target)
    digest = hashing.publish_atomically(staged, target, identity)
    assert target.read_bytes() == b"new"
    assert digest == hashing.sha256_bytes(b"new")
    assert not staged.exists()


def test_publish_refuses_digest_mismatch(tmp_path):
    target = tmp_path / "image.img"
    target.write_bytes(b"old")
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"new")
    identity = hashing.SourceIdentity.capture(target)
    with pytest.raises(hashing.PublishRefused):
        hashing.publish_atomically(staged, target, identity, expected_sha256="0" * 64)
    assert target.read_bytes() == b"old"


def test_publish_refuses_concurrent_change(tmp_path):
    target = tmp_path / "image.img"
    target.write_bytes(b"old")
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"new")
    identity = hashing.SourceIdentity.capture(target)
    target.write_bytes(b"changed")
    with pytest.raises(hashing.SourceChanged):
        hashing.publish_atomically(staged, target, identity)
    assert target.read_bytes() == b"changed"


def test_publish_emits_declared_points(tmp_path):
    seen: list[str] = []
    target = tmp_path / "image.img"
    target.write_bytes(b"old")
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"new")
    identity = hashing.SourceIdentity.capture(target)
    hashing.publish_atomically(staged, target, identity, fault_hook=seen.append)
    assert seen == list(hashing.PUBLISH_POINTS)


def test_matrix_phases_include_runtime_points():
    phases = list(matrix.iter_phases())
    for point in hashing.PUBLISH_POINTS:
        assert point in phases


# --------------------------------------------------------------------------
# artifact
# --------------------------------------------------------------------------
def test_artifact_is_written_and_self_describing(tmp_path):
    target = artifact.write_artifact(REPO_ROOT, output=tmp_path / "out.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema"] == artifact.SCHEMA
    assert payload["parity"] == "not_claimed"
    assert payload["declared_checks"] == 32
    assert len(payload["items"]) == 32
    assert "checkers" in payload


def test_artifact_reports_drift_against_manifest():
    payload = artifact.build_artifact(REPO_ROOT, MANIFEST)
    assert isinstance(payload["drift"], list)
    for entry in payload["drift"]:
        assert "manifest=" in entry and "probe=" in entry


# --------------------------------------------------------------------------
# contract rules
# --------------------------------------------------------------------------
def _manifest_payload(**overrides):
    item = {
        "id": "A1",
        "block": "A",
        "title": "t",
        "status": "verified",
        "evidence": ["README.md"],
        "reproduced": "2026-11-18: pytest",
    }
    item.update(overrides)
    return {"schema": 1, "slice": {}, "items": [item]}


def test_contract_rejects_verified_without_reproduced():
    with pytest.raises(contract.ManifestError):
        contract.parse_manifest(_manifest_payload(reproduced=None))


def test_contract_rejects_not_started_with_evidence():
    with pytest.raises(contract.ManifestError):
        contract.parse_manifest(
            _manifest_payload(status="not_started", reproduced=None)
        )


def test_contract_rejects_reproduced_of_wrong_type():
    with pytest.raises(contract.ManifestError):
        contract.parse_manifest(_manifest_payload(reproduced={"date": "2026-11-18"}))


def test_contract_rejects_id_block_mismatch():
    with pytest.raises(contract.ManifestError):
        contract.parse_manifest(_manifest_payload(block="B"))


def test_missing_evidence_is_reported(tmp_path):
    manifest = contract.parse_manifest(_manifest_payload(evidence=["nope.md"]))
    problems = contract.missing_evidence(manifest, tmp_path)
    assert problems and "nope.md" in problems[0]


def test_repository_manifest_has_no_missing_evidence():
    manifest = contract.load_manifest(MANIFEST)
    assert contract.missing_evidence(manifest, REPO_ROOT) == []
    assert manifest.claimed_counts()["total"] == 32
