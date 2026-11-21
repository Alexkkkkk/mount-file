# SPDX-License-Identifier: MIT
"""Runtime allocation-gate tests: MFT record reuse through the planner (#510 C5).

The gate sits in the production ``native_ntfs_recover`` path in front of
every ``InitializeFileRecordSegment`` write: a free (not-in-use) MFT slot may
only be initialized when its ``$MFTMirr`` copy is byte-identical and the log
reference sequence continues the pre-image.  The fixture-based regression
runs the full replay on a real ``mkntfs`` crash image; it skips without the
ntfs-3g tools and a skip is never reported as a pass.
"""

import shutil
from types import SimpleNamespace

import pytest

from mountfile.ntfs_mft import parse_ntfs_file_record_header
from mountfile.ntfs_replay import (
    MFT_MIRROR_RECORDS,
    NtfsReplayError,
    gate_mft_record_reuse,
    native_ntfs_recover,
)
from tests.test_ntfs_mft_allocation import _raw_record

RECORD_SIZE = 512
MIRROR_OFFSET = 4 * 1024 * 1024
MIRROR_WINDOW = 4 * RECORD_SIZE


class _FakeStream:
    """Minimal seek/read stream backed by one in-memory mirror window."""

    def __init__(self, mirror_window: bytes) -> None:
        assert len(mirror_window) == MIRROR_WINDOW
        self._window = mirror_window
        self.pos = 0

    def seek(self, offset: int) -> int:
        # The gate computes absolute image offsets; the fake stream only
        # exposes the $MFTMirr window, so translate to window-relative bytes.
        self.pos = offset - MIRROR_OFFSET
        return offset

    def read(self, size: int) -> bytes:
        data = self._window[self.pos : self.pos + size]
        self.pos += len(data)
        return data


def _mirror_window(slot_records: dict[int, bytes]) -> bytes:
    """Build the full 4-slot $MFTMirr window with the given slot contents."""
    filler = _raw_record(1, in_use=True)
    window = b"".join(slot_records.get(i, filler) for i in range(MFT_MIRROR_RECORDS))
    assert len(window) == MIRROR_WINDOW
    return window


def _run_gate(
    *,
    record_number: int,
    raw_before: bytes,
    initialized_seq: int,
    mirror_window: bytes,
) -> None:
    stream = _FakeStream(mirror_window)
    mft = SimpleNamespace(record_size=RECORD_SIZE)
    boot = SimpleNamespace(mft_mirror_offset=MIRROR_OFFSET)
    initialized = parse_ntfs_file_record_header(
        _raw_record(initialized_seq, in_use=True)
    )
    gate_mft_record_reuse(
        stream,
        mft,
        boot,
        base_offset=0,
        record_number=record_number,
        raw_before=raw_before,
        initialized=initialized,
        lsn=1,
    )


def test_free_record_with_matching_mirror_passes_the_gate():
    free = _raw_record(6, in_use=False)
    _run_gate(
        record_number=2,
        raw_before=free,
        initialized_seq=7,
        mirror_window=_mirror_window({2: free}),
    )


def test_mirror_content_mismatch_refuses_before_write():
    with pytest.raises(NtfsReplayError, match="reuse refused"):
        _run_gate(
            record_number=2,
            raw_before=_raw_record(6, in_use=False),
            initialized_seq=7,
            mirror_window=_mirror_window({2: _raw_record(9, in_use=False)}),
        )


def test_sequence_discontinuity_refuses_before_write():
    with pytest.raises(NtfsReplayError, match="reuse refused"):
        _run_gate(
            record_number=2,
            raw_before=_raw_record(6, in_use=False),
            initialized_seq=9,
            mirror_window=_mirror_window({2: _raw_record(6, in_use=False)}),
        )


def test_allocation_outside_mirror_window_is_refused():
    with pytest.raises(NtfsReplayError, match="outside"):
        _run_gate(
            record_number=MFT_MIRROR_RECORDS,
            raw_before=_raw_record(6, in_use=False),
            initialized_seq=7,
            mirror_window=_mirror_window({}),
        )


def test_in_use_pre_image_keeps_existing_behavior_without_mirror_read():
    # Re-initialization of a live record is not allocation: the gate must not
    # consult (or require) the mirror at all - hence an empty window here.
    _run_gate(
        record_number=2,
        raw_before=_raw_record(6, in_use=True),
        initialized_seq=7,
        mirror_window=b"",
    )


def test_unreadable_pre_image_keeps_existing_behavior_without_mirror_read():
    _run_gate(
        record_number=2,
        raw_before=b"\x00" * RECORD_SIZE,
        initialized_seq=7,
        mirror_window=b"",
    )


needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="mkntfs/ntfscp are not installed",
)


@needs_ntfs
def test_runtime_replay_passes_the_gate_on_a_crash_fixture(tmp_path):
    from tests.ntfs_crash_fixture import make_crash_fixture

    fixture = make_crash_fixture(tmp_path)
    result = native_ntfs_recover(str(fixture.image))
    assert result.changed is True
    assert result.redo_applied + result.undo_applied > 0
