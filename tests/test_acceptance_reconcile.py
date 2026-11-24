# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Contract tests for ``scripts/acceptance_reconcile.py``.

The reconcile gate exists because the two #510 gates each compare a claim with
itself and therefore agree while the tree disagrees (manifest 23 verified,
probes 15).  These tests pin the four things that make it a gate rather than
another report: the strength order, the per-id join, the line between "the
probe looked and saw less" and "the probe could not look", and the baseline
rules inherited from ``docs/acceptance-known-drift.txt``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "acceptance_reconcile.py"
BASELINE = REPO_ROOT / "docs" / "acceptance-claim-probe-drift.txt"
MANIFEST = REPO_ROOT / "docs" / "replay-acceptance.json"

pytestmark = pytest.mark.unit


def _load_module():
    spec = importlib.util.spec_from_file_location("acceptance_reconcile", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconcile_script = _load_module()


# --------------------------------------------------------------------------
# strength order
# --------------------------------------------------------------------------
def test_strength_ranks_verified_highest_and_unproven_lowest() -> None:
    strength = reconcile_script.strength
    assert strength("verified") > strength("partial") > strength("blocked")
    assert strength("blocked") == strength("not_started") + 1
    assert strength("not_exercised") == strength("not_started")


def test_unknown_status_has_no_strength() -> None:
    assert reconcile_script.strength("fabricated") == -1


# --------------------------------------------------------------------------
# per-id join
# --------------------------------------------------------------------------
def test_claim_stronger_than_probe_is_a_contradiction() -> None:
    contradictions, understated, unprobed, inconclusive = reconcile_script.reconcile(
        {"C3": "verified"}, {"C3": "partial"}
    )
    assert contradictions == [("C3", "verified", "partial")]
    assert understated == []
    assert unprobed == []
    assert inconclusive == []


def test_probe_stronger_than_claim_is_only_understated() -> None:
    contradictions, understated, _, _ = reconcile_script.reconcile(
        {"C5": "partial"}, {"C5": "verified"}
    )
    assert contradictions == []
    assert understated == [("C5", "partial", "verified")]


def test_equal_statuses_reconcile_cleanly() -> None:
    contradictions, understated, unprobed, inconclusive = reconcile_script.reconcile(
        {"B1": "verified", "C4": "blocked"}, {"B1": "verified", "C4": "blocked"}
    )
    assert (contradictions, understated, unprobed, inconclusive) == ([], [], [], [])


def test_claimed_id_without_a_probe_is_reported() -> None:
    contradictions, _, unprobed, _ = reconcile_script.reconcile(
        {"D9": "verified"}, {"D1": "partial"}
    )
    assert contradictions == []
    assert unprobed == ["D9"]


def test_items_are_sorted_numerically_not_lexically() -> None:
    contradictions, _, _, _ = reconcile_script.reconcile(
        {"B10": "verified", "B2": "verified"},
        {"B10": "partial", "B2": "partial"},
    )
    assert [c[0] for c in contradictions] == ["B2", "B10"]


def test_not_exercised_probe_cannot_back_a_verified_claim() -> None:
    contradictions, _, _, _ = reconcile_script.reconcile(
        {"D1": "verified"}, {"D1": "not_exercised"}
    )
    assert contradictions == [("D1", "verified", "not_exercised")]


# --------------------------------------------------------------------------
# "could not look" is not "looked and saw less"
# --------------------------------------------------------------------------
def test_unavailable_probe_is_inconclusive_not_a_contradiction() -> None:
    """A missing e2fsck / release tag is an absence of observation."""
    contradictions, _, _, inconclusive = reconcile_script.reconcile(
        {"B2": "verified", "A2": "verified"},
        {"B2": "unavailable", "A2": "unavailable"},
    )
    assert contradictions == []
    assert [i[0] for i in inconclusive] == ["A2", "B2"]


def test_unavailable_probe_cannot_hide_a_stale_waiver() -> None:
    """Inconclusive ids are still kept out of the baseline's drift set."""
    contradictions, _, _, inconclusive = reconcile_script.reconcile(
        {"D5": "partial"},
        {"D5": "unavailable"},
    )
    assert contradictions == []
    assert inconclusive == [("D5", "partial", "unavailable")]


def test_known_status_with_a_real_observation_is_still_a_contradiction() -> None:
    """``unavailable`` is the only status allowed to mean "no observation"."""
    contradictions, _, _, inconclusive = reconcile_script.reconcile(
        {"B2": "verified"}, {"B2": "partial"}
    )
    assert contradictions == [("B2", "verified", "partial")]
    assert inconclusive == []


def test_observation_object_carries_the_detail_and_tier() -> None:
    observation = reconcile_script.Observation(
        status="unavailable", detail="e2fsck unavailable", tier="behaviour"
    )
    assert observation.looked is False
    assert observation.crashed is False


def test_crashed_probe_is_not_reported_as_no_observation() -> None:
    """``BlockCheck.run`` swallows exceptions; the gate must not read that as ok."""
    observations = {
        "B2": reconcile_script.Observation(
            status="unavailable",
            detail="probe error: FileNotFoundError: git",
        ),
        "D5": reconcile_script.Observation(
            status="unavailable", detail="e2fsck unavailable"
        ),
    }
    assert reconcile_script.crashed_probes(observations) == ["B2"]


def test_the_detail_of_an_inconclusive_id_reaches_the_payload() -> None:
    payload_reason = reconcile_script._detail_of(
        reconcile_script.Observation(
            status="unavailable", detail="mkfs.ext4/e2fsck missing"
        )
    )
    assert payload_reason == "mkfs.ext4/e2fsck missing"


# --------------------------------------------------------------------------
# baseline rules: same file, same meaning as the gap gate
# --------------------------------------------------------------------------
def test_baseline_is_separate_from_the_count_drift_baseline() -> None:
    """Count drift and claim/probe drift are waived in different files."""
    assert BASELINE.name == "acceptance-claim-probe-drift.txt"
    assert (REPO_ROOT / "docs" / "acceptance-known-drift.txt").is_file()
    assert (REPO_ROOT / "docs" / "acceptance-known-drift.txt") != BASELINE


def test_baseline_file_exists_and_reuses_the_gap_gate_format() -> None:
    assert BASELINE.is_file()
    entries = reconcile_script.load_baseline(BASELINE)
    assert isinstance(entries, dict)
    for ident in entries:
        assert ident and ident[0].isalpha()


def test_baseline_rejects_a_duplicate_id(tmp_path: Path) -> None:
    path = tmp_path / "known-drift.txt"
    path.write_text("C3  # one\nC3  # two\n", encoding="utf-8")
    with pytest.raises(ValueError):
        reconcile_script.load_baseline(path)


def test_every_waived_id_is_a_structure_probe() -> None:
    """The waiver only ever covers a probe that cannot leave ``partial``."""
    from mountfile.acceptance.blocks import BLOCK_CHECKS, STRUCTURE

    tiers = {check.ident: check.tier for check in BLOCK_CHECKS}
    for ident in reconcile_script.load_baseline(BASELINE):
        assert tiers.get(ident) == STRUCTURE, f"{ident} is not a structure probe"


# --------------------------------------------------------------------------
# the live tree
# --------------------------------------------------------------------------
def test_claims_and_probes_cover_the_same_ids() -> None:
    claims = reconcile_script.load_claims(MANIFEST)
    observed = reconcile_script.load_observed(REPO_ROOT, MANIFEST)
    assert set(claims) == set(observed)


def test_no_contradiction_outside_the_baseline() -> None:
    claims = reconcile_script.load_claims(MANIFEST)
    observed = reconcile_script.load_observed(REPO_ROOT, MANIFEST)
    contradictions, _, unprobed, _ = reconcile_script.reconcile(claims, observed)
    baseline = reconcile_script.load_baseline(BASELINE)

    novel = [c[0] for c in contradictions if c[0] not in baseline]
    stale = [ident for ident in baseline if ident not in {c[0] for c in contradictions}]

    assert unprobed == [], f"claimed items without a probe: {unprobed}"
    assert novel == [], f"claim stronger than evidence: {novel}"
    assert stale == [], f"baseline entries that no longer drift: {stale}"


def test_no_probe_crashed_on_the_live_tree() -> None:
    observations = reconcile_script.load_observations(REPO_ROOT, MANIFEST)
    assert reconcile_script.crashed_probes(observations) == []


def test_check_passes_on_the_live_tree() -> None:
    """End-to-end: the real tree has no contradiction outside the baseline.

    Covers both environments this gate runs in: on a Linux runner with
    ``e2fsck`` present only ``A2`` is inconclusive, and where the tools are
    missing ``B2``/``D5`` join it - none of the three is a contradiction.
    """
    assert reconcile_script.main(["--root", str(REPO_ROOT), "--check"]) == 0


def test_stale_baseline_entry_fails_the_gate(tmp_path: Path) -> None:
    """A waiver that no longer drifts must fail, not silently tolerate."""
    baseline = tmp_path / "stale.txt"
    baseline.write_text("Z9  # уже не расходится\n", encoding="utf-8")
    code = reconcile_script.main(
        ["--root", str(REPO_ROOT), "--check", "--baseline", str(baseline)]
    )
    assert code == 1


def test_payload_marks_waived_ids_as_known_not_as_contradictions() -> None:
    claims = reconcile_script.load_claims(MANIFEST)
    observed = reconcile_script.load_observed(REPO_ROOT, MANIFEST)
    contradictions, _, _, _ = reconcile_script.reconcile(claims, observed)
    baseline = reconcile_script.load_baseline(BASELINE)
    waived = {c[0] for c in contradictions}
    assert waived <= set(baseline), f"unwaived contradiction: {waived - set(baseline)}"


def test_missing_manifest_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(reconcile_script.ReconcileError):
        reconcile_script.load_claims(tmp_path / "absent.json")


def test_script_is_documented_in_the_workflow() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "status-sync.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/acceptance_reconcile.py --check" in text
