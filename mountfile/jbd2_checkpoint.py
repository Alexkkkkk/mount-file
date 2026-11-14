"""Durable ordering plan for the bounded classic JBD2 profile.

This module deliberately plans a checkpoint but does not mutate an image.  A
checkpoint is safe only when the caller maps filesystem block numbers to a
validated image, writes data blocks first, issues a durable flush, then writes
the journal superblock with ``s_sequence == s_start == 0`` and flushes again.
Unknown checksum/64-bit journal profiles remain rejected by the existing JBD2
parser.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from os import fsync
from typing import BinaryIO

from .jbd2_writer import (
    _JBD2_MAGIC,
    _SUPPORTED_BLOCK_SIZES,
    JBD2DecodedTransaction,
    JBD2JournalState,
    JBD2WriteProfileError,
    decode_classic_jbd2_transaction,
    parse_classic_jbd2_superblock,
)


@dataclass(frozen=True)
class JBD2CheckpointWrite:
    """One full-block write in the order required by a checkpoint."""

    block: int
    data: bytes
    phase: str  # data or journal-superblock


@dataclass(frozen=True)
class JBD2CheckpointPlan:
    """Immutable checkpoint plan; no image bytes are changed by this module."""

    writes: tuple[JBD2CheckpointWrite, ...]
    clean_superblock: bytes
    state_before: JBD2JournalState
    flush_boundaries: tuple[int, ...]


class JBD2CheckpointExecutionError(RuntimeError):
    """Checkpoint execution stopped before the clean marker was durable."""


def scan_classic_jbd2_transactions(
    journal: Sequence[bytes],
    state: JBD2JournalState,
    *,
    max_transactions: int = 1 << 16,
) -> tuple[tuple[bytes, ...], ...]:
    """Extract complete classic transactions from a validated journal ring.

    Only the no-checksum 32-bit-tag profile is accepted. The scanner follows
    ``state.start``, validates descriptor tags and UUID placement, collects
    data/revoke blocks until a matching commit, and returns immutable record
    groups for :func:`coordinate_classic_jbd2_replay`. It never mutates the
    journal and refuses torn or unknown control records.
    """
    if not isinstance(state, JBD2JournalState):
        raise JBD2WriteProfileError("JBD2 scanner state has the wrong type")
    if not isinstance(journal, Sequence) or isinstance(journal, bytes | str):
        raise JBD2WriteProfileError("JBD2 journal must be a sequence of blocks")
    if len(journal) != state.max_length:
        raise JBD2WriteProfileError("JBD2 journal ring length disagrees with state")
    if not isinstance(max_transactions, int) or isinstance(max_transactions, bool):
        raise JBD2WriteProfileError("JBD2 scanner transaction limit is invalid")
    if max_transactions <= 0:
        raise JBD2WriteProfileError("JBD2 scanner transaction limit is invalid")
    if state.start == 0:
        return ()
    block_size = state.block_size
    if any(not isinstance(block, bytes) or len(block) != block_size for block in journal):
        raise JBD2WriteProfileError("JBD2 journal contains a partial block")

    def at(position: int) -> bytes:
        if not state.first <= position < state.max_length:
            raise JBD2WriteProfileError("JBD2 scanner position escaped the ring")
        return journal[position]

    def advance(position: int) -> int:
        return state.first if position + 1 >= state.max_length else position + 1

    position = state.start
    transactions: list[tuple[bytes, ...]] = []
    visited: set[int] = set()
    while position not in visited and len(transactions) < max_transactions:
        visited.add(position)
        descriptor = at(position)
        if not any(descriptor):
            break
        magic, record_type, sequence = struct.unpack_from(">III", descriptor, 0)
        if magic != _JBD2_MAGIC or record_type != 1:
            raise JBD2WriteProfileError("JBD2 scanner expected a descriptor")
        if sequence != state.sequence and not transactions:
            raise JBD2WriteProfileError("JBD2 descriptor sequence disagrees with state")
        cursor = 12
        tags = 0
        while True:
            if cursor + 8 > block_size:
                raise JBD2WriteProfileError("JBD2 descriptor tags are truncated")
            _target, flags = struct.unpack_from(">II", descriptor, cursor)
            cursor += 8
            if tags == 0 and not flags & 0x00000002:
                if cursor + 16 > block_size:
                    raise JBD2WriteProfileError("JBD2 descriptor UUID is truncated")
                cursor += 16
            tags += 1
            if flags & 0x00000008:
                break
            if tags > (block_size - 12) // 8:
                raise JBD2WriteProfileError("JBD2 descriptor has too many tags")
        records = [descriptor]
        position = advance(position)
        for _ in range(tags):
            records.append(at(position))
            position = advance(position)
        while True:
            control = at(position)
            if not any(control):
                raise JBD2WriteProfileError(
                    "JBD2 transaction is missing a commit record"
                )
            control_magic, control_type, control_sequence = struct.unpack_from(
                ">III", control, 0
            )
            if control_magic != _JBD2_MAGIC or control_sequence != sequence:
                raise JBD2WriteProfileError("JBD2 control record sequence is invalid")
            if control_type == 5:
                if struct.unpack_from(">I", control, 12)[0] < 16:
                    raise JBD2WriteProfileError("JBD2 revoke record length is invalid")
                records.append(control)
                position = advance(position)
                continue
            if control_type != 2:
                raise JBD2WriteProfileError("JBD2 transaction is missing a commit record")
            records.append(control)
            transactions.append(tuple(records))
            position = advance(position)
            break
    if not transactions:
        raise JBD2WriteProfileError("JBD2 scanner found no complete transaction")
    if len(transactions) >= max_transactions and position not in visited:
        raise JBD2WriteProfileError("JBD2 scanner transaction limit exceeded")
    return tuple(transactions)


def reduce_classic_jbd2_transactions(
    transactions: Sequence[JBD2DecodedTransaction],
) -> dict[int, bytes]:
    """Reduce committed transactions to final filesystem block contents.

    Transactions must already be in journal sequence order. A revoke removes
    an older pending write before later writes are applied; the decoder has
    already proved that one transaction cannot both write and revoke a block.
    """
    if not isinstance(transactions, Sequence) or isinstance(
        transactions, bytes | bytearray | str
    ):
        raise JBD2WriteProfileError("transactions must be a sequence")
    reduced: dict[int, bytes] = {}
    previous_sequence: int | None = None
    for transaction in transactions:
        if not isinstance(transaction, JBD2DecodedTransaction):
            raise JBD2WriteProfileError("transaction has the wrong type")
        sequence = transaction.sequence
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 < sequence <= 0xFFFFFFFF
        ):
            raise JBD2WriteProfileError("invalid JBD2 transaction sequence")
        if previous_sequence is not None:
            delta = (sequence - previous_sequence) & 0xFFFFFFFF
            if not 0 < delta < 0x80000000:
                raise JBD2WriteProfileError(
                    "JBD2 transactions are not in forward sequence order"
                )
        for target in transaction.revokes:
            reduced.pop(target, None)
        reduced.update(transaction.writes)
        previous_sequence = sequence
    return dict(sorted(reduced.items()))


def coordinate_classic_jbd2_replay(
    stream: BinaryIO,
    *,
    superblock: bytes | bytearray | memoryview,
    records: Sequence[Sequence[bytes]],
    filesystem_block_size: int,
    filesystem_uuid: bytes,
    journal_blocks: int,
    filesystem_blocks: int,
    journal_physical_blocks: Sequence[int],
    journal_superblock_physical_block: int,
    journal_features: tuple[int, int, int] = (0, 0, 0),
    sync: Callable[[], None] | None = None,
    fault_injector: Callable[[JBD2CheckpointWrite], None] | None = None,
) -> JBD2CheckpointPlan:
    """Run the complete bounded classic replay/checkpoint pipeline.

    ``records`` must already be split at committed transaction boundaries by a
    filesystem-aware journal scanner. This coordinator performs the remaining
    safety-critical sequence: reject checksum/unknown features, decode every
    transaction before mutating the stream, reduce writes/revokes in sequence
    order, plan data-before-clean-marker ordering, and execute the checkpoint.
    It never publishes a filesystem image and refuses an empty or malformed
    transaction set.
    """
    if not isinstance(records, Sequence) or isinstance(records, bytes | str):
        raise JBD2WriteProfileError("JBD2 replay records must be a sequence")
    decoded = tuple(
        decode_classic_jbd2_transaction(
            transaction,
            block_size=filesystem_block_size,
            filesystem_blocks=filesystem_blocks,
            journal_blocks=journal_physical_blocks,
            journal_features=journal_features,
        )
        for transaction in records
    )
    data_writes = reduce_classic_jbd2_transactions(decoded)
    plan = plan_classic_jbd2_checkpoint(
        superblock=superblock,
        filesystem_block_size=filesystem_block_size,
        filesystem_uuid=filesystem_uuid,
        journal_blocks=journal_blocks,
        filesystem_blocks=filesystem_blocks,
        journal_physical_blocks=journal_physical_blocks,
        data_writes=data_writes,
        journal_superblock_physical_block=journal_superblock_physical_block,
    )
    execute_classic_jbd2_checkpoint(
        stream,
        plan,
        block_size=filesystem_block_size,
        sync=sync,
        fault_injector=fault_injector,
    )
    return plan


def _validate_block_number(block: int, filesystem_blocks: int) -> None:
    if isinstance(block, bool) or not isinstance(block, int):
        raise JBD2WriteProfileError("checkpoint block must be an integer")
    if not 0 < block < filesystem_blocks:
        raise JBD2WriteProfileError("checkpoint block is outside filesystem bounds")


def plan_classic_jbd2_checkpoint(
    *,
    superblock: bytes | bytearray | memoryview,
    filesystem_block_size: int,
    filesystem_uuid: bytes,
    journal_blocks: int,
    filesystem_blocks: int,
    journal_physical_blocks: Sequence[int],
    data_writes: Mapping[int, bytes],
    journal_superblock_physical_block: int,
) -> JBD2CheckpointPlan:
    """Plan a checkpoint from already validated post-replay filesystem blocks.

    ``data_writes`` must contain the final contents for each filesystem block
    whose journal transaction has been durably committed.  The function does
    not parse or guess transaction contents: callers must first validate the
    complete descriptor/data/revoke/commit sequence and apply revoke rules.

    The returned order has a single durability boundary after all data writes.
    The clean journal superblock is written only after that boundary.  Ring
    record clearing is intentionally omitted: once the clean marker is durable,
    stale records are unreachable and clearing them would add an unnecessary
    crash window.
    """
    if filesystem_block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported filesystem block size")
    if isinstance(filesystem_blocks, bool) or not isinstance(filesystem_blocks, int):
        raise JBD2WriteProfileError("filesystem block count must be an integer")
    if filesystem_blocks < 2:
        raise JBD2WriteProfileError("filesystem is too small for checkpointing")
    if not isinstance(data_writes, Mapping):
        raise JBD2WriteProfileError("checkpoint data writes must be a mapping")
    if not isinstance(journal_physical_blocks, Sequence) or isinstance(
        journal_physical_blocks, bytes | bytearray | str
    ):
        raise JBD2WriteProfileError("journal physical blocks must be a sequence")
    journal_owned = set(journal_physical_blocks)
    if len(journal_owned) != len(journal_physical_blocks) or not journal_owned:
        raise JBD2WriteProfileError("journal physical block set is invalid")
    for block in journal_owned:
        _validate_block_number(block, filesystem_blocks)
    _validate_block_number(journal_superblock_physical_block, filesystem_blocks)
    if journal_superblock_physical_block not in journal_owned:
        raise JBD2WriteProfileError("journal superblock is outside journal blocks")

    state = parse_classic_jbd2_superblock(
        superblock,
        filesystem_block_size=filesystem_block_size,
        filesystem_uuid=filesystem_uuid,
        journal_blocks=journal_blocks,
    )
    if state.empty or state.start == 0:
        raise JBD2WriteProfileError("cannot checkpoint an already-clean JBD2 journal")
    raw = bytes(superblock)
    if len(raw) != filesystem_block_size:
        raise JBD2WriteProfileError("JBD2 superblock must be exactly one block")
    if struct.unpack_from(">I", raw, 0)[0] != _JBD2_MAGIC:
        raise JBD2WriteProfileError("invalid JBD2 superblock magic")

    writes: list[JBD2CheckpointWrite] = []
    for block, data in sorted(data_writes.items()):
        _validate_block_number(block, filesystem_blocks)
        if block in journal_owned:
            raise JBD2WriteProfileError("checkpoint data overlaps journal blocks")
        if not isinstance(data, bytes | bytearray | memoryview):
            raise JBD2WriteProfileError("checkpoint data must be bytes-like")
        normalized = bytes(data)
        if len(normalized) != filesystem_block_size:
            raise JBD2WriteProfileError("checkpoint data block has the wrong size")
        writes.append(JBD2CheckpointWrite(block, normalized, "data"))

    clean = bytearray(raw)
    # h_sequence, s_sequence, and s_start. A zero start is the durable empty
    # marker; resetting both sequence copies prevents stale transaction replay.
    struct.pack_into(">I", clean, 8, 0)
    struct.pack_into(">I", clean, 24, 0)
    struct.pack_into(">I", clean, 28, 0)
    writes.append(
        JBD2CheckpointWrite(
            journal_superblock_physical_block,
            bytes(clean),
            "journal-superblock",
        )
    )
    return JBD2CheckpointPlan(
        writes=tuple(writes),
        clean_superblock=bytes(clean),
        state_before=state,
        flush_boundaries=(len(writes) - 1,),
    )


def _write_full_block(
    stream: BinaryIO, offset: int, data: bytes, block_size: int
) -> None:
    if len(data) != block_size:
        raise JBD2CheckpointExecutionError("checkpoint attempted a partial block")
    stream.seek(offset * block_size)
    cursor = 0
    while cursor < len(data):
        written = stream.write(data[cursor:])
        if not isinstance(written, int) or written <= 0:
            raise JBD2CheckpointExecutionError("checkpoint stream made no progress")
        cursor += written


def execute_classic_jbd2_checkpoint(
    stream: BinaryIO,
    plan: JBD2CheckpointPlan,
    *,
    block_size: int,
    sync: Callable[[], None] | None = None,
    fault_injector: Callable[[JBD2CheckpointWrite], None] | None = None,
) -> None:
    """Execute a checkpoint plan with a durable data-before-marker barrier.

    ``stream`` must be a writable, seekable image opened by the caller. The
    default sync calls ``fsync(stream.fileno())``; tests and controlled device
    adapters may provide an equivalent sync callback. If a fault is injected
    before the journal-superblock write begins, the old dirty marker is
    intentionally left untouched and recovery remains required.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2CheckpointExecutionError("unsupported checkpoint block size")
    if not isinstance(plan, JBD2CheckpointPlan):
        raise JBD2CheckpointExecutionError("checkpoint plan has the wrong type")
    if sync is None:
        try:
            descriptor = stream.fileno()
        except (AttributeError, OSError) as exc:
            raise JBD2CheckpointExecutionError(
                "checkpoint stream has no durable fsync descriptor"
            ) from exc

        def sync_fn() -> None:
            stream.flush()
            fsync(descriptor)

    else:

        def sync_fn() -> None:
            stream.flush()
            sync()

    try:
        for write in plan.writes[:-1]:
            if write.phase != "data":
                raise JBD2CheckpointExecutionError("data writes are not ordered first")
            if fault_injector is not None:
                fault_injector(write)
            _write_full_block(stream, write.block, write.data, block_size)
        sync_fn()
        marker = plan.writes[-1]
        if marker.phase != "journal-superblock":
            raise JBD2CheckpointExecutionError("clean marker is not the final write")
        if fault_injector is not None:
            fault_injector(marker)
        _write_full_block(stream, marker.block, marker.data, block_size)
        sync_fn()
    except JBD2CheckpointExecutionError:
        raise
    except Exception as exc:
        raise JBD2CheckpointExecutionError(
            "checkpoint stopped before clean marker became durable"
        ) from exc
