# SPDX-License-Identifier: MIT
"""External-oracle acceptance for the NTFS replay executor (issue #530 P1).

Every cataloged dirty fixture is replayed by the production executor and
then checked against the external ``ntfsfix`` oracle and byte-level
comparisons of the recovered ``$MFT`` / ``$Bitmap`` streams.  These tests
require the ntfs-3g tools and run in the Linux integration and nightly
legs; elsewhere they skip - a skip is never reported as a pass.
"""

import shutil
from pathlib import Path

import pytest

from tests.ntfs_oracle import SCENARIOS, build_dirty_fixture, run_oracle

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfsfix") is None,
    reason="mkntfs/ntfscp/ntfsfix are not installed",
)


def test_dirty_fixture_catalog_has_at_least_five_scenarios():
    assert len(SCENARIOS) >= 5


@needs_ntfs
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_replay_matches_external_ntfsfix_oracle(tmp_path, scenario):
    fixture = build_dirty_fixture(tmp_path, scenario)
    result = run_oracle(fixture, tmp_path)
    # Byte comparison of the recovered $MFT record and $Bitmap between two
    # independent executor runs: divergence of a single byte is a bug.
    assert result["mft_record_bytes_equal"] is True, result["ntfsfix_output"]
    assert result["bitmap_bytes_equal"] is True, result["ntfsfix_output"]
    assert result["target_content_consistent"] is True, result["ntfsfix_output"]
    if scenario != "mft-mirr-corrupt":
        # For every non-mirror scenario the replayed volume must be reported
        # clean by the external checker.  The mirror scenario may legitimately
        # leave mirror repair to ntfsfix; the divergence is recorded, not hidden.
        assert result["ntfsfix_check"] == "pass", result["ntfsfix_output"]


def test_source_integrity_flag_is_a_real_comparison(tmp_path):
    """Regression guard for the oracle tautology found on 2026-11-22.

    ``source_untouched`` used to be computed as
    ``sha256(source) == sha256(ours) or True`` — always truthy, so the
    evidence artifact could never report fixture tampering.  The flag is
    now a snapshot comparison; prove it can actually go false.
    """
    from tests.ntfs_oracle import _sha256, _source_integrity

    fixture = tmp_path / "fixture.img"
    fixture.write_bytes(b"PRE-RUN-SNAPSHOT" * 8)
    before = _sha256(fixture)

    untouched = _source_integrity(fixture, before)
    assert untouched["source_untouched"] is True
    assert untouched["source_hash_after"] == before

    fixture.write_bytes(b"TAMPERED-AFTER-RUN" * 8)
    touched = _source_integrity(fixture, before)
    assert touched["source_untouched"] is False
    assert touched["source_hash_after"] != touched["source_hash_before"]


def test_oracle_module_does_not_short_circuit_source_untouched():
    """The ``or True`` tautology must not silently come back."""
    import tests.ntfs_oracle as oracle_module

    source = Path(oracle_module.__file__).read_text(encoding="utf-8")
    assert "or True" not in source


@needs_ntfs
def test_oracle_evidence_covers_every_cataloged_scenario(tmp_path):
    from scripts.ntfs_oracle_evidence import build_evidence

    evidence = build_evidence(tmp_path)
    assert evidence["schema"] == "mountfile.ntfs-oracle-evidence/v1"
    assert len(evidence["scenarios"]) == len(SCENARIOS)
    assert evidence["unavailable_scenarios"] == 0
    assert all(
        item["verdict"] in ("pass", "divergence") for item in evidence["scenarios"]
    )
