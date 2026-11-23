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

from mountfile.ext4_jbd2_csum import (
    JBD2_FEATURE_INCOMPAT_64BIT,
    JBD2_FEATURE_INCOMPAT_CHECKSUMS,
    JBD2_FEATURE_INCOMPAT_CSUM_V2,
    JBD2_FEATURE_INCOMPAT_CSUM_V3,
    JBD2_FEATURE_INCOMPAT_REVOKE,
    JBD2ChecksumError,
    commit_checksum,
    descriptor_checksum,
    parse_tag,
    tag_bytes_for_features,
    tag_checksum,
    verify_commit_checksum,
    verify_descriptor_checksum,
    verify_journal_superblock_checksum,
)

_JBD2_MAGIC = 0xC03B3998
_JBD2_MAGIC_BYTES = struct.pack(">I", _JBD2_MAGIC)
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_JBD2_TAG_ESCAPE = 0x00000001
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


@dataclass(frozen=True)
class JBD2JournalState:
    """Validated classic JBD2 superblock state for a bounded journal ring.

    This is deliberately a state-only parser.  It does not authorize an image
    mutation: checksum features, durable ordering, checkpointing and recovery
    evidence remain separate requirements for an ext3/ext4 writer.
    """

    block_size: int
    max_length: int
    first: int
    sequence: int
    start: int
    uuid: bytes
    empty: bool
    feature_compat: int = 0
    feature_incompat: int = 0
    feature_ro_compat: int = 0
    checksum_type: int = 0

    @property
    def checksummed(self) -> bool:
        return bool(self.feature_incompat & JBD2_FEATURE_INCOMPAT_CHECKSUMS)


@dataclass(frozen=True)
class JBD2DecodedTransaction:
    """Validated transaction payload ready for bounded checkpoint reduction."""

    sequence: int
    writes: tuple[tuple[int, bytes], ...]
    revokes: tuple[int, ...]


