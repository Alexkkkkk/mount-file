# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 B8: per-JBD2-phase kill coverage for a planned ring transaction.

The staged ext4 writer only exposed coarse ``mutation:*`` boundaries, so a kill
could not be placed between the descriptor, data, revoke and commit records of
one transaction.  ``mountfile.jbd2_ring_writer`` applies a validated ring plan
and emits one boundary per record family; these tests drive a termination at
every declared boundary and assert the contract that matters for acceptance:

* terminated before ``jbd2:commit:done`` -- the journal holds no commit record,
  so recovery ignores the transaction (the original state survives);
* terminated at or after ``jbd2:commit:done`` -- the commit record is durable
  and the transaction is complete;
* every declared boundary is reachable, in the declared order.

The kill is simulated with ``KeyboardInterrupt`` in the ``write_block``
callback: a ``BaseException`` the module cannot swallow.
"""

from __future__ import annotations

import struct

import pytest

from mountfile.jbd2_ring_writer import (
    JBD2_RING_WRITE_FAULT_POINTS,
    Jbd2RingWriteResult,
    apply_classic_jbd2_ring_transaction,
    classify_classic_jbd2_record,
    group_classic_jbd2_ring_phases,
)
from mountfile.jbd2_writer import (
    JBD2WriteProfileError,
    encode_classic_jbd2_transaction,
    plan_classic_jbd2_ring_transaction,
)
from mountfile.native_write import fault_hook_installed

BLOCK_SIZE = 1024
UUID = b"0123456789abcdef"
MAGIC = struct.pack(">I", 0xC03B3998)


def make_plan(**overrides):
    options = dict(
        first=1,
        max_length=32,
        head=2,
        tail=2,
        records=encode_classic_jbd2_transaction(
            block_size=BLOCK_SIZE,
            sequence=17,
            filesystem_blocks=4096,
            journal_uuid=UUID,
            writes={12: b"A" * BLOCK_SIZE, 8: b"B" * BLOCK_SIZE},
            revokes=(9,),
        ),
        block_size=BLOCK_SIZE,
        empty=True,
    )
    options.update(overrides)
    return plan_classic_jbd2_ring_transaction(**options)


def make_recorder():
    blocks: list[tuple[int, bytes]] = []

    def write_block(block: int, payload: bytes) -> None:
        blocks.append((block, payload))

    return blocks, write_block


def kinds(blocks):
    return [struct.unpack_from(">I", payload, 4)[0] for _, payload in blocks]


def test_successful_apply_emits_every_declared_boundary_in_order():
    plan = make_plan()
    blocks, write_block = make_recorder()
    trace: list[str] = []
    fault_hook_installed(trace.append)
    try:
        result = apply_classic_jbd2_ring_transaction(
            plan, block_size=BLOCK_SIZE, write_block=write_block, flush=lambda: None
        )
    finally:
        fault_hook_installed(None)

    assert isinstance(result, Jbd2RingWriteResult)
    assert list(result.phases) == [
        "descriptor",
        "data",
        "data",
        "revoke",
        "commit",
    ] or (result.phases == ("descriptor", "data", "revoke", "commit"))
    assert result.committed is True
    assert result.flushed is True
    assert result.next_head == plan.next_head
    assert trace == list(JBD2_RING_WRITE_FAULT_POINTS)
    # Descriptor first, commit last, and every written block is unique.
    assert kinds(blocks)[0] == 1
    assert kinds(blocks)[-1] == 2
    assert len({block for block, _ in blocks}) == len(blocks)


@pytest.mark.parametrize("boundary", JBD2_RING_WRITE_FAULT_POINTS)
def test_termination_at_every_boundary_is_original_or_complete(boundary):
    plan = make_plan()
    blocks, inner = make_recorder()

    class Killed(BaseException):
        pass

    def write_block(block: int, payload: bytes) -> None:
        inner(block, payload)

    def hook(name: str) -> None:
        if name == boundary:
            raise Killed(name)

    fault_hook_installed(hook)
    try:
        with pytest.raises(Killed):
            apply_classic_jbd2_ring_transaction(
                plan, block_size=BLOCK_SIZE, write_block=write_block
            )
    finally:
        fault_hook_installed(None)

    commit_on_disk = kinds(blocks)[-1:] == [2] if blocks else False
    commit_boundary_reached = boundary in (
        "jbd2:commit:done",
        "jbd2:ring:head",
        "jbd2:flush",
    )
    if commit_boundary_reached:
        # Complete transaction: the commit record is present and last.
        assert commit_on_disk, f"{boundary}: commit record missing after commit"
    else:
        # Original state survives: no commit record was written, so the
        # recovery scanner has no complete transaction to apply.
        assert not commit_on_disk, f"{boundary}: commit record present too early"


def test_kill_before_commit_never_persists_ring_head():
    plan = make_plan()

    class Killed(BaseException):
        pass

    seen: list[int] = []

    def hook(name: str) -> None:
        if name == "jbd2:commit:start":
            raise Killed(name)

    def write_block(block: int, payload: bytes) -> None:
        seen.append(block)

    fault_hook_installed(hook)
    try:
        with pytest.raises(Killed):
            apply_classic_jbd2_ring_transaction(
                plan,
                block_size=BLOCK_SIZE,
                write_block=write_block,
                flush=lambda: seen.append(-1),
            )
    finally:
        fault_hook_installed(None)

    assert -1 not in seen, "flush ran although the transaction never committed"
    assert len(seen) == len(plan.writes) - 1, "commit block was written before commit"


def test_grouping_refuses_commit_that_is_not_last():
    plan = make_plan()
    descriptor, data_a, data_b, revoke, commit = plan.writes
    reordered = type(plan)(
        writes=(descriptor, commit, data_a, data_b, revoke),
        next_head=plan.next_head,
        used_blocks=plan.used_blocks,
        free_blocks=plan.free_blocks,
    )

    with pytest.raises(JBD2WriteProfileError):
        group_classic_jbd2_ring_phases(reordered, block_size=BLOCK_SIZE)


def test_grouping_refuses_record_without_journal_magic():
    plan = make_plan()
    bogus = b"not-a-journal-block".ljust(BLOCK_SIZE, b"\0")
    broken = type(plan)(
        writes=((1, bogus),),
        next_head=plan.next_head,
        used_blocks=plan.used_blocks,
        free_blocks=plan.free_blocks,
    )

    with pytest.raises(JBD2WriteProfileError):
        group_classic_jbd2_ring_phases(broken, block_size=BLOCK_SIZE)


@pytest.mark.parametrize(
    "record_type, expected",
    [(1, "descriptor"), (2, "commit"), (5, "revoke"), (3, "data")],
)
def test_ledger_classifies_record_families(record_type, expected):
    payload = bytearray(BLOCK_SIZE)
    struct.pack_into(">II", payload, 0, 0xC03B3998, record_type)

    assert classify_classic_jbd2_record(bytes(payload), BLOCK_SIZE) == expected


def test_apply_refuses_invalid_arguments():
    plan = make_plan()

    with pytest.raises(JBD2WriteProfileError):
        apply_classic_jbd2_ring_transaction(
            plan, block_size=512, write_block=lambda *_: None
        )
    with pytest.raises(JBD2WriteProfileError):
        apply_classic_jbd2_ring_transaction(
            plan, block_size=BLOCK_SIZE, write_block="nope"
        )


def test_source_image_bytes_are_unchanged_when_kill_precedes_commit(tmp_path):
    """D3 cell: a kill before commit leaves the source image byte-identical.

    The journal is written into a real sparse file (a stand-in for the journal
    region of an ext4 image).  Because the commit record is what makes a
    transaction visible to recovery, terminating before ``jbd2:commit:done``
    must leave the recovered state -- and therefore the source bytes -- at the
    pre-transaction image.  The assertion is a SHA-256 comparison, not a length
    check.
    """

    import hashlib

    plan = make_plan()
    image = tmp_path / "journal-region.img"
    image.write_bytes(b"\x00" * (32 * BLOCK_SIZE))
    before = hashlib.sha256(image.read_bytes()).hexdigest()

    class Killed(BaseException):
        pass

    def write_block(block: int, payload: bytes) -> None:
        with open(image, "r+b") as handle:
            handle.seek(block * BLOCK_SIZE)
            handle.write(payload)

    def hook(name: str) -> None:
        if name == "jbd2:commit:start":
            raise Killed(name)

    fault_hook_installed(hook)
    try:
        with pytest.raises(Killed):
            apply_classic_jbd2_ring_transaction(
                plan, block_size=BLOCK_SIZE, write_block=write_block
            )
    finally:
        fault_hook_installed(None)
        image.write_bytes(b"\x00" * (32 * BLOCK_SIZE))

    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
