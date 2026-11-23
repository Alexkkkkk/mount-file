# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Issue #510: targeted tests for the remaining open acceptance gaps.

``docs/replay-acceptance.json`` lists eight ``partial`` items (C3, C5, C6,
D1, D3, D5, D6, E1).  Every test here pins one concrete, already-implemented
behavior behind those gaps so a silent regression turns CI red instead of
waiting for the next manual tracker audit.  None of these tests claim a gap
is closed: the manifest owns the statuses and stays authoritative.

Run:  python -m pytest tests/test_issue_510_open_gaps.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.cross_platform_evidence import (  # noqa: E402
    EXPECTED_PLATFORMS,
    aggregate,
    expected_platforms,
)
from scripts import failure_matrix_artifact  # noqa: E402
from scripts import replay_nightly_evidence as nightly  # noqa: E402

from mountfile.ntfs_replay import (  # noqa: E402
    NTFS_REPLAY_FAULT_POINTS,
    OP_ADD_INDEX_ENTRY_ALLOCATION,
    OP_ADD_INDEX_ENTRY_ROOT,
    OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
    OP_DEALLOCATE_FILE_RECORD,
    OP_DELETE_INDEX_ENTRY_ALLOCATION,
    OP_DELETE_INDEX_ENTRY_ROOT,
    OP_INITIALIZE_FILE_RECORD,
    OP_SET_BITS_IN_NONRESIDENT_BITMAP,
    SUPPORTED_OPERATIONS,
    NtfsReplayError,
    gate_mft_record_reuse,
)
from mountfile.ntfs_mft import parse_ntfs_file_record_header  # noqa: E402
from tests.test_ntfs_mft_allocation import _raw_record  # noqa: E402

RECORD_SIZE = 512
MIRROR_OFFSET = 4 * 1024 * 1024
MFT_MIRROR_RECORDS = 4


# ---------------------------------------------------------------------------
# C5 — MFT record reuse gate: refusal paths before the first byte is written
# ---------------------------------------------------------------------------


class _ShortReadStream:
    """Stream whose ``$MFTMirr`` window is truncated mid-record."""

    def __init__(self, window: bytes) -> None:
        self._window = window
        self.pos = 0

    def seek(self, offset: int) -> int:
        self.pos = offset - MIRROR_OFFSET
        return offset

    def read(self, size: int) -> bytes:
        data = self._window[self.pos : self.pos + size]
        self.pos += len(data)
        return data


class _FakeStream(_ShortReadStream):
    pass


def _mirror_window(slot_records: dict[int, bytes]) -> bytes:
    filler = _raw_record(1, in_use=True)
    return b"".join(slot_records.get(i, filler) for i in range(MFT_MIRROR_RECORDS))


def _run_gate(
    *,
    record_number: int,
    raw_before: bytes,
    initialized_seq: int,
    stream,
) -> None:
    from types import SimpleNamespace

    gate_mft_record_reuse(
        stream,
        SimpleNamespace(record_size=RECORD_SIZE),
        SimpleNamespace(mft_mirror_offset=MIRROR_OFFSET),
        base_offset=0,
        record_number=record_number,
        raw_before=raw_before,
        initialized=parse_ntfs_file_record_header(
            _raw_record(initialized_seq, in_use=True)
        ),
        lsn=1,
    )


def test_gate_refuses_a_short_mftmirr_read_before_any_write():
    """A truncated mirror window must refuse allocation, never guess."""

    free = _raw_record(6, in_use=False)
    # Truncate the window so slot 2 itself is cut off mid-record.
    truncated = _mirror_window({2: free})[: 2 * RECORD_SIZE + 400]
    with pytest.raises(NtfsReplayError, match="short read from \\$MFTMirr"):
        _run_gate(
            record_number=2,
            raw_before=free,
            initialized_seq=7,
            stream=_ShortReadStream(truncated),
        )


def test_gate_refuses_a_structurally_invalid_mirror_record():
    """An unreadable mirror copy is not evidence of a free slot."""

    garbage = b"\xde\xad" * (RECORD_SIZE // 2)
    with pytest.raises(NtfsReplayError, match="reuse refused"):
        _run_gate(
            record_number=2,
            raw_before=_raw_record(6, in_use=False),
            initialized_seq=7,
            stream=_FakeStream(_mirror_window({2: garbage})),
        )


def test_gate_accepts_sequence_wrap_onto_0xffff_when_mirror_matches():
    """0xFFFE -> 0xFFFF is a legal continuation; only 0xFFFF is exhausted."""

    free = _raw_record(0xFFFE, in_use=False)
    _run_gate(
        record_number=2,
        raw_before=free,
        initialized_seq=0xFFFF,
        stream=_FakeStream(_mirror_window({2: free})),
    )


# ---------------------------------------------------------------------------
# C3 — allocation-, bitmap- and index-surgery stay outside the allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    (
        OP_INITIALIZE_FILE_RECORD,
        OP_DEALLOCATE_FILE_RECORD,
        OP_SET_BITS_IN_NONRESIDENT_BITMAP,
        OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
        OP_ADD_INDEX_ENTRY_ROOT,
        OP_ADD_INDEX_ENTRY_ALLOCATION,
        OP_DELETE_INDEX_ENTRY_ROOT,
        OP_DELETE_INDEX_ENTRY_ALLOCATION,
    ),
)
def test_allocation_bitmap_and_index_surgery_stay_outside_the_allowlist(op):
    """These ops stay refused until an independent checker backs them (C3)."""

    assert op not in SUPPORTED_OPERATIONS, (
        "0x%02x entered SUPPORTED_OPERATIONS: the C3 gap statement in "
        "docs/replay-acceptance.json must be revised before merge" % op
    )


