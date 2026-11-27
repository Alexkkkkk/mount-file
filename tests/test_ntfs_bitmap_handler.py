from __future__ import annotations

import io
import struct

import pytest

from mountfile.ntfs_logfile import LogRecord
from mountfile.ntfs_reader import NtfsAttributeSummary, NtfsRun
from mountfile.ntfs_replay import (
    OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
    OP_SET_BITS_IN_NONRESIDENT_BITMAP,
    NtfsReplayError,
    _apply_stream_operation,
)


def _record(operation: int, undo_operation: int | None = None) -> LogRecord:
    return LogRecord(
        lsn=1,
        byte_offset=0,
        page_index=0,
        record_type=0,
        transaction_id=1,
        sequence_number=1,
        client_index=0,
        prev_lsn=0,
        undo_next_lsn=0,
        flags=0,
        data_length=0,
        redo_op=operation,
        undo_op=operation if undo_operation is None else undo_operation,
        redo_data=b"",
        undo_data=b"",
        target_attr=0xB0,
        lcns_follow=1,
        record_off=0,
        attr_off=0,
        cluster_off=0,
        target_vcn=0,
        page_lcns=(0,),
    )


def _attribute(
    data_size: int = 512, name: str = "$Bitmap", type_code: int = 0xB0
) -> NtfsAttributeSummary:
    return NtfsAttributeSummary(
        type_code=type_code,
        length=0,
        nonresident=True,
        name=name,
        runs=(NtfsRun(vcn=0, lcn=0, length=1),),
        data_size=data_size,
        allocated_size=512,
    )


@pytest.mark.parametrize(
    ("operation", "initial", "expected"),
    [
        (
            OP_SET_BITS_IN_NONRESIDENT_BITMAP,
            bytes((0b11110000, 0b10101010)),
            bytes((0b11111000, 0b10101011)),
        ),
        (
            OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
            bytes((0b11111111, 0b11111111)),
            bytes((0b00000111, 0b11111100)),
        ),
    ],
)
def test_misaligned_bitmap_range_updates_requested_bits_and_preserves_neighbors(
    operation: int, initial: bytes, expected: bytes
) -> None:
    image = bytearray(512)
    image[:2] = initial
    stream = io.BytesIO(image)
    payload = struct.pack("<II", 3, 7)  # bits 3..9, crossing a byte boundary

    result = _apply_stream_operation(
        stream,
        None,
        _attribute(),
        _record(operation),
        payload,
        cluster_size=512,
        base_offset=0,
        redo=True,
    )

    assert result == 1
    assert stream.getvalue()[:2] == expected
    assert stream.getvalue()[2:] == bytes(510)


def test_bitmap_redo_then_undo_restores_exact_stream_bytes() -> None:
    """The same bounded range must round-trip through redo and undo."""
    initial = bytes([0x00, 0x00, *([0xA5] * 510)])
    stream = io.BytesIO(initial)
    payload = struct.pack("<II", 3, 7)
    record = _record(
        OP_SET_BITS_IN_NONRESIDENT_BITMAP,
        OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
    )

    assert (
        _apply_stream_operation(
            stream,
            None,
            _attribute(),
            record,
            payload,
            cluster_size=512,
            base_offset=0,
            redo=True,
        )
        == 1
    )
    assert stream.getvalue() != initial

    assert (
        _apply_stream_operation(
            stream,
            None,
            _attribute(),
            record,
            payload,
            cluster_size=512,
            base_offset=0,
            redo=False,
        )
        == 1
    )
    assert stream.getvalue() == initial


def test_bitmap_handler_refuses_out_of_bounds_range_without_writing() -> None:
    image = bytearray([0xA5] * 512)
    stream = io.BytesIO(image)
    before = stream.getvalue()

    with pytest.raises(NtfsReplayError, match="stream range exceeds"):
        _apply_stream_operation(
            stream,
            None,
            _attribute(data_size=1),
            _record(OP_SET_BITS_IN_NONRESIDENT_BITMAP),
            struct.pack("<II", 3, 7),
            cluster_size=512,
            base_offset=0,
            redo=True,
        )

    assert stream.getvalue() == before


def test_bitmap_handler_refuses_unrecognized_bitmap_stream_name() -> None:
    image = bytearray([0xA5] * 512)
    stream = io.BytesIO(image)
    before = stream.getvalue()

    with pytest.raises(NtfsReplayError, match="unrecognized bitmap stream"):
        _apply_stream_operation(
            stream,
            None,
            _attribute(name="$UNTRUSTED"),
            _record(OP_SET_BITS_IN_NONRESIDENT_BITMAP),
            struct.pack("<II", 3, 7),
            cluster_size=512,
            base_offset=0,
            redo=True,
        )

    assert stream.getvalue() == before


@pytest.mark.parametrize("name", ["$bitmap", "$I30", "$i30"])
def test_bitmap_handler_accepts_only_known_metadata_stream_names(name: str) -> None:
    image = bytearray(512)
    stream = io.BytesIO(image)

    result = _apply_stream_operation(
        stream,
        None,
        _attribute(name=name),
        _record(OP_SET_BITS_IN_NONRESIDENT_BITMAP),
        struct.pack("<II", 0, 1),
        cluster_size=512,
        base_offset=0,
        redo=True,
    )

    assert result == 1
    assert stream.getvalue()[0] == 0x01


@pytest.mark.parametrize(
    ("name", "type_code"),
    [("$Bitmap", 0x80), ("", 0xB0)],
)
def test_bitmap_handler_refuses_non_metadata_identity_without_writing(
    name: str, type_code: int
) -> None:
    image = bytearray([0xA5] * 512)
    stream = io.BytesIO(image)
    before = stream.getvalue()

    with pytest.raises(NtfsReplayError, match="(non-bitmap|unrecognized bitmap)"):
        _apply_stream_operation(
            stream,
            None,
            _attribute(name=name, type_code=type_code),
            _record(OP_SET_BITS_IN_NONRESIDENT_BITMAP),
            struct.pack("<II", 3, 7),
            cluster_size=512,
            base_offset=0,
            redo=True,
        )

    assert stream.getvalue() == before