def decode_classic_jbd2_transaction(
    records: Sequence[bytes],
    *,
    block_size: int,
    filesystem_blocks: int,
    journal_blocks: Sequence[int] = (),
    journal_features: tuple[int, int, int] = (0, 0, 0),
) -> JBD2DecodedTransaction:
    """Decode one already-committed classic transaction without applying it.

    The existing structural validator remains the authority for record shape.
    This function only extracts the validated target/data pairs and restores
    ESCAPE-tagged magic prefixes. It never writes an image and rejects targets
    outside the filesystem or inside journal-owned blocks. The optional
    ``journal_features`` tuple is the parsed ``(compat, incompat, ro)``
    superblock feature set; any nonzero value is refused so a checksum-enabled
    transaction cannot pass through the no-checksum decoder unchecked.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    if (
        not isinstance(journal_features, tuple)
        or len(journal_features) != 3
        or any(
            isinstance(feature, bool) or not isinstance(feature, int)
            for feature in journal_features
        )
    ):
        raise JBD2WriteProfileError(
            "JBD2 journal features must be a (compat, incompat, ro) tuple"
        )
    if any(journal_features):
        raise JBD2WriteProfileError(
            "checksum or other JBD2 journal features are unsupported by the "
            "classic decoder"
        )
    if not isinstance(records, Sequence) or isinstance(records, bytes | str):
        raise JBD2WriteProfileError("JBD2 records must be a sequence of blocks")
    forbidden = set(journal_blocks)
    if len(forbidden) != len(journal_blocks):
        raise JBD2WriteProfileError("duplicate journal-owned block")
    _validate_transaction_records(records, block_size)
    magic, record_type, sequence = struct.unpack_from(">III", records[0])
    writes: list[tuple[int, bytes]] = []
    revokes: list[int] = []
    index = 0
    if record_type == _JBD2_DESCRIPTOR:
        cursor = 12
        escaped: list[bool] = []
        targets: list[int] = []
        while True:
            target, flags = struct.unpack_from(">II", records[0], cursor)
            cursor += 8
            targets.append(target)
            escaped.append(bool(flags & _JBD2_TAG_ESCAPE))
            if len(targets) == 1 and not flags & _JBD2_TAG_SAME_UUID:
                cursor += 16
            index += 1
            if flags & _JBD2_TAG_LAST:
                break
        for target, is_escaped, data in zip(
            targets, escaped, records[1 : 1 + len(targets)], strict=True
        ):
            _validate_target(target, filesystem_blocks, forbidden)
            if is_escaped:
                if data[:4] != b"\x00" * 4:
                    raise JBD2WriteProfileError("escaped JBD2 data lacks zero prefix")
                data = _JBD2_MAGIC_BYTES + data[4:]
            writes.append((target, data))
        index = 1 + len(targets)

    while index < len(records) - 1:
        revoke = records[index]
        _magic, _type, revoke_sequence, byte_count = struct.unpack_from(">IIII", revoke)
        if revoke_sequence != sequence:
            raise JBD2WriteProfileError("JBD2 revoke sequence mismatch")
        for cursor in range(16, byte_count, 4):
            target = struct.unpack_from(">I", revoke, cursor)[0]
            _validate_target(target, filesystem_blocks, forbidden)
            revokes.append(target)
        index += 1
    return JBD2DecodedTransaction(sequence, tuple(writes), tuple(revokes))


def decode_jbd2_transaction(
    records: Sequence[bytes],
    *,
    block_size: int,
    filesystem_blocks: int,
    journal_blocks: Sequence[int] = (),
    journal_features: tuple[int, int, int] = (0, 0, 0),
    filesystem_uuid: bytes | None = None,
) -> JBD2DecodedTransaction:
    """Decode one transaction through the profile selected by its features.

    Checksummed v2/v3 journals require ``filesystem_uuid`` because every data
    tag and control block is UUID-seeded.  The classic profile keeps its
    existing signature and rejects every feature bit it cannot decode.
    """
    if (
        not isinstance(journal_features, tuple)
        or len(journal_features) != 3
        or any(
            isinstance(feature, bool) or not isinstance(feature, int)
            for feature in journal_features
        )
    ):
        raise JBD2WriteProfileError(
            "JBD2 journal features must be a (compat, incompat, ro) tuple"
        )
    _compat, incompat, _ro = journal_features
    if incompat & JBD2_FEATURE_INCOMPAT_CHECKSUMS:
        if filesystem_uuid is None:
            raise JBD2WriteProfileError(
                "checksummed JBD2 decoding requires the filesystem UUID"
            )
        return decode_checksummed_jbd2_transaction(
            records,
            block_size=block_size,
            filesystem_blocks=filesystem_blocks,
            journal_blocks=journal_blocks,
            journal_features=journal_features,
            filesystem_uuid=filesystem_uuid,
        )
    return decode_classic_jbd2_transaction(
        records,
        block_size=block_size,
        filesystem_blocks=filesystem_blocks,
        journal_blocks=journal_blocks,
        journal_features=journal_features,
    )


def decode_checksummed_jbd2_transaction(
    records: Sequence[bytes],
    *,
    block_size: int,
    filesystem_blocks: int,
    journal_blocks: Sequence[int] = (),
    journal_features: tuple[int, int, int] = (0, 0, 0),
    filesystem_uuid: bytes,
) -> JBD2DecodedTransaction:
    """Decode one committed JBD2 v2/v3 transaction after checksum validation.

    Every control block is verified before its contents are trusted: the
    descriptor and revoke tails use the UUID-seeded running CRC32C, each data
    tag hashes the big-endian sequence followed by the complete journal copy of
    the block, and the commit block zeroes its checksum fields.  ESCAPE-tagged
    blocks restore the JBD2 magic prefix exactly like the kernel recovery pass.
    A single mismatching checksum refuses the whole transaction before any
    target is emitted.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    if (
        not isinstance(journal_features, tuple)
        or len(journal_features) != 3
        or any(
            isinstance(feature, bool) or not isinstance(feature, int)
            for feature in journal_features
        )
    ):
        raise JBD2WriteProfileError(
            "JBD2 journal features must be a (compat, incompat, ro) tuple"
        )
    compat, incompat, ro = journal_features
    if compat or ro or incompat & ~_SUPPORTED_JBD2_INCOMPAT:
        raise JBD2WriteProfileError("unsupported JBD2 journal feature")
    checksum_bits = incompat & JBD2_FEATURE_INCOMPAT_CHECKSUMS
    if checksum_bits not in (
        JBD2_FEATURE_INCOMPAT_CSUM_V2,
        JBD2_FEATURE_INCOMPAT_CSUM_V3,
    ):
        raise JBD2WriteProfileError(
            "checksummed JBD2 decoder requires exactly one checksum feature"
        )
    try:
        from mountfile.ext4_jbd2_csum import uuid_seed

        seed = uuid_seed(bytes(filesystem_uuid))
    except (TypeError, ValueError, JBD2ChecksumError) as exc:
        raise JBD2WriteProfileError(f"JBD2 checksum seed is invalid: {exc}") from exc
    tag_size = tag_bytes_for_features(incompat)
    if not isinstance(records, Sequence) or isinstance(records, bytes | str):
        raise JBD2WriteProfileError("JBD2 records must be a sequence of blocks")
    if len(records) < 2:
        raise JBD2WriteProfileError("JBD2 transaction lacks records or commit")
    if any(
        not isinstance(record, bytes) or len(record) != block_size for record in records
    ):
        raise JBD2WriteProfileError("JBD2 ring records must be full-size byte blocks")
    forbidden = set(journal_blocks)
    if len(forbidden) != len(journal_blocks):
        raise JBD2WriteProfileError("duplicate journal-owned block")

    first = records[0]
    magic, record_type, sequence = struct.unpack_from(">III", first, 0)
    if magic != _JBD2_MAGIC or record_type not in (_JBD2_DESCRIPTOR, _JBD2_REVOKE):
        raise JBD2WriteProfileError("JBD2 transaction has an invalid first record")

    writes: list[tuple[int, bytes]] = []
    revokes: list[int] = []
    index = 0
    if record_type == _JBD2_DESCRIPTOR:
        try:
            if not verify_descriptor_checksum(first, seed):
                raise JBD2WriteProfileError("JBD2 descriptor checksum does not match")
        except JBD2ChecksumError as exc:
            raise JBD2WriteProfileError(
                f"JBD2 descriptor checksum is malformed: {exc}"
            ) from exc
        cursor = 12
        tags: list[tuple[int, int, int]] = []
        first_tag = True
        while True:
            if cursor + tag_size > block_size - 4:
                raise JBD2WriteProfileError("JBD2 descriptor tags are truncated")
            try:
                target, flags, stored, cursor = parse_tag(first, cursor, tag_size)
            except JBD2ChecksumError as exc:
                raise JBD2WriteProfileError(str(exc)) from exc
            if flags & ~(_JBD2_TAG_ESCAPE | _JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID):
                raise JBD2WriteProfileError(
                    "JBD2 descriptor contains an unknown tag flag"
                )
            if first_tag:
                if flags & _JBD2_TAG_SAME_UUID:
                    raise JBD2WriteProfileError("JBD2 first tag lacks its journal UUID")
                if cursor + 16 > block_size - 4:
                    raise JBD2WriteProfileError("JBD2 descriptor UUID is truncated")
                if first[cursor : cursor + 16] != bytes(filesystem_uuid):
                    raise JBD2WriteProfileError(
                        "JBD2 descriptor UUID does not match filesystem"
                    )
                cursor += 16
                first_tag = False
            elif not flags & _JBD2_TAG_SAME_UUID:
                raise JBD2WriteProfileError("JBD2 later tag lacks SAME_UUID")
            tags.append((target, flags, stored))
            if flags & _JBD2_TAG_LAST:
                break
            if len(tags) > (block_size - 12) // tag_size:
                raise JBD2WriteProfileError("JBD2 descriptor has too many tags")
        index = 1 + len(tags)
        if index > len(records) - 1:
            raise JBD2WriteProfileError(
                "JBD2 descriptor is missing data blocks or commit"
            )
        width = 4 if checksum_bits == JBD2_FEATURE_INCOMPAT_CSUM_V3 else 2
        for (target, flags, stored), data in zip(tags, records[1:index], strict=True):
            journal_data = data
            if flags & _JBD2_TAG_ESCAPE:
                if data[:4] != b"\x00" * 4:
                    raise JBD2WriteProfileError("escaped JBD2 data lacks zero prefix")
            try:
                expected = tag_checksum(sequence, journal_data, seed)
            except JBD2ChecksumError as exc:
                raise JBD2WriteProfileError(str(exc)) from exc
            if (expected & (0xFFFF if width == 2 else 0xFFFFFFFF)) != stored:
                raise JBD2WriteProfileError("JBD2 data tag checksum does not match")
            _validate_target(target, filesystem_blocks, forbidden)
            if flags & _JBD2_TAG_ESCAPE:
                data = _JBD2_MAGIC_BYTES + data[4:]
            writes.append((target, data))

    while index < len(records) - 1:
        revoke = records[index]
        try:
            if not verify_descriptor_checksum(revoke, seed):
                raise JBD2WriteProfileError("JBD2 revoke checksum does not match")
        except JBD2ChecksumError as exc:
            raise JBD2WriteProfileError(
                f"JBD2 revoke checksum is malformed: {exc}"
            ) from exc
        revoke_magic, revoke_type, revoke_sequence, byte_count = struct.unpack_from(
            ">IIII", revoke, 0
        )
        if (
            revoke_magic != _JBD2_MAGIC
            or revoke_type != _JBD2_REVOKE
            or revoke_sequence != sequence
            or byte_count < 16
            or byte_count > block_size
            or (byte_count - 16) % 4
        ):
            raise JBD2WriteProfileError("JBD2 transaction has an invalid revoke record")
        for cursor in range(16, byte_count, 4):
            target = struct.unpack_from(">I", revoke, cursor)[0]
            _validate_target(target, filesystem_blocks, forbidden)
            revokes.append(target)
        index += 1

    if not writes and not revokes:
        raise JBD2WriteProfileError("empty JBD2 transactions are not supported")
    commit = records[-1]
    commit_magic, commit_type, commit_sequence = struct.unpack_from(">III", commit, 0)
    if (
        commit_magic != _JBD2_MAGIC
        or commit_type != _JBD2_COMMIT
        or commit_sequence != sequence
    ):
        raise JBD2WriteProfileError(
            "JBD2 transaction has no matching final commit record"
        )
    try:
        if not verify_commit_checksum(commit, seed):
            raise JBD2WriteProfileError("JBD2 commit checksum does not match")
    except JBD2ChecksumError as exc:
        raise JBD2WriteProfileError(
            f"JBD2 commit checksum is malformed: {exc}"
        ) from exc
    return JBD2DecodedTransaction(sequence, tuple(writes), tuple(revokes))


