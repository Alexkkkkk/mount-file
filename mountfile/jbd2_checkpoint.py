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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .jbd2_writer import (
    _JBD2_MAGIC,
    _SUPPORTED_BLOCK_SIZES,
    JBD2JournalState,
    JBD2WriteProfileError,
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
