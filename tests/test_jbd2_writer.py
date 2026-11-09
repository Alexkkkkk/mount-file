import struct
from array import array
from types import SimpleNamespace

import pytest

from mountfile.ext3_reader import NativeExt3Image
from mountfile.jbd2_writer import (
    JBD2JournalState,
    JBD2WriteProfileError,
    encode_classic_jbd2_transaction,
    parse_classic_jbd2_superblock,
    plan_classic_jbd2_ring_transaction,
)

_MAGIC = 0xC03B3998
_MAGIC_BYTES = struct.pack(">I", _MAGIC)
_UUID = b"0123456789abcdef"


def encode(*, block_size=1024, sequence=17, writes=None, revokes=(), **kwargs):
    return encode_classic_jbd2_transaction(
        block_size=block_size,
        sequence=sequence,
        filesystem_blocks=4096,
        journal_uuid=_UUID,
        writes={} if writes is None else writes,
        revokes=revokes,
        **kwargs,
    )


def test_descriptor_has_golden_first_uuid_and_later_same_uuid_tags():
    records = encode(writes={12: b"A" * 1024, 8: b"B" * 1024})

    expected_descriptor = bytearray(1024)
    struct.pack_into(">III", expected_descriptor, 0, _MAGIC, 1, 17)
    struct.pack_into(">II", expected_descriptor, 12, 8, 0)
    expected_descriptor[20:36] = _UUID
    struct.pack_into(">II", expected_descriptor, 36, 12, 10)
    assert records[0] == bytes(expected_descriptor)
    assert records[1:] == (b"B" * 1024, b"A" * 1024, records[-1])
    assert struct.unpack_from(">III", records[-1]) == (_MAGIC, 2, 17)
    assert all(len(record) == 1024 for record in records)


def test_single_tag_golden_bytes_include_uuid_and_last_without_same_uuid():
    records = encode(writes={7: b"D" * 1024})
    assert records[0][:36] == (
        struct.pack(">III", _MAGIC, 1, 17) + struct.pack(">II", 7, 8) + _UUID
    )


@pytest.mark.parametrize(
    "invalid_uuid", [b"short", b"x" * 15, b"x" * 17, "0123456789abcdef", None]
)
def test_rejects_invalid_journal_uuid(invalid_uuid):
    with pytest.raises(JBD2WriteProfileError, match="UUID"):
        encode_classic_jbd2_transaction(
            block_size=1024,
            sequence=1,
            filesystem_blocks=64,
            journal_uuid=invalid_uuid,
            writes={8: b"A" * 1024},
        )


def test_accepts_bytes_like_uuid_and_normalizes_it():
    records = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=1,
        filesystem_blocks=64,
        journal_uuid=memoryview(bytearray(_UUID)),
        writes={8: b"A" * 1024},
    )
    assert records[0][20:36] == _UUID


def test_multibyte_memoryview_is_snapshotted_and_validated_by_byte_length():
    source = memoryview(array("I", [0x41414141] * 256))
    assert len(source) == 256
    assert source.nbytes == 1024

    records = encode(writes={8: source})

    assert records[1] == bytes(source)
    assert isinstance(records[1], bytes)
    assert len(records[1]) == 1024


def test_accepts_byte_oriented_memoryview_and_copies_its_contents():
    source = memoryview(bytearray(b"M" * 1024))
    records = encode(writes={8: source})
    source[0] = ord("N")
    assert records[1] == b"M" * 1024


def test_encodes_data_starting_with_magic_using_escape_tag():
    payload = _MAGIC_BYTES + b"A" * 1020

    records = encode(writes={8: payload})

    assert struct.unpack_from(">II", records[0], 12) == (8, 0x00000009)
    assert records[1] == b"\x00" * 4 + payload[4:]
    plan = plan_classic_jbd2_ring_transaction(
        first=1,
        max_length=8,
        head=1,
        tail=1,
        records=records,
        block_size=1024,
        empty=True,
    )
    assert len(plan.writes) == len(records)


def test_ring_planner_rejects_escaped_tag_with_nonzero_prefix():
    records = [
        bytearray(record) for record in encode(writes={8: _MAGIC_BYTES + b"A" * 1020})
    ]
    records[1][:4] = b"BAD!"

    with pytest.raises(JBD2WriteProfileError, match="zero prefix"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=8,
            head=1,
            tail=1,
            records=[bytes(record) for record in records],
            block_size=1024,
            empty=True,
        )


