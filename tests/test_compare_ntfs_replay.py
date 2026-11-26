# SPDX-License-Identifier: MIT
"""Unit tests for the typed native replay comparison status."""

from scripts.compare_ntfs_replay import NOT_APPLICABLE_REASON, replay_comparison_status


def _streams(**overrides):
    base = {
        "mft": {"byte_identical": True},
        "mftmirr": {"byte_identical": True},
        "bitmap": {"byte_identical": True},
        "logfile": {"byte_identical": True},
    }
    base.update(overrides)
    return base


def _no_restart_replay():
    """Shape of NtfsReplayResult.as_dict() for a restart-page-less $LogFile."""
    return {
        "changed": False,
        "clean_restart_pages": 0,
        "mirror_updated": False,
        "records_written": 0,
        "redo_applied": 0,
        "redo_start": 0,
        "stream_writes": 0,
        "undo_applied": 0,
        "volume_flags_after": 128,
        "volume_flags_before": 128,
        "notes": ["$LogFile has no restart page; repaired $Volume/$MFTMirr only"],
    }


def test_no_restart_page_is_not_applicable_even_when_all_streams_match():
    assert replay_comparison_status(_no_restart_replay(), _streams()) == (
        "not_applicable"
    )


def test_replay_with_records_and_identical_streams_is_a_pass():
    replay = _no_restart_replay()
    replay["clean_restart_pages"] = 1
    replay["records_written"] = 1
    replay["redo_applied"] = 1
    replay["notes"] = ["replayed one committed record"]
    assert replay_comparison_status(replay, _streams()) == "pass"


def test_no_restart_page_log_is_typed_not_applicable_not_mismatch():
    streams = _streams(mft={"byte_identical": False}, logfile={"byte_identical": False})
    assert replay_comparison_status(_no_restart_replay(), streams) == "not_applicable"


def test_fabricated_records_with_differing_streams_stay_a_mismatch():
    replay = _no_restart_replay()
    replay["records_written"] = 3
    replay["notes"] = ["replayed 3 records from restart tables"]
    streams = _streams(mft={"byte_identical": False})
    assert replay_comparison_status(replay, streams) == "mismatch"


def test_replay_that_wrote_streams_is_never_not_applicable():
    replay = _no_restart_replay()
    replay["stream_writes"] = 2
    streams = _streams(mft={"byte_identical": False})
    assert replay_comparison_status(replay, streams) == "mismatch"


def test_mirror_repair_alone_does_not_qualify_as_not_applicable_reason():
    """changed/mirror flags stay informational; only the counters decide."""
    replay = _no_restart_replay()
    replay["mirror_updated"] = True
    streams = _streams(mft={"byte_identical": False})
    assert replay_comparison_status(replay, streams) == "not_applicable"
    assert "fabricates no records" in NOT_APPLICABLE_REASON
