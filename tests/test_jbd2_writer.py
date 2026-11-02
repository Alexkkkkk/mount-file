import struct
from types import SimpleNamespace

import pytest

from mountfile.ext3_reader import NativeExt3Image
from mountfile.jbd2_writer import (
    JBD2WriteProfileError,
    encode_classic_jbd2_transaction,
    plan_classic_jbd2_ring_transaction,
)

_MAGIC = 0xC03B3998


def test_encodes_bounded_descriptor_data_revoke_and_commit_records():
    first_data = b"A" * 1024
    second_data = b"B" * 1024
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=17,
        filesystem_blocks=128,
        writes={12: first_data, 8: second_data},
        revokes=[31, 32],
        journal_blocks=[40, 41],
    )

    assert len(records) == 5
    assert struct.unpack_from(">III", records[0]) == (_MAGIC, 1, 17)
    assert struct.unpack_from(">II", records[0], 12) == (8, 2)
    assert struct.unpack_from(">II", records[0], 20) == (12, 10)
    assert records[1:3] == (second_data, first_data)
    assert struct.unpack_from(">IIII", records[3]) == (_MAGIC, 5, 17, 24)
    assert struct.unpack_from(">II", records[3], 16) == (31, 32)
    assert struct.unpack_from(">III", records[4]) == (_MAGIC, 2, 17)
    assert all(len(record) == 1024 for record in records)


def test_encodes_revoke_only_transaction():
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=3,
        filesystem_blocks=64,
        writes={},
        revokes=[9],
    )

    assert len(records) == 2
    assert struct.unpack_from(">IIII", records[0]) == (_MAGIC, 5, 3, 20)
    assert struct.unpack_from(">I", records[0], 16)[0] == 9
    assert struct.unpack_from(">III", records[1]) == (_MAGIC, 2, 3)


def test_serialized_transaction_passes_existing_classic_jbd2_validator():
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=23,
        filesystem_blocks=128,
        writes={8: b"A" * 1024, 12: b"B" * 1024},
        revokes=[31],
    )
    reader = NativeExt3Image.__new__(NativeExt3Image)
    reader.block_size = 1024
    reader.blocks_count = 128
    reader.profile = SimpleNamespace(filesystem_uuid=b"0123456789abcdef")

    def read_record(_inode, logical, _journal_blocks):
        return records[logical]

    reader._journal_logical_block = read_record
    terminal = reader._validate_jbd2_transaction(
        None, len(records), 0, 23, journal_data_blocks=None
    )

    assert terminal == 4


def test_ring_planner_places_transaction_across_wrap_boundary():
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=4,
        filesystem_blocks=64,
        writes={8: b"A" * 1024},
        revokes=[9],
    )

    plan = plan_classic_jbd2_ring_transaction(
        first=1,
        max_length=6,
        head=4,
        tail=4,
        records=records,
        block_size=1024,
        empty=True,
    )

    assert [position for position, _block in plan.writes] == [4, 5, 1, 2]
    assert [block for _position, block in plan.writes] == list(records)
    assert plan.next_head == 3
    assert plan.used_blocks == 4
    assert plan.free_blocks == 0


def test_ring_planner_accounts_for_live_records_before_placement():
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=5,
        filesystem_blocks=64,
        writes={},
        revokes=[9],
    )

    plan = plan_classic_jbd2_ring_transaction(
        first=1,
        max_length=6,
        head=5,
        tail=3,
        records=records,
        block_size=1024,
    )

    assert [position for position, _block in plan.writes] == [5, 1]
    assert plan.next_head == 2
    assert plan.used_blocks == 4
    assert plan.free_blocks == 0


@pytest.mark.parametrize(
    ("head", "tail", "empty", "records"),
    [
        (4, 4, False, (b"A" * 1024, b"B" * 1024)),
        (5, 3, False, (b"A" * 1024, b"B" * 1024, b"C" * 1024)),
        (1, 1, True, (b"A" * 1024,)),
        (4, 4, True, (b"bad",)),
    ],
)
def test_ring_planner_rejects_ambiguous_overfull_or_malformed_state(
    head, tail, empty, records
):
    with pytest.raises(JBD2WriteProfileError):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=6,
            head=head,
            tail=tail,
            records=records,
            block_size=1024,
            empty=empty,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"block_size": 512},
        {"sequence": 0},
        {"filesystem_blocks": 0},
        {"writes": {0: b"A" * 1024}},
        {"writes": {64: b"A" * 1024}},
        {"writes": {8: b"short"}},
        {"writes": {8: b"A" * 1024}, "revokes": [8]},
        {"writes": {8: b"A" * 1024}, "journal_blocks": [8]},
        {"writes": {}, "revokes": [9, 9]},
        {
            "filesystem_blocks": 128,
            "writes": {index: b"A" * 1024 for index in range(1, 128)},
        },
    ],
)
def test_rejects_unsupported_or_unsafe_transaction_shapes(kwargs):
    options = {
        "block_size": 1024,
        "sequence": 1,
        "filesystem_blocks": 64,
        "writes": {8: b"A" * 1024},
    }
    options.update(kwargs)
    with pytest.raises(JBD2WriteProfileError):
        encode_classic_jbd2_transaction(**options)