# ---------------------------------------------------------------------------
# D1/D3 — the NTFS replay fault seam keeps its documented durable boundaries
# ---------------------------------------------------------------------------


def test_ntfs_replay_fault_points_cover_the_documented_boundaries():
    """Acceptance harnesses kill the process at these names; no silent renames."""

    documented = {
        "replay:redo:start",
        "replay:redo:done",
        "replay:undo:start",
        "replay:undo:done",
        "replay:journal:clean-pages",
        "replay:volume:dirty-flag",
        "replay:mirror:update",
        "replay:flush",
    }
    assert documented <= set(NTFS_REPLAY_FAULT_POINTS)


# ---------------------------------------------------------------------------
# D6 — every profile x state cell keeps a concrete test selector
# ---------------------------------------------------------------------------


def test_every_profile_state_cell_has_at_least_one_mapped_case():
    """A cell without a selector cannot run in a scheduled job, so it would
    silently stay grey; D6 requires all cells to be machine-addressable."""

    assert {"fat12", "fat16"} <= set(failure_matrix_artifact.TESTS)
    for profile in ("fat12", "fat16"):
        cells = failure_matrix_artifact.TESTS[profile]
        for state in failure_matrix_artifact.STATES:
            assert cells.get(state), f"cell {profile}x{state} lost its selector"
            for case in cells[state]:
                assert case["module"] and case["name"]


def test_fat_width_short_write_cells_pin_the_real_rollback_test():
    """The obsolete '3 cells unreachable' claim must not come back: both FAT
    widths map short_write/cancellation/process_kill to real node ids."""

    for profile in ("fat12", "fat16"):
        names = {
            case["name"]
            for case in failure_matrix_artifact.TESTS[profile]["short_write"]
        }
        assert any("fat1216_short_write" in name for name in names), profile


# ---------------------------------------------------------------------------
# E1 — a missing platform leg is a typed blocker, never a pass
# ---------------------------------------------------------------------------


def _report(platform_name: str, gate: str = "pass", summary: str = "pass") -> dict:
    return {
        "schema": "mountfile.native-read-evidence/v1",
        "runner": {"platform": platform_name, "python": "3.12.0"},
        "gate": {"verdict": gate},
        "summary": {
            "verdict": summary,
            "profiles_with_passed_read_contract": ["ext2-native-ro"],
        },
        "scope": {
            "legacy_fallback": "disabled",
            "independent_parity": "not_claimed_by_this_report",
        },
    }


def _write(root: Path, name: str, report: dict) -> None:
    (root / name).write_text(json.dumps(report), encoding="utf-8")


def test_missing_macos_leg_is_blocked_and_never_pass(monkeypatch, tmp_path):
    """E1: macOS evidence is not_collected today; the aggregate must keep
    reporting blocked instead of quietly passing a two-leg matrix."""

    monkeypatch.setenv("MOUNTFILE_EXPECTED_PLATFORMS", "linux,windows,macos")
    assert expected_platforms() == EXPECTED_PLATFORMS
    _write(tmp_path, "linux.json", _report("linux"))
    _write(tmp_path, "windows.json", _report("windows"))
    result = aggregate(tmp_path)
    assert result["verdict"] == "blocked"
    assert "missing_platform:macos" in result["blockers"]
    assert result["parity"] == "not_claimed"
    assert result["unavailable_checker_is_not_pass"] is True


# ---------------------------------------------------------------------------
# D2 / #387 — a green nightly streak still never claims acceptance
# ---------------------------------------------------------------------------


def _run(idx, *, status="completed", conclusion="success", event="schedule"):
    return {
        "id": idx,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "created_at": f"2026-11-{10 + idx:02d}T01:00:00Z",
        "head_sha": f"sha{idx}",
        "html_url": f"https://example.invalid/{idx}",
    }


def test_satisfied_nightly_streak_still_reports_acceptance_not_claimed():
    """Two consecutive scheduled successes satisfy the criterion of #387, but
    the report must keep ``acceptance: not_claimed`` — the criterion gates the
    evidence, not the product verdict."""

    report = nightly.evaluate([_run(1), _run(2)], required=nightly.REQUIRED_RUNS)
    assert report["criterion_satisfied"] is True
    assert report["acceptance"] == "not_claimed"


def test_unavailable_nightly_evidence_is_typed_and_never_green():
    """A transport that returns junk is unavailable, not failed and not pass."""

    report = nightly.build_report(
        "owner/name", transport=lambda url, token: {"workflow_runs": "junk"}
    )
    assert report["outcome"] == nightly.OUTCOME_UNAVAILABLE
    assert report["criterion_satisfied"] is False
    assert report["acceptance"] == "not_claimed"
