from __future__ import annotations

import pytest

from mountfile.ntfs_reader import NtfsRun

from mountfile.ntfs_logfile import (
    NtfsLogFileAnalysis,
    NtfsReplayOverlay,
    NtfsReplayOverlayError,
    OverlayProvenance,
    ReplayStep,
    ReplayTarget,
    replay_analysis_to_overlay,
    replay_analysis_on_resident_record,
    replay_analysis_on_nonresident_run,
    resolve_nonresident_run_target,
    resolve_resident_attribute_target,
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


def test_overlay_allows_adjacent_non_overlapping_targets() -> None:
    result, provenance = NtfsReplayOverlay(b"abcdef").apply(
        [
            step(kind="redo", payload=b"XY", target_vcn=0),
            step(kind="undo", payload=b"Z", target_vcn=1),
        ],
        {
            (128, 0): ReplayTarget(128, 0, 0, 2),
            (128, 1): ReplayTarget(128, 1, 2, 1),
        },
    )
    assert result == b"XYZdef"
    assert [item.offset for item in provenance] == [0, 2]


def test_overlay_handles_large_payload_without_per_byte_state() -> None:
    payload = b"q" * (1 << 20)
    result, provenance = NtfsReplayOverlay(bytes(1 << 20)).apply(
        [step(kind="redo", payload=payload)],
        {(128, 0): ReplayTarget(128, 0, 0, len(payload))},
    )
    assert result == payload
    assert provenance[0].length == len(payload)


def _analysis(*, redo=(), undo=(), truncated=False) -> NtfsLogFileAnalysis:
    return NtfsLogFileAnalysis(
        restart=object(),
        page_size=4096,
        first_page=0x4000,
        log_size=0x10000,
        scanned_bytes=0x10000,
        current_lsn=10,
        checkpoint_lsn=1,
        oldest_lsn=1,
        redo_plan=tuple(redo),
        undo_plan=tuple(undo),
        truncated=truncated,
    )


def test_analysis_executor_applies_committed_redo_before_active_undo() -> None:
    result, provenance = replay_analysis_to_overlay(
        _analysis(
            redo=(step(kind="redo", payload=b"RED", lsn=1),),
            undo=(step(kind="undo", payload=b"UND", lsn=2),),
        ),
        b"abcdef",
        {(128, 0): ReplayTarget(128, 0, 2, 3)},
    )
    assert result == b"abUNDf"
    assert [item.kind for item in provenance] == ["redo", "undo"]


def test_analysis_executor_refuses_truncated_log_before_overlay() -> None:
    with pytest.raises(NtfsReplayOverlayError, match="truncated"):
        replay_analysis_to_overlay(
            _analysis(redo=(step(kind="redo", payload=b"XYZ"),), truncated=True),
            b"abcdef",
            {(128, 0): target()},
        )


def test_analysis_executor_refuses_mft_and_index_operations() -> None:
    metadata_step = step(kind="redo", payload=b"XYZ")
    metadata_step = ReplayStep(
        lsn=metadata_step.lsn,
        transaction_id=metadata_step.transaction_id,
        kind=metadata_step.kind,
        operation=0x0C,  # AddIndexEntryRoot
        length=metadata_step.length,
        payload=metadata_step.payload,
        target_attr=metadata_step.target_attr,
        target_vcn=metadata_step.target_vcn,
    )
    with pytest.raises(NtfsReplayOverlayError, match="image-level metadata"):
        replay_analysis_to_overlay(
            _analysis(redo=(metadata_step,)),
            b"abcdef",
            {(128, 0): target()},
        )


def test_resident_target_resolver_returns_record_relative_range() -> None:
    from test_ntfs_reader import _record_with_attributes, _resident_attribute
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(
        _record_with_attributes((_resident_attribute(0x80, 7, b"abcdef"),))
    )
    resolved = resolve_resident_attribute_target(
        record, attribute_id=7, value_offset=2, length=3
    )
    assert resolved.target_attr == 7
    assert record.fixed_record[resolved.offset : resolved.offset + resolved.length] == b"cde"


def test_resident_target_resolver_refuses_nonresident_and_out_of_bounds_ranges() -> None:
    from test_ntfs_reader import (
        _nonresident_attribute,
        _record_with_attributes,
        _resident_attribute,
    )
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(
        _record_with_attributes((_nonresident_attribute(0x80, 7, 0, 0, 10, 8),))
    )
    with pytest.raises(NtfsReplayOverlayError, match="not resident"):
        resolve_resident_attribute_target(record, attribute_id=7, value_offset=0, length=1)

    resident = parse_ntfs_file_record_header(
        _record_with_attributes((_resident_attribute(0x80, 7, b"abc"),))
    )
    with pytest.raises(NtfsReplayOverlayError, match="exceeds"):
        resolve_resident_attribute_target(resident, attribute_id=7, value_offset=2, length=2)


def test_resident_record_executor_derives_target_and_applies_redo_then_undo() -> None:
    from test_ntfs_reader import _record_with_attributes, _resident_attribute
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(
        _record_with_attributes((_resident_attribute(0x80, 7, b"abcdef"),))
    )
    redo = step(kind="redo", payload=b"RED", target_attr=7, lsn=1)
    undo = step(kind="undo", payload=b"UND", target_attr=7, lsn=2)
    result, provenance = replay_analysis_on_resident_record(
        _analysis(redo=(redo,), undo=(undo,)), record
    )
    assert result != record.fixed_record
    assert [item.kind for item in provenance] == ["redo", "undo"]
    target = resolve_resident_attribute_target(
        record, attribute_id=7, value_offset=0, length=3
    )
    assert result[target.offset : target.offset + target.length] == b"UND"


def test_resident_record_executor_rejects_mixed_attribute_targets() -> None:
    from test_ntfs_reader import _record_with_attributes, _resident_attribute
    from mountfile.ntfs_mft import parse_ntfs_file_record_header

    record = parse_ntfs_file_record_header(
        _record_with_attributes(
            (_resident_attribute(0x80, 7, b"abc"), _resident_attribute(0x80, 8, b"xyz"))
        )
    )
    with pytest.raises(NtfsReplayOverlayError, match="one attribute target"):
        replay_analysis_on_resident_record(
            _analysis(
                redo=(
                    step(kind="redo", payload=b"AAA", target_attr=7),
                    step(kind="redo", payload=b"BBB", target_attr=8),
                )
            ),
            record,
        )


def test_nonresident_resolver_maps_one_complete_run() -> None:
    from mountfile.ntfs_reader import NtfsAttributeSummary, NtfsRun

    attribute = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        runs=(NtfsRun(0, 20, 2),),
        data_size=8192,
        attribute_id=9,
    )
    resolved = resolve_nonresident_run_target(
        attribute,
        volume_offset=4096,
        cluster_size=4096,
        byte_offset=4096,
        length=1024,
    )
    assert resolved.target_attr == 9
    assert resolved.target_vcn == 0
    assert resolved.offset == 4096 + 20 * 4096 + 4096


