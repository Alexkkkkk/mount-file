"""Bounded serializer for classic 32-bit, no-checksum JBD2 transactions.

This module only encodes transaction records and computes bounded ring
placements. It does not write a filesystem image, update durable journal state,
checkpoint metadata, or claim ext3/ext4 write support. Checksum, 64-bit-tag,
and unknown journal profiles must stay disabled.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_JBD2_MAGIC = 0xC03B3998
_JBD2_MAGIC_BYTES = struct.pack(">I", _JBD2_MAGIC)
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_JBD2_TAG_LAST = 0x00000008
_JBD2_TAG_SAME_UUID = 0x00000002
_U32_MAX = 0xFFFFFFFF
_SUPPORTED_BLOCK_SIZES = {1024, 2048, 4096}


class JBD2WriteProfileError(ValueError):
    """The bounded classic JBD2 transaction profile was rejected."""


@dataclass(frozen=True)
class JBD2RingPlan:
    """Immutable placement plan; callers must still enforce durable ordering."""

    writes: tuple[tuple[int, bytes], ...]
    next_head: int
    used_blocks: int
    free_blocks: int


def _validate_target(target: int, filesystem_blocks: int, forbidden: set[int]) -> None:
    if isinstance(target, bool) or not isinstance(target, int):
        raise JBD2WriteProfileError("JBD2 target block must be an integer")
    if target <= 0 or target >= filesystem_blocks:
        raise JBD2WriteProfileError("JBD2 target is outside filesystem bounds")
    if target in forbidden:
        raise JBD2WriteProfileError("JBD2 target overlaps journal-owned blocks")


def _normalize_journal_uuid(journal_uuid: bytes) -> bytes:
    if not isinstance(journal_uuid, bytes | bytearray | memoryview):
        raise JBD2WriteProfileError("JBD2 journal UUID must be bytes-like")
    try:
        normalized = bytes(journal_uuid)
    except (TypeError, ValueError) as exc:
        raise JBD2WriteProfileError("JBD2 journal UUID must be bytes-like") from exc
    if len(normalized) != 16:
        raise JBD2WriteProfileError("JBD2 journal UUID must be exactly 16 bytes")
    return normalized


def encode_classic_jbd2_transaction(
    *,
    block_size: int,
    sequence: int,
    filesystem_blocks: int,
    journal_uuid: bytes,
    writes: Mapping[int, bytes],
    revokes: Sequence[int] = (),
    journal_blocks: Sequence[int] = (),
) -> tuple[bytes, ...]:
    """Encode one classic transaction as descriptor, data, revokes, commit.

    The first descriptor tag carries the validated 16-byte ``journal_uuid``;
    only later tags use SAME_UUID. Data beginning with the JBD2 magic is rejected
    because this profile does not implement ESCAPE records. Checksums, 64-bit
    block tags, and unknown journal profiles remain unsupported. A single
    descriptor block is permitted; oversized write sets fail closed. The caller
    remains responsible for ring capacity, transaction ordering, durable writes,
    checkpointing, recovery, and independent filesystem validation.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    journal_uuid = _normalize_journal_uuid(journal_uuid)
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
    if not isinstance(revokes, Sequence) or isinstance(revokes, str | bytes):
        raise JBD2WriteProfileError("JBD2 revokes must be a sequence of block numbers")

    if not isinstance(journal_blocks, Sequence) or isinstance(
        journal_blocks, str | bytes
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

    normalized_writes = []
    for target, data in write_items:
        if not isinstance(data, bytes | bytearray | memoryview):
            raise JBD2WriteProfileError("JBD2 data blocks must be bytes-like")
        try:
            normalized_data = bytes(data)
        except (TypeError, ValueError) as exc:
            raise JBD2WriteProfileError("JBD2 data blocks must be bytes-like") from exc
        if len(normalized_data) != block_size:
            raise JBD2WriteProfileError("JBD2 data block has the wrong size")
        if normalized_data.startswith(_JBD2_MAGIC_BYTES):
            raise JBD2WriteProfileError(
                "JBD2 data block requires unsupported ESCAPE handling"
            )
        normalized_writes.append((target, normalized_data))

    for target in revoke_items:
        _validate_target(target, filesystem_blocks, forbidden)

    write_targets = {target for target, _data in normalized_writes}
    if len(set(revoke_items)) != len(revoke_items):
        raise JBD2WriteProfileError("duplicate JBD2 revoke target")
    if write_targets.intersection(revoke_items):
        raise JBD2WriteProfileError("JBD2 transaction cannot write and revoke a block")

    max_tags = (block_size - 12 - 16) // 8
    if len(normalized_writes) > max_tags:
        raise JBD2WriteProfileError("write set exceeds one JBD2 descriptor block")
    revoke_capacity = (block_size - 16) // 4
    revoke_chunks = [
        revoke_items[index : index + revoke_capacity]
        for index in range(0, len(revoke_items), revoke_capacity)
    ]

    records: list[bytes] = []
    if normalized_writes:
        descriptor = bytearray(block_size)
        struct.pack_into(">III", descriptor, 0, _JBD2_MAGIC, _JBD2_DESCRIPTOR, sequence)
        cursor = 12
        data_blocks = []
        for index, (target, data) in enumerate(normalized_writes):
            flags = 0 if index == 0 else _JBD2_TAG_SAME_UUID
            if index == len(normalized_writes) - 1:
                flags |= _JBD2_TAG_LAST
            struct.pack_into(">II", descriptor, cursor, target, flags)
            cursor += 8
            if index == 0:
                descriptor[cursor : cursor + 16] = journal_uuid
                cursor += 16
            data_blocks.append(data)
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


def _validate_transaction_records(records: Sequence[bytes], block_size: int) -> None:
    """Validate the encoder's complete descriptor/data/revoke/commit profile."""
    if len(records) < 2:
        raise JBD2WriteProfileError("JBD2 transaction lacks records or commit")
    if any(
        not isinstance(record, bytes) or len(record) != block_size for record in records
    ):
        raise JBD2WriteProfileError("JBD2 ring records must be full-size byte blocks")

    first_magic, first_type, sequence = struct.unpack_from(">III", records[0])
    if (
        first_magic != _JBD2_MAGIC
        or first_type
        not in (
            _JBD2_DESCRIPTOR,
            _JBD2_REVOKE,
        )
        or sequence == 0
    ):
        raise JBD2WriteProfileError("JBD2 transaction has an invalid first record")

    index = 0
    write_targets: set[int] = set()
    if first_type == _JBD2_DESCRIPTOR:
        cursor = 12
        tag_count = 0
        last = False
        while cursor + 8 <= block_size:
            target, flags = struct.unpack_from(">II", records[0], cursor)
            cursor += 8
            tag_count += 1
            if target == 0 or flags & ~(_JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID):
                raise JBD2WriteProfileError("JBD2 descriptor contains an invalid tag")
            if target in write_targets:
                raise JBD2WriteProfileError("JBD2 descriptor target is duplicated")
            write_targets.add(target)
            if tag_count == 1:
                if flags & _JBD2_TAG_SAME_UUID or cursor + 16 > block_size:
                    raise JBD2WriteProfileError("JBD2 first tag lacks its journal UUID")
                cursor += 16
            elif not flags & _JBD2_TAG_SAME_UUID:
                raise JBD2WriteProfileError("JBD2 later tag lacks SAME_UUID")
            if flags & _JBD2_TAG_LAST:
                last = True
                break
        if not tag_count or not last:
            raise JBD2WriteProfileError("JBD2 descriptor is unterminated or empty")
        index = 1 + tag_count
        if index >= len(records):
            raise JBD2WriteProfileError("JBD2 descriptor is missing data or commit")
        for data in records[1:index]:
            if data.startswith(_JBD2_MAGIC_BYTES):
                raise JBD2WriteProfileError(
                    "JBD2 data block requires unsupported ESCAPE handling"
                )

    revoke_targets: set[int] = set()
    while index < len(records) - 1:
        record = records[index]
        magic, record_type, revoke_sequence, byte_count = struct.unpack_from(
            ">IIII", record
        )
        if (
            magic != _JBD2_MAGIC
            or record_type != _JBD2_REVOKE
            or revoke_sequence != sequence
            or byte_count < 20
            or byte_count > block_size
            or (byte_count - 16) % 4
        ):
            raise JBD2WriteProfileError("JBD2 transaction has an invalid revoke record")
        for cursor in range(16, byte_count, 4):
            target = struct.unpack_from(">I", record, cursor)[0]
            if target == 0 or target in revoke_targets or target in write_targets:
                raise JBD2WriteProfileError(
                    "JBD2 transaction has an invalid revoke target"
                )
            revoke_targets.add(target)
        index += 1

    if not write_targets and not revoke_targets:
        raise JBD2WriteProfileError("empty JBD2 transactions are not supported")
    commit_magic, commit_type, commit_sequence = struct.unpack_from(">III", records[-1])
    if (
        commit_magic != _JBD2_MAGIC
        or commit_type != _JBD2_COMMIT
        or commit_sequence != sequence
    ):
        raise JBD2WriteProfileError(
            "JBD2 transaction has no matching final commit record"
        )


def plan_classic_jbd2_ring_transaction(
    *,
    first: int,
    max_length: int,
    head: int,
    tail: int,
    records: Sequence[bytes],
    block_size: int,
    empty: bool = False,
) -> JBD2RingPlan:
    """Plan record placement in a bounded circular ring without writing it.

    One ring block remains unused to distinguish full from empty. The caller
    must supply an explicit boolean empty-state bit because ``head == tail`` alone
    cannot distinguish an empty journal from an invalid/full one. The planner
    validates the full supported descriptor/data/revoke/commit transaction and
    its shared sequence before calculating placements. Superblock updates,
    locking,
    write ordering, flushes, and recovery remain separate obligations.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    for name, value in (
        ("first", first),
        ("max_length", max_length),
        ("head", head),
        ("tail", tail),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise JBD2WriteProfileError(f"JBD2 {name} must be an integer")
    if not isinstance(empty, bool):
        raise JBD2WriteProfileError("JBD2 empty state must be a bool")
    if first < 1 or max_length <= first:
        raise JBD2WriteProfileError("invalid JBD2 ring geometry")
    if not first <= head < max_length or not first <= tail < max_length:
        raise JBD2WriteProfileError("JBD2 head or tail is outside ring bounds")
    if not isinstance(records, Sequence) or isinstance(records, str | bytes):
        raise JBD2WriteProfileError("JBD2 records must be a sequence of blocks")
    _validate_transaction_records(records, block_size)

    ring_length = max_length - first
    if empty:
        if head != tail:
            raise JBD2WriteProfileError("empty JBD2 ring must have matching head/tail")
        used = 0
    else:
        if head == tail:
            raise JBD2WriteProfileError("JBD2 head/tail state is ambiguous or full")
        used = (head - tail) % ring_length
    free = ring_length - used - 1
    if len(records) > free:
        raise JBD2WriteProfileError("JBD2 transaction would overrun live ring records")

    writes = tuple(
        (
            first + ((head - first + index) % ring_length),
            record,
        )
        for index, record in enumerate(records)
    )
    next_head = first + ((head - first + len(records)) % ring_length)
    return JBD2RingPlan(
        writes=writes,
        next_head=next_head,
        used_blocks=used + len(records),
        free_blocks=free - len(records),
    )