def test_ring_planner_rejects_magic_prefix_without_escape_tag():
    records = [bytearray(record) for record in encode(writes={8: b"A" * 1024})]
    struct.pack_into(">I", records[0], 16, 0x00000008)
    records[1][:4] = _MAGIC_BYTES

    with pytest.raises(JBD2WriteProfileError, match="lacks ESCAPE"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=8,
            head=1,
            tail=1,
            records=[bytes(record) for record in records],
            block_size=1024,
            empty=True,
        )


def test_encodes_revoke_only_transaction():
    records = encode(sequence=3, revokes=[9])

    assert len(records) == 2
    assert struct.unpack_from(">IIII", records[0]) == (_MAGIC, 5, 3, 20)
    assert struct.unpack_from(">I", records[0], 16)[0] == 9
    assert struct.unpack_from(">III", records[1]) == (_MAGIC, 2, 3)


def test_encodes_multiple_revoke_blocks_and_planner_accepts_them():
    revoke_capacity = (1024 - 16) // 4
    records = encode(sequence=6, revokes=list(range(1, revoke_capacity + 2)))

    assert len(records) == 3
    assert struct.unpack_from(">IIII", records[0]) == (_MAGIC, 5, 6, 1024)
    assert struct.unpack_from(">IIII", records[1]) == (_MAGIC, 5, 6, 20)
    assert struct.unpack_from(">III", records[2]) == (_MAGIC, 2, 6)
    plan = plan_classic_jbd2_ring_transaction(
        first=1,
        max_length=10,
        head=1,
        tail=1,
        records=records,
        block_size=1024,
        empty=True,
    )
    assert len(plan.writes) == 3


def test_serialized_transaction_passes_existing_classic_jbd2_validator():
    records = encode(
        sequence=23,
        writes={8: _MAGIC_BYTES + b"A" * 1020, 12: b"B" * 1024},
        revokes=[31],
    )
    reader = NativeExt3Image.__new__(NativeExt3Image)
    reader.block_size = 1024
    reader.blocks_count = 128
    reader.profile = SimpleNamespace(filesystem_uuid=_UUID)

    def read_record(_inode, logical, _journal_blocks):
        return records[logical]

    reader._journal_logical_block = read_record
    terminal = reader._validate_jbd2_transaction(
        None, len(records), 0, 23, journal_data_blocks=None
    )

    assert terminal == 4


def test_ring_planner_places_transaction_across_live_wrap_boundary():
    records = encode(sequence=4, writes={8: b"A" * 1024}, revokes=[9])

    plan = plan_classic_jbd2_ring_transaction(
        first=1,
        max_length=11,
        head=9,
        tail=8,
        records=records,
        block_size=1024,
    )

    assert [position for position, _block in plan.writes] == [9, 10, 1, 2]
    assert [block for _position, block in plan.writes] == list(records)
    assert plan.next_head == 3
    assert plan.used_blocks == 5
    assert plan.free_blocks == 4


def test_ring_planner_accounts_for_live_records_before_placement():
    records = encode(sequence=5, revokes=[9])

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


@pytest.mark.parametrize("block_size", [1024, 2048, 4096])
def test_descriptor_accepts_exact_capacity_for_supported_block_sizes(block_size):
    capacity = (block_size - 12 - 16) // 8
    records = encode(
        block_size=block_size,
        writes={index + 1: b"A" * block_size for index in range(capacity)},
    )
    assert len(records) == capacity + 2
    assert len(records[0]) == block_size

    with pytest.raises(JBD2WriteProfileError, match="descriptor block"):
        encode(
            block_size=block_size,
            writes={index + 1: b"A" * block_size for index in range(capacity + 1)},
        )


def test_ring_planner_rejects_ambiguous_ring_with_valid_transaction():
    records = encode(sequence=7, revokes=[8])
    with pytest.raises(JBD2WriteProfileError, match="ambiguous or full"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=6,
            head=4,
            tail=4,
            records=records,
            block_size=1024,
        )


def test_ring_planner_rejects_overfull_live_ring_with_valid_transaction():
    records = encode(sequence=8, writes={8: b"A" * 1024})
    with pytest.raises(JBD2WriteProfileError, match="overrun live ring"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=6,
            head=5,
            tail=3,
            records=records,
            block_size=1024,
        )


def test_ring_planner_requires_boolean_empty_state():
    records = encode(sequence=9, revokes=[8])
    with pytest.raises(JBD2WriteProfileError, match="empty state must be a bool"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=6,
            head=1,
            tail=1,
            records=records,
            block_size=1024,
            empty="False",
        )


def test_ring_planner_rejects_empty_state_with_different_head_and_tail():
    records = encode(sequence=10, revokes=[8])
    with pytest.raises(JBD2WriteProfileError, match="matching head/tail"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=6,
            head=2,
            tail=1,
            records=records,
            block_size=1024,
            empty=True,
        )


