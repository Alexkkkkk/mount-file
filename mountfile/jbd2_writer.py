"""Bounded serializer for classic 32-bit, no-checksum JBD2 transactions.

This module only encodes transaction records. It does not write a filesystem
image, manage the journal ring, checkpoint metadata, or claim ext3/ext4 write
support. Checksum, 64-bit-tag, and unknown journal profiles must stay disabled.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_JBD2_TAG_LAST = 0x00000008
_JBD2_TAG_SAME_UUID = 0x00000002
_U32_MAX = 0xFFFFFFFF
_SUPPORTED_BLOCK_SIZES = {1024, 2048, 4096}


class JBD2WriteProfileError(ValueError):
    """The bounded classic JBD2 transaction profile was rejected."""


def _validate_target(target: int, filesystem_blocks: int, forbidden: set[int]) -> None:
    if isinstance(target, bool) or not isinstance(target, int):
        raise JBD2WriteProfileError("JBD2 target block must be an integer")
    if target <= 0 or target >= filesystem_blocks:
        raise JBD2WriteProfileError("JBD2 target is outside filesystem bounds")
    if target in forbidden:
        raise JBD2WriteProfileError("JBD2 target overlaps journal-owned blocks")


def encode_classic_jbd2_transaction(
    *,
    block_size: int,
    sequence: int,
    filesystem_blocks: int,
    writes: Mapping[int, bytes],
    revokes: Sequence[int] = (),
    journal_blocks: Sequence[int] = (),
) -> tuple[bytes, ...]:
    """Encode one classic transaction as descriptor, data, revokes, commit.

    Descriptor tags use the SAME_UUID flag, so this profile contains no UUID
    extensions, checksums, 64-bit block tags, or escaped-data records. A single
    descriptor block is permitted; oversized write sets fail closed. The caller
    remains responsible for ring capacity, transaction ordering, durable writes,
    checkpointing, recovery, and independent filesystem validation.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise JBD2WriteProfileError("JBD2 sequence must be an integer")
    if not 1 <= sequence <= _U32_MAX:
        raise JBD2WriteProfileError("JBD2 sequence is outside 32-bit bounds")
    if (
        isinstance(filesystem_blocks, bool)
        or not isinstance(filesystem_blocks, int)
        or not 2 <= filesystem_blocks <= _U32_MAX
    ):
        raise JBD2WriteProfileError("filesystem block count is outside profile bounds")
    if not isinstance(writes, Mapping):
        raise JBD2WriteProfileError("JBD2 writes must be a block-to-bytes mapping")
    if not isinstance(revokes, Sequence) or isinstance(revokes, (str, bytes)):
        raise JBD2WriteProfileError("JBD2 revokes must be a sequence of block numbers")

    if not isinstance(journal_blocks, Sequence) or isinstance(
        journal_blocks, (str, bytes)
    ):
        raise JBD2WriteProfileError(
            "journal blocks must be a sequence of block numbers"
        )
    for target in journal_blocks:
        _validate_target(target, filesystem_blocks, set())
    forbidden = set(journal_blocks)
    if len(forbidden) != len(journal_blocks):
        raise JBD2WriteProfileError("duplicate journal-owned block")
    write_items = list(writes.items())
    for target, _data in write_items:
        _validate_target(target, filesystem_blocks, forbidden)
    write_items.sort(key=lambda item: item[0])
    revoke_items = list(revokes)
    if not write_items and not revoke_items:
        raise JBD2WriteProfileError("empty JBD2 transactions are not supported")

    for target, data in write_items:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise JBD2WriteProfileError("JBD2 data blocks must be bytes-like")
        if len(data) != block_size:
            raise JBD2WriteProfileError("JBD2 data block has the wrong size")
    for target in revoke_items:
        _validate_target(target, filesystem_blocks, forbidden)

    write_targets = {target for target, _data in write_items}
    if len(set(revoke_items)) != len(revoke_items):
        raise JBD2WriteProfileError("duplicate JBD2 revoke target")
    if write_targets.intersection(revoke_items):
        raise JBD2WriteProfileError("JBD2 transaction cannot write and revoke a block")

    max_tags = (block_size - 12) // 8
    if len(write_items) > max_tags:
        raise JBD2WriteProfileError("write set exceeds one JBD2 descriptor block")
    revoke_capacity = (block_size - 16) // 4
    revoke_chunks = [
        revoke_items[index : index + revoke_capacity]
        for index in range(0, len(revoke_items), revoke_capacity)
    ]

    records: list[bytes] = []
    if write_items:
        descriptor = bytearray(block_size)
        struct.pack_into(">III", descriptor, 0, _JBD2_MAGIC, _JBD2_DESCRIPTOR, sequence)
        cursor = 12
        data_blocks = []
        for index, (target, data) in enumerate(write_items):
            flags = _JBD2_TAG_SAME_UUID
            if index == len(write_items) - 1:
                flags |= _JBD2_TAG_LAST
            struct.pack_into(">II", descriptor, cursor, target, flags)
            cursor += 8
            data_blocks.append(bytes(data))
        records.append(bytes(descriptor))
        records.extend(data_blocks)

    for chunk in revoke_chunks:
        revoke_record = bytearray(block_size)
        byte_count = 16 + 4 * len(chunk)
        struct.pack_into(
            ">IIII", revoke_record, 0, _JBD2_MAGIC, _JBD2_REVOKE, sequence, byte_count
        )
        for index, target in enumerate(chunk):
            struct.pack_into(">I", revoke_record, 16 + index * 4, target)
        records.append(bytes(revoke_record))

    commit = bytearray(block_size)
    struct.pack_into(">III", commit, 0, _JBD2_MAGIC, _JBD2_COMMIT, sequence)
    records.append(bytes(commit))
    return tuple(records)
