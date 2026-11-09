import struct

import pytest

from mountfile.jbd2_checkpoint import (
    JBD2CheckpointExecutionError,
    execute_classic_jbd2_checkpoint,
    plan_classic_jbd2_checkpoint,
    reduce_classic_jbd2_transactions,
)
from mountfile.jbd2_writer import (
    JBD2WriteProfileError,
    decode_classic_jbd2_transaction,
    encode_classic_jbd2_transaction,
    parse_classic_jbd2_superblock,
)

UUID = b"0123456789abcdef"


def journal_superblock(*, sequence=17, start=5, block_size=1024):
    raw = bytearray(block_size)
    struct.pack_into(">III", raw, 0, 0xC03B3998, 3, sequence)
    struct.pack_into(">IIIII", raw, 12, block_size, 32, 1, sequence, start)
    raw[48:64] = UUID
    return bytes(raw)


def make_plan(**kwargs):
    options = dict(
        superblock=journal_superblock(),
        filesystem_block_size=1024,
        filesystem_uuid=UUID,
        journal_blocks=32,
        filesystem_blocks=100,
        journal_physical_blocks=[40, 41, 42, 43],
        data_writes={8: b"A" * 1024, 12: b"B" * 1024},
        journal_superblock_physical_block=40,
    )
    options.update(kwargs)
    return plan_classic_jbd2_checkpoint(**options)


def test_checkpoint_orders_data_before_single_clean_superblock():
    plan = make_plan()

    assert [write.phase for write in plan.writes] == [
        "data",
        "data",
        "journal-superblock",
    ]
    assert [write.block for write in plan.writes] == [8, 12, 40]
    assert plan.flush_boundaries == (2,)
    assert struct.unpack_from(">III", plan.clean_superblock, 0) == (
        0xC03B3998,
        3,
        0,
    )
    assert struct.unpack_from(">I", plan.clean_superblock, 24)[0] == 0
    assert struct.unpack_from(">I", plan.clean_superblock, 28)[0] == 0
    clean_state = parse_classic_jbd2_superblock(
        plan.clean_superblock,
        filesystem_block_size=1024,
        filesystem_uuid=UUID,
        journal_blocks=32,
    )
    assert clean_state.empty is True
    assert clean_state.sequence == 0


def test_checkpoint_is_deterministic_and_does_not_mutate_input():
    raw = bytearray(journal_superblock())
    before = bytes(raw)

    plan = make_plan(superblock=raw, data_writes={8: bytearray(b"X" * 1024)})

    assert bytes(raw) == before
    assert plan.writes[0].data == b"X" * 1024


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"data_writes": {40: b"X" * 1024}}, "overlaps journal"),
        ({"journal_superblock_physical_block": 9}, "outside journal"),
        ({"data_writes": {8: b"short"}}, "wrong size"),
        ({"data_writes": {0: b"X" * 1024}}, "outside filesystem"),
    ],
)
def test_checkpoint_rejects_unsafe_write_shapes(kwargs, message):
    with pytest.raises(JBD2WriteProfileError, match=message):
        make_plan(**kwargs)


def test_checkpoint_rejects_clean_or_empty_journal():
    with pytest.raises(JBD2WriteProfileError, match="already-clean"):
        make_plan(superblock=journal_superblock(start=0))


def test_checkpoint_rejects_unknown_journal_features():
    raw = bytearray(journal_superblock())
    struct.pack_into(">I", raw, 40, 1)

    with pytest.raises(JBD2WriteProfileError, match="feature"):
        make_plan(superblock=raw)


def test_decoder_restores_escaped_data_and_reducer_applies_revoke():
    escaped = struct.pack(">I", 0xC03B3998) + b"A" * 1020
    first = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=17,
        filesystem_blocks=100,
        journal_uuid=UUID,
        writes={8: escaped, 12: b"B" * 1024},
    )
    second = encode_classic_jbd2_transaction(
        block_size=1024,
        sequence=18,
        filesystem_blocks=100,
        journal_uuid=UUID,
        writes={20: b"C" * 1024},
        revokes=[8],
    )

    decoded_first = decode_classic_jbd2_transaction(
        first,
        block_size=1024,
        filesystem_blocks=100,
        journal_blocks=[40],
    )
    decoded_second = decode_classic_jbd2_transaction(
        second,
        block_size=1024,
        filesystem_blocks=100,
        journal_blocks=[40],
    )
    assert dict(decoded_first.writes)[8] == escaped
    assert reduce_classic_jbd2_transactions([decoded_first, decoded_second]) == {
        12: b"B" * 1024,
        20: b"C" * 1024,
    }


def test_executor_syncs_data_before_clean_marker_and_can_inject_failure(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"\x00" * (100 * 1024))
    plan = make_plan(data_writes={8: b"X" * 1024})
    syncs = []
    with image.open("r+b") as stream:
        stream.seek(40 * 1024)
        stream.write(journal_superblock())

    def fail_on_marker(write):
        if write.phase == "journal-superblock":
            raise RuntimeError("simulated process termination")

    with image.open("r+b") as stream, pytest.raises(
        JBD2CheckpointExecutionError, match="before clean"
    ):
        execute_classic_jbd2_checkpoint(
            stream,
            plan,
            block_size=1024,
            sync=lambda: syncs.append("sync"),
            fault_injector=fail_on_marker,
        )
    assert syncs == ["sync"]
    with image.open("rb") as stream:
        stream.seek(8 * 1024)
        assert stream.read(1024) == b"X" * 1024
        stream.seek(40 * 1024)
        assert struct.unpack_from(">I", stream.read(1024), 28)[0] == 5


def test_executor_success_syncs_twice_and_publishes_clean_marker(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"\x00" * (100 * 1024))
    plan = make_plan(data_writes={8: b"X" * 1024})
    syncs = []
    with image.open("r+b") as stream:
        stream.seek(40 * 1024)
        stream.write(journal_superblock())
    with image.open("r+b") as stream:
        execute_classic_jbd2_checkpoint(
            stream,
            plan,
            block_size=1024,
            sync=lambda: syncs.append("sync"),
        )
    assert syncs == ["sync", "sync"]
    with image.open("rb") as stream:
        stream.seek(40 * 1024)
        marker = stream.read(1024)
    assert struct.unpack_from(">III", marker, 0) == (0xC03B3998, 3, 0)
    assert struct.unpack_from(">II", marker, 24) == (0, 0)