def encode_checksummed_jbd2_transaction(
    *,
    block_size: int,
    sequence: int,
    filesystem_blocks: int,
    journal_uuid: bytes,
    writes: Mapping[int, bytes],
    revokes: Sequence[int] = (),
    journal_blocks: Sequence[int] = (),
    checksum_v3: bool = True,
    feature_64bit: bool = False,
) -> tuple[bytes, ...]:
    """Encode one committed v2/v3 transaction for fixtures and replay tests.

    The encoder is the symmetric counterpart of
    :func:`decode_checksummed_jbd2_transaction`: it writes the UUID-seeded
    descriptor/data/revoke/commit checksums exactly as JBD2 commit does.  It
    does not place records in a ring and never writes an image.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    journal_uuid = _normalize_journal_uuid(journal_uuid)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise JBD2WriteProfileError("JBD2 sequence must be an integer")
    if not 0 <= sequence <= _U32_MAX:
        raise JBD2WriteProfileError("JBD2 sequence is outside 32-bit bounds")
    if (
        isinstance(filesystem_blocks, bool)
        or not isinstance(filesystem_blocks, int)
        or not 2 <= filesystem_blocks <= (1 << 48)
    ):
        raise JBD2WriteProfileError("filesystem block count is outside profile bounds")
    if not isinstance(writes, Mapping):
        raise JBD2WriteProfileError("JBD2 writes must be a block-to-bytes mapping")
    if not isinstance(revokes, Sequence) or isinstance(revokes, str | bytes):
        raise JBD2WriteProfileError("JBD2 revokes must be a sequence of block numbers")
    forbidden = set(journal_blocks)
    if len(forbidden) != len(journal_blocks):
        raise JBD2WriteProfileError("duplicate journal-owned block")
    write_items = sorted(writes.items())
    for target, data in write_items:
        _validate_target(target, filesystem_blocks, forbidden)
        if not isinstance(data, bytes | bytearray | memoryview):
            raise JBD2WriteProfileError("JBD2 data blocks must be bytes-like")
        if len(bytes(data)) != block_size:
            raise JBD2WriteProfileError("JBD2 data block has the wrong size")
    revoke_items = list(revokes)
    for target in revoke_items:
        _validate_target(target, filesystem_blocks, forbidden)
    if not write_items and not revoke_items:
        raise JBD2WriteProfileError("empty JBD2 transactions are not supported")
    if len(set(revoke_items)) != len(revoke_items):
        raise JBD2WriteProfileError("duplicate JBD2 revoke target")
    if {target for target, _data in write_items}.intersection(revoke_items):
        raise JBD2WriteProfileError("JBD2 transaction cannot write and revoke a block")
    try:
        from mountfile.ext4_jbd2_csum import uuid_seed

        seed = uuid_seed(journal_uuid)
    except JBD2ChecksumError as exc:  # pragma: no cover - normalized above
        raise JBD2WriteProfileError(str(exc)) from exc

    incompat = (
        JBD2_FEATURE_INCOMPAT_CSUM_V3 if checksum_v3 else JBD2_FEATURE_INCOMPAT_CSUM_V2
    )
    if feature_64bit:
        incompat |= JBD2_FEATURE_INCOMPAT_64BIT
    tag_size = tag_bytes_for_features(incompat)
    max_tags = (block_size - 12 - 16 - 4) // tag_size
    if len(write_items) > max_tags:
        raise JBD2WriteProfileError("write set exceeds one JBD2 descriptor block")

    records: list[bytes] = []
    if write_items:
        descriptor = bytearray(block_size)
        struct.pack_into(">III", descriptor, 0, _JBD2_MAGIC, _JBD2_DESCRIPTOR, sequence)
        cursor = 12
        data_blocks: list[bytes] = []
        for index, (target, data) in enumerate(write_items):
            raw = bytes(data)
            escaped = raw.startswith(_JBD2_MAGIC_BYTES)
            journal_data = b"\x00" * 4 + raw[4:] if escaped else raw
            flags = _JBD2_TAG_ESCAPE if escaped else 0
            if index:
                flags |= _JBD2_TAG_SAME_UUID
            if index == len(write_items) - 1:
                flags |= _JBD2_TAG_LAST
            checksum = tag_checksum(sequence, journal_data, seed)
            low = target & 0xFFFFFFFF
            high = (target >> 32) & 0xFFFFFFFF
            if checksum_v3:
                struct.pack_into(
                    ">IIII",
                    descriptor,
                    cursor,
                    low,
                    flags,
                    high if feature_64bit else 0,
                    checksum,
                )
            else:
                struct.pack_into(
                    ">IHH", descriptor, cursor, low, checksum & 0xFFFF, flags
                )
                if feature_64bit:
                    struct.pack_into(">I", descriptor, cursor + 8, high)
            cursor += tag_size
            if index == 0:
                descriptor[cursor : cursor + 16] = journal_uuid
                cursor += 16
            data_blocks.append(journal_data)
        struct.pack_into(
            ">I",
            descriptor,
            block_size - 4,
            descriptor_checksum(bytes(descriptor), seed),
        )
        records.append(bytes(descriptor))
        records.extend(data_blocks)

    revoke_capacity = (block_size - 16 - 4) // 4
    for start in range(0, len(revoke_items), revoke_capacity):
        chunk = revoke_items[start : start + revoke_capacity]
        revoke_record = bytearray(block_size)
        byte_count = 16 + 4 * len(chunk)
        struct.pack_into(
            ">IIII", revoke_record, 0, _JBD2_MAGIC, _JBD2_REVOKE, sequence, byte_count
        )
        for index, target in enumerate(chunk):
            struct.pack_into(">I", revoke_record, 16 + index * 4, target)
        struct.pack_into(
            ">I",
            revoke_record,
            block_size - 4,
            descriptor_checksum(bytes(revoke_record), seed),
        )
        records.append(bytes(revoke_record))

    commit = bytearray(block_size)
    struct.pack_into(">III", commit, 0, _JBD2_MAGIC, _JBD2_COMMIT, sequence)
    commit[12] = 0xFF
    commit[13] = 0xFF
    struct.pack_into(">I", commit, 16, commit_checksum(bytes(commit), seed))
    records.append(bytes(commit))
    return tuple(records)


def parse_classic_jbd2_superblock(
    raw: bytes | bytearray | memoryview,
    *,
    filesystem_block_size: int,
    filesystem_uuid: bytes,
    journal_blocks: int,
) -> JBD2JournalState:
    """Validate and decode a classic internal JBD2 journal superblock.

    Only the no-checksum, 32-bit-tag profile used by the bounded serializer is
    accepted.  The journal's declared ring must fit inside the journal inode;
    ``s_start == 0`` is the only accepted empty-ring representation.  The
    returned state can feed a later durable writer, but this function never
    writes the image or claims ext3/ext4 write support.
    """
    return parse_jbd2_superblock(
        raw,
        filesystem_block_size=filesystem_block_size,
        filesystem_uuid=filesystem_uuid,
        journal_blocks=journal_blocks,
        allow_checksums=False,
    )


_SUPPORTED_JBD2_INCOMPAT = (
    JBD2_FEATURE_INCOMPAT_REVOKE
    | JBD2_FEATURE_INCOMPAT_64BIT
    | JBD2_FEATURE_INCOMPAT_CSUM_V2
    | JBD2_FEATURE_INCOMPAT_CSUM_V3
)


def parse_jbd2_superblock(
    raw: bytes | bytearray | memoryview,
    *,
    filesystem_block_size: int,
    filesystem_uuid: bytes,
    journal_blocks: int,
    allow_checksums: bool = False,
) -> JBD2JournalState:
    """Validate a JBD2 superblock, optionally including checksum v2/v3.

    ``allow_checksums`` admits exactly one of ``CSUM_V2``/``CSUM_V3`` together
    with the ``REVOKE`` and ``64BIT`` feature bits.  A checksummed superblock
    must declare ``s_checksum_type == CRC32C`` and carry a matching
    ``s_checksum`` computed over bytes ``[0, 0xFC)``.  Unknown or mutually
    exclusive feature combinations remain typed refusals; the parser never
    downgrades a checksummed journal to the classic profile.
    """
    if not isinstance(raw, bytes | bytearray | memoryview):
        raise JBD2WriteProfileError("JBD2 superblock must be bytes-like")
    raw = bytes(raw)
    if len(raw) < 64:
        raise JBD2WriteProfileError("short JBD2 journal superblock")
    if filesystem_block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported filesystem block size")
    if not isinstance(journal_blocks, int) or isinstance(journal_blocks, bool):
        raise JBD2WriteProfileError("journal block count must be an integer")
    if journal_blocks < 2:
        raise JBD2WriteProfileError("journal inode is too small for a ring")
    try:
        expected_uuid = bytes(filesystem_uuid)
    except (TypeError, ValueError) as exc:
        raise JBD2WriteProfileError("filesystem UUID must be bytes-like") from exc
    if len(expected_uuid) != 16:
        raise JBD2WriteProfileError("filesystem UUID must be exactly 16 bytes")

    magic, block_type, _header_sequence = struct.unpack_from(">III", raw, 0)
    block_size, max_length, first, sequence, start = struct.unpack_from(
        ">IIIII", raw, 12
    )
    feature_compat, feature_incompat, feature_ro = struct.unpack_from(">III", raw, 36)
    journal_uuid = raw[48:64]
    if magic != _JBD2_MAGIC or block_type not in (3, 4):
        raise JBD2WriteProfileError("invalid JBD2 journal superblock header")
    if block_size != filesystem_block_size:
        raise JBD2WriteProfileError("JBD2 block size disagrees with filesystem")
    if max_length != journal_blocks or max_length <= first + 1:
        raise JBD2WriteProfileError("JBD2 ring length is outside journal inode bounds")
    if not 1 <= first < max_length:
        raise JBD2WriteProfileError("JBD2 ring first block is outside bounds")
    # ``h_sequence`` in a journal superblock is unused; the expected transaction
    # ID is ``s_sequence`` at 0x18.  ``s_start == 0`` is the canonical empty
    # ring, while a fresh journal legitimately keeps ``s_sequence == 1``.
    empty = start == 0
    if not empty and sequence == 0:
        raise JBD2WriteProfileError("JBD2 journal sequence must be nonzero")
    if journal_uuid != expected_uuid:
        raise JBD2WriteProfileError("JBD2 journal UUID does not match filesystem")
    if feature_compat or feature_ro:
        raise JBD2WriteProfileError("unsupported or unchecked JBD2 journal feature")
    checksum_bits = feature_incompat & JBD2_FEATURE_INCOMPAT_CHECKSUMS
    checksum_type = raw[0x50] if len(raw) > 0x50 else 0
    if not allow_checksums:
        # The classic decoder is a 32-bit, no-checksum profile.  Accepting a
        # feature bit it cannot decode would silently misread tags.
        if feature_incompat:
            raise JBD2WriteProfileError(
                "checksum or other JBD2 journal features are unsupported by the "
                "classic parser"
            )
    else:
        if feature_incompat & ~_SUPPORTED_JBD2_INCOMPAT:
            raise JBD2WriteProfileError("unsupported or unchecked JBD2 journal feature")
        if feature_incompat & JBD2_FEATURE_INCOMPAT_64BIT and not checksum_bits:
            raise JBD2WriteProfileError(
                "classic 64-bit JBD2 tags are outside the parsed profile"
            )
        if checksum_bits:
            if checksum_bits not in (
                JBD2_FEATURE_INCOMPAT_CSUM_V2,
                JBD2_FEATURE_INCOMPAT_CSUM_V3,
            ):
                raise JBD2WriteProfileError(
                    "JBD2 checksum v2 and v3 are mutually exclusive"
                )
            if len(raw) < filesystem_block_size:
                raise JBD2WriteProfileError("checksummed JBD2 superblock is truncated")
            try:
                # ``verify_journal_superblock_checksum`` *returns* a bool and
                # only raises for a malformed/unsupported checksum field.  The
                # return value used to be discarded, so a superblock whose
                # stored s_checksum simply did not match the journal content
                # was accepted as valid: probe_journal() then reported CLEAN or
                # NEEDS_REPLAY for a corrupt journal instead of ERROR.
                checksum_ok = verify_journal_superblock_checksum(
                    raw[:filesystem_block_size]
                )
            except JBD2ChecksumError as exc:
                raise JBD2WriteProfileError(
                    f"JBD2 journal superblock checksum is invalid: {exc}"
                ) from exc
            if not checksum_ok:
                raise JBD2WriteProfileError(
                    "JBD2 journal superblock checksum does not match its content"
                )
    if start != 0 and not first <= start < max_length:
        raise JBD2WriteProfileError("JBD2 journal start is outside ring bounds")
    return JBD2JournalState(
        block_size=block_size,
        max_length=max_length,
        first=first,
        sequence=sequence,
        start=start,
        uuid=journal_uuid,
        empty=empty or start == 0,
        feature_compat=feature_compat,
        feature_incompat=feature_incompat,
        feature_ro_compat=feature_ro,
        checksum_type=checksum_type if checksum_bits else 0,
    )


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
    only later tags use SAME_UUID. Data beginning with the JBD2 magic is zeroed
    in the journal and marked with ESCAPE in its descriptor tag. Checksums,
    64-bit block tags, and unknown journal profiles remain unsupported. A single
    descriptor block is permitted; oversized write sets fail closed. The caller
    remains responsible for ring capacity, transaction ordering, durable writes,
    checkpointing, recovery, and independent filesystem validation.
    """
    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    journal_uuid = _normalize_journal_uuid(journal_uuid)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise JBD2WriteProfileError("JBD2 sequence must be an integer")
    if not 0 <= sequence <= _U32_MAX:
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
        escaped = normalized_data.startswith(_JBD2_MAGIC_BYTES)
        journal_data = b"\x00" * 4 + normalized_data[4:] if escaped else normalized_data
        normalized_writes.append((target, journal_data, escaped))

    for target in revoke_items:
        _validate_target(target, filesystem_blocks, forbidden)

    write_targets = {target for target, _data, _escaped in normalized_writes}
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
        for index, (target, data, escaped) in enumerate(normalized_writes):
            flags = _JBD2_TAG_ESCAPE if escaped else 0
            if index:
                flags |= _JBD2_TAG_SAME_UUID
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
        escaped_data_flags = []
        while cursor + 8 <= block_size:
            target, flags = struct.unpack_from(">II", records[0], cursor)
            cursor += 8
            tag_count += 1
            if target == 0 or flags & ~(
                _JBD2_TAG_ESCAPE | _JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID
            ):
                raise JBD2WriteProfileError("JBD2 descriptor contains an invalid tag")
            if target in write_targets:
                raise JBD2WriteProfileError("JBD2 descriptor target is duplicated")
            write_targets.add(target)
            escaped_data_flags.append(bool(flags & _JBD2_TAG_ESCAPE))
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
        for escaped, data in zip(escaped_data_flags, records[1:index], strict=True):
            if escaped and data[:4] != b"\x00" * 4:
                raise JBD2WriteProfileError(
                    "JBD2 escaped data block has an invalid zero prefix"
                )
            if not escaped and data.startswith(_JBD2_MAGIC_BYTES):
                raise JBD2WriteProfileError(
                    "JBD2 data block magic prefix lacks ESCAPE flag"
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
