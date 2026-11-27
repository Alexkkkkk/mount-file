# SPDX-License-Identifier: MIT
"""Unit tests for the C6 draft classifier (pure logic, locally provable)."""

from scripts.ntfs_diff_classifier import (
    classify_diffs,
    parity_verdict,
)


def test_offsets_inside_the_replay_write_set_are_replay_relevant():
    write_set = {"mft": ((0x1000, 0x2000),)}
    result = classify_diffs("mft", [0x1000, 0x10FF, 0x1FFF], write_set)
    assert result["replay_relevant_count"] == 3
    assert result["replay_relevant_offsets"] == [0x1000, 0x10FF, 0x1FFF]


def test_offsets_outside_any_write_set_are_outside_scope():
    write_set = {"mft": ((0x1000, 0x2000),)}
    result = classify_diffs("mft", [0x2000, 0xABC], write_set)
    assert result["replay_relevant_count"] == 0
    assert result["outside_scope_offsets"] == [0xABC, 0x2000]


def test_missing_write_set_degrades_to_outside_scope_never_to_a_pass():
    result = classify_diffs("mft", [1, 2, 3], {})
    assert result["replay_relevant_count"] == 0
    assert result["outside_scope_offsets"] == [1, 2, 3]
    assert parity_verdict([result]) != "pass"


def test_checker_lifecycle_only_via_the_explicit_allowlist():
    from scripts.ntfs_diff_classifier import CHECKER_LIFECYCLE_REGIONS

    assert CHECKER_LIFECYCLE_REGIONS == {}, "allowlist stays empty until E1 lands"


def test_parity_verdict_blocks_on_any_replay_relevant_byte():
    inside = classify_diffs("bitmap", [0x40], {"bitmap": ((0x00, 0x100),)})
    outside = classify_diffs("bitmap", [0x400], {"bitmap": ((0x00, 0x100),)})
    assert parity_verdict([outside]) == "pass"
    assert parity_verdict([outside, inside]) == "parity_bug"
    assert parity_verdict([]) == "pass"
