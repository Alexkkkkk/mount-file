from __future__ import annotations

import pytest

from mountfile.ntfs_logfile import (
    NtfsReplayOverlay,
    NtfsReplayOverlayError,
    OverlayProvenance,
    ReplayStep,
    ReplayTarget,
)


def step(
    *,
    kind: str,
    payload: bytes,
    lsn: int = 1,
    target_attr: int = 128,
    target_vcn: int = 0,
) -> ReplayStep:
    return ReplayStep(
        lsn=lsn,
        transaction_id=7,
        kind=kind,
        operation=0x07,
        length=len(payload),
        payload=payload,
        target_attr=target_attr,
        target_vcn=target_vcn,
    )


def target(length: int = 3) -> ReplayTarget:
    return ReplayTarget(target_attr=128, target_vcn=0, offset=2, length=length)


def test_overlay_applies_explicit_redo_and_returns_provenance() -> None:
    source = b"abcdef"
    result, provenance = NtfsReplayOverlay(source).apply(
        [step(kind="redo", payload=b"XYZ")], {(128, 0): target()}
    )
    assert source == b"abcdef"
    assert result == b"abXYZf"
    assert provenance == (
        OverlayProvenance(
            lsn=1,
            transaction_id=7,
            kind="redo",
            operation="UpdateResidentValue",
            target_attr=128,
            target_vcn=0,
            offset=2,
            length=3,
        ),
    )


def test_overlay_keeps_undo_payload_separate_from_redo() -> None:
    result, provenance = NtfsReplayOverlay(b"abcdef").apply(
        [
            step(kind="redo", payload=b"RED", lsn=1),
            step(kind="undo", payload=b"UND", lsn=2),
        ],
        {(128, 0): ReplayTarget(128, 0, 2, 3)},
    )
    assert result == b"abUNDf"
    assert [item.kind for item in provenance] == ["redo", "undo"]


def test_overlay_validates_every_step_before_mutating_result() -> None:
    with pytest.raises(NtfsReplayOverlayError, match="unresolved"):
        NtfsReplayOverlay(b"abcdef").apply(
            [
                step(kind="redo", payload=b"XYZ"),
                step(kind="redo", payload=b"NO", target_attr=129),
            ],
            {(128, 0): target()},
        )


def test_overlay_rejects_overlap_and_out_of_bounds_targets() -> None:
    with pytest.raises(NtfsReplayOverlayError, match="overlapping"):
        NtfsReplayOverlay(b"abcdef").apply(
            [
                step(kind="redo", payload=b"XYZ"),
                step(kind="undo", payload=b"123", target_vcn=1),
            ],
            {
                (128, 0): ReplayTarget(128, 0, 0, 3),
                (128, 1): ReplayTarget(128, 1, 2, 3),
            },
        )
    with pytest.raises(NtfsReplayOverlayError, match="outside"):
        NtfsReplayOverlay(b"abcdef").apply(
            [step(kind="redo", payload=b"XYZ")],
            {(128, 0): ReplayTarget(128, 0, 5, 3)},
        )