def test_ring_planner_rejects_sequence_mismatch_in_commit():
    records = [bytearray(record) for record in encode(sequence=11, revokes=[8])]
    struct.pack_into(">I", records[-1], 8, 12)
    records = [bytes(record) for record in records]
    with pytest.raises(JBD2WriteProfileError, match="matching final commit"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=8,
            head=1,
            tail=1,
            records=records,
            block_size=1024,
            empty=True,
        )


def test_ring_planner_rejects_sequence_mismatch_in_revoke():
    records = [
        bytearray(record)
        for record in encode(sequence=13, writes={8: b"A" * 1024}, revokes=[9])
    ]
    struct.pack_into(">I", records[2], 8, 14)
    records = [bytes(record) for record in records]
    with pytest.raises(JBD2WriteProfileError, match="invalid revoke"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=8,
            head=1,
            tail=1,
            records=records,
            block_size=1024,
            empty=True,
        )


def test_ring_planner_rejects_arbitrary_descriptor_prefix_and_bad_layout():
    records = list(encode(sequence=15, writes={8: b"A" * 1024}))
    records[0] = _MAGIC_BYTES + struct.pack(">II", 1, 15) + b"Z" * 1012
    with pytest.raises(JBD2WriteProfileError, match="descriptor"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=10,
            head=1,
            tail=1,
            records=records,
            block_size=1024,
            empty=True,
        )


def test_ring_planner_rejects_data_and_revoke_target_overlap():
    records = [
        bytearray(record)
        for record in encode(sequence=16, writes={8: b"A" * 1024}, revokes=[9])
    ]
    struct.pack_into(">I", records[2], 16, 8)
    records = [bytes(record) for record in records]
    with pytest.raises(JBD2WriteProfileError, match="revoke target"):
        plan_classic_jbd2_ring_transaction(
            first=1,
            max_length=10,
            head=1,
            tail=1,
            records=records,
            block_size=1024,
            empty=True,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"block_size": 512},
        {"sequence": 0},
        {"filesystem_blocks": 0},
        {"writes": {0: b"A" * 1024}},
        {"writes": {4096: b"A" * 1024}},
        {"writes": {8: b"short"}},
        {"writes": {8: b"A" * 1024}, "revokes": [8]},
        {"writes": {8: b"A" * 1024}, "journal_blocks": [8]},
        {"writes": {}, "revokes": [9, 9]},
        {"writes": {index: b"A" * 1024 for index in range(1, 200)}},
    ],
)
def test_rejects_unsupported_or_unsafe_transaction_shapes(kwargs):
    options = {
        "block_size": 1024,
        "sequence": 1,
        "filesystem_blocks": 64,
        "journal_uuid": _UUID,
        "writes": {8: b"A" * 1024},
    }
    options.update(kwargs)
    with pytest.raises(JBD2WriteProfileError):
        encode_classic_jbd2_transaction(**options)


def _superblock(*, block_size=1024, max_length=64, first=1, sequence=17, start=0):
    raw = bytearray(block_size)
    struct.pack_into(">III", raw, 0, _MAGIC, 3, sequence)
    struct.pack_into(">IIIII", raw, 12, block_size, max_length, first, sequence, start)
    raw[48:64] = _UUID
    return bytes(raw)


def test_parse_classic_jbd2_superblock_validates_empty_ring_state():
    state = parse_classic_jbd2_superblock(
        _superblock(),
        filesystem_block_size=1024,
        filesystem_uuid=_UUID,
        journal_blocks=64,
    )
    assert state == JBD2JournalState(
        block_size=1024,
        max_length=64,
        first=1,
        sequence=17,
        start=0,
        uuid=_UUID,
        empty=True,
    )


@pytest.mark.parametrize(
    "mutates, message",
    [
        (lambda raw: struct.pack_into(">I", raw, 12, 2048), "block size"),
        (lambda raw: struct.pack_into(">I", raw, 16, 63), "ring length"),
        (lambda raw: struct.pack_into(">I", raw, 8, 0), "sequence"),
        (lambda raw: raw.__setitem__(slice(48, 64), b"x" * 16), "UUID"),
        (lambda raw: struct.pack_into(">I", raw, 36, 1), "feature"),
        (lambda raw: struct.pack_into(">I", raw, 28, 64), "start"),
    ],
)
def test_parse_classic_jbd2_superblock_rejects_unsafe_state(mutates, message):
    raw = bytearray(_superblock())
    mutates(raw)
    with pytest.raises(JBD2WriteProfileError, match=message):
        parse_classic_jbd2_superblock(
            raw,
            filesystem_block_size=1024,
            filesystem_uuid=_UUID,
            journal_blocks=64,
        )