def test_nonresident_resolver_refuses_sparse_and_cross_run_ranges() -> None:
    from mountfile.ntfs_reader import NtfsAttributeSummary, NtfsRun

    sparse = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        runs=(NtfsRun(0, None, 2),),
        data_size=8192,
    )
    with pytest.raises(NtfsReplayOverlayError, match="sparse"):
        resolve_nonresident_run_target(
            sparse, volume_offset=0, cluster_size=4096, byte_offset=0, length=1
        )

    fragmented = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        runs=(NtfsRun(0, 20, 1), NtfsRun(1, 30, 1)),
        data_size=8192,
    )
    with pytest.raises(NtfsReplayOverlayError, match="run boundary"):
        resolve_nonresident_run_target(
            fragmented, volume_offset=0, cluster_size=4096, byte_offset=2048, length=4096
        )

    unsupported = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        runs=(NtfsRun(0, 20, 2),),
        data_size=8192,
        content_supported=False,
    )
    with pytest.raises(NtfsReplayOverlayError, match="unsupported"):
        resolve_nonresident_run_target(
            unsupported, volume_offset=0, cluster_size=4096, byte_offset=0, length=1
        )


@pytest.mark.parametrize(
    ("attribute", "message"),
    [
        (
            {
                "highest_vcn": 2,
                "runs": (NtfsRun(0, 20, 2),),
            },
            "highest VCN",
        ),
        (
            {
                "highest_vcn": 2,
                "runs": (NtfsRun(0, 20, 1), NtfsRun(2, 30, 1)),
            },
            "gap or overlap",
        ),
    ],
)
def test_nonresident_resolver_rejects_inconsistent_runlist(attribute, message) -> None:
    from mountfile.ntfs_reader import NtfsAttributeSummary

    candidate = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=attribute["highest_vcn"],
        runs=attribute["runs"],
        data_size=8192,
    )
    with pytest.raises(NtfsReplayOverlayError, match=message):
        resolve_nonresident_run_target(
            candidate, volume_offset=0, cluster_size=4096, byte_offset=0, length=1
        )


def test_nonresident_run_executor_maps_update_and_applies_redo_then_undo() -> None:
    from mountfile.ntfs_reader import NtfsAttributeSummary, NtfsRun

    attribute = NtfsAttributeSummary(
        0x80,
        72,
        True,
        "",
        lowest_vcn=0,
        highest_vcn=1,
        runs=(NtfsRun(0, 20, 2),),
        data_size=8192,
        attribute_id=9,
    )
    redo = ReplayStep(1, 7, "redo", 0x08, 3, b"RED", 9, 0)
    undo = ReplayStep(2, 7, "undo", 0x08, 3, b"UND", 9, 0)
    source = bytearray(100 * 4096)
    result, provenance = replay_analysis_on_nonresident_run(
        _analysis(redo=(redo,), undo=(undo,)),
        source,
        attribute,
        volume_offset=4096,
        cluster_size=4096,
        byte_offset=4096,
    )
    physical = 4096 + 20 * 4096 + 4096
    assert result[physical : physical + 3] == b"UND"
    assert [item.kind for item in provenance] == ["redo", "undo"]
