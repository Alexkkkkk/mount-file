# SPDX-License-Identifier: MIT
"""Read-only, fail-closed JBD2 checksum v2/v3 primitives.

This module deliberately does not replay transactions or mutate images. The
algorithms mirror current Linux JBD2: CRC32C uses the UUID-derived running
seed, descriptor/revoke tails hash the complete block with a zeroed tail,
data tags hash sequence followed by the complete data block, and commit
checksums hash the complete block with checksum fields zeroed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from mountfile.ext_checksum import crc32c_raw

MAGIC = 0xC03B3998
TAG_FLAG_ESCAPE = 0x00000001
TAG_FLAG_SAME_UUID = 0x00000002
TAG_FLAG_DELETED = 0x00000004
TAG_FLAG_LAST = 0x00000008
TAIL_SIZE = 4
HEADER_SIZE = 12

# JBD2 superblock feature flags (include/linux/jbd2.h).
JBD2_FEATURE_COMPAT_CHECKSUM = 0x00000001
JBD2_FEATURE_INCOMPAT_REVOKE = 0x00000001
JBD2_FEATURE_INCOMPAT_64BIT = 0x00000002
JBD2_FEATURE_INCOMPAT_ASYNC_COMMIT = 0x00000004
JBD2_FEATURE_INCOMPAT_CSUM_V2 = 0x00000008
JBD2_FEATURE_INCOMPAT_CSUM_V3 = 0x00000010
JBD2_FEATURE_INCOMPAT_FAST_COMMIT = 0x00000020
JBD2_FEATURE_INCOMPAT_CHECKSUMS = (
    JBD2_FEATURE_INCOMPAT_CSUM_V2 | JBD2_FEATURE_INCOMPAT_CSUM_V3
)
# ``JBD2_CRC32C_CHKSUM`` is 4 in both include/linux/jbd2.h and e2fsprogs'
# kernel-jbd.h; it is a checksum-algorithm identifier, not a boolean.
JBD2_CRC32C_CHKSUM = 4

# ``journal_superblock_t`` places ``s_checksum_type`` at 0x50 and the trailing
# ``s_checksum`` at 0xFC; e2fsprogs hashes bytes [0, 0xFC) with raw seed ~0.
SUPERBLOCK_CHECKSUM_OFFSET = 0xFC
SUPERBLOCK_CHECKSUM_TYPE_OFFSET = 0x50


class JBD2ChecksumError(ValueError):
    """Malformed or unverifiable JBD2 checksum input."""


def _be32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise JBD2ChecksumError("truncated big-endian uint32")
    return struct.unpack_from(">I", data, offset)[0]


def uuid_seed(uuid: bytes) -> int:
    """Return JBD2's UUID-derived CRC32C running state."""
    if len(uuid) != 16:
        raise JBD2ChecksumError("JBD2 UUID must be exactly 16 bytes")
    return crc32c_raw(uuid, 0xFFFFFFFF)


def _with_zeroed(data: bytes, start: int, size: int) -> bytes:
    if start < 0 or size < 0 or start + size > len(data):
        raise JBD2ChecksumError("checksum field lies outside the block")
    result = bytearray(data)
    result[start : start + size] = b"\0" * size
    return bytes(result)


def descriptor_checksum(block: bytes, seed: int) -> int:
    """Calculate a descriptor or revoke checksum with its final tail zeroed."""
    if len(block) < HEADER_SIZE + TAIL_SIZE:
        raise JBD2ChecksumError("JBD2 block is too small for a checksum tail")
    return crc32c_raw(_with_zeroed(block, len(block) - TAIL_SIZE, TAIL_SIZE), seed)


def verify_descriptor_checksum(block: bytes, seed: int) -> bool:
    if len(block) < TAIL_SIZE:
        raise JBD2ChecksumError("JBD2 block is too small")
    return _be32(block, len(block) - TAIL_SIZE) == descriptor_checksum(block, seed)


def tag_checksum(sequence: int, data_block: bytes, seed: int) -> int:
    """Calculate v2/v3 tag checksum: BE32 sequence then full data block."""
    if not 0 <= sequence <= 0xFFFFFFFF:
        raise JBD2ChecksumError("transaction sequence is not uint32")
    state = crc32c_raw(struct.pack(">I", sequence), seed)
    return crc32c_raw(data_block, state)


def tag_checksum_matches(
    sequence: int,
    data_block: bytes,
    stored: int,
    seed: int,
    *,
    deleted: bool = False,
    width: int = 4,
) -> bool:
    """Verify a tag checksum; deleted tags have no data payload to verify."""
    if width not in (2, 4):
        raise JBD2ChecksumError("tag checksum width must be 2 or 4 bytes")
    if deleted:
        return True
    expected = tag_checksum(sequence, data_block, seed)
    return (expected & (0xFFFF if width == 2 else 0xFFFFFFFF)) == stored


def commit_checksum(block: bytes, seed: int) -> int:
    """Calculate v2/v3 commit checksum over the complete journal block."""
    # commit_header: type at 12, size at 13, padding at 14..15, csum[0] at 16.
    if len(block) < 20:
        raise JBD2ChecksumError("commit block is too small")
    return crc32c_raw(_with_zeroed(_with_zeroed(block, 12, 2), 16, 4), seed)


def verify_commit_checksum(block: bytes, seed: int) -> bool:
    if len(block) < 20:
        raise JBD2ChecksumError("commit block is too small")
    return _be32(block, 16) == commit_checksum(block, seed)


def tid_after_or_equal(candidate: int, expected: int) -> bool:
    """JBD2 modulo-uint32 ordering, valid only within the half-range."""
    if not (0 <= candidate <= 0xFFFFFFFF and 0 <= expected <= 0xFFFFFFFF):
        raise JBD2ChecksumError("transaction ID is not uint32")
    difference = (candidate - expected) & 0xFFFFFFFF
    return difference == 0 or difference < 0x80000000


def journal_superblock_checksum(block: bytes) -> int:
    """Return ``s_checksum`` for a v2/v3 JBD2 superblock.

    e2fsprogs (``e2fsck_journal_sb_csum``) and the kernel
    (``jbd2_superblock_csum``) zero ``s_checksum`` and hash the complete
    1024-byte ``journal_superblock_t`` with the raw CRC32C seed ``~0``.
    Hashing only the bytes before the field is *not* equivalent: the four
    zeroed checksum bytes and the trailing ``s_users`` array change the CRC
    state.  Unlike descriptor/commit blocks this checksum is not UUID-seeded.
    """
    if len(block) < 1024:
        raise JBD2ChecksumError("JBD2 superblock is too small for its checksum")
    view = bytearray(block[:1024])
    view[SUPERBLOCK_CHECKSUM_OFFSET : SUPERBLOCK_CHECKSUM_OFFSET + 4] = b"\x00" * 4
    return crc32c_raw(bytes(view), 0xFFFFFFFF)


def verify_journal_superblock_checksum(block: bytes) -> bool:
    """Verify a v2/v3 journal superblock checksum (``s_checksum_type == 1``)."""
    if len(block) < 1024:
        raise JBD2ChecksumError("JBD2 superblock is too small for its checksum")
    if block[SUPERBLOCK_CHECKSUM_TYPE_OFFSET] != JBD2_CRC32C_CHKSUM:
        raise JBD2ChecksumError(
            "JBD2 journal superblock checksum type is not CRC32C"
        )
    return _be32(block, SUPERBLOCK_CHECKSUM_OFFSET) == journal_superblock_checksum(
        block
    )


def tag_bytes_for_features(feature_incompat: int) -> int:
    """Return ``journal_tag_bytes()`` for a validated JBD2 feature word.

    Mirrors ``journal_tag_bytes()`` in ``fs/jbd2/journal.c``: v3 tags are 16
    bytes, v2 tags are 12 bytes plus the two-byte checksum field, and the
    64-bit block-number word is only present when INCOMPAT_64BIT is set.
    """
    if isinstance(feature_incompat, bool) or not isinstance(feature_incompat, int):
        raise JBD2ChecksumError("JBD2 feature word must be an integer")
    if feature_incompat & ~0x3F:
        raise JBD2ChecksumError("JBD2 feature word has unknown bits")
    has_v2 = bool(feature_incompat & JBD2_FEATURE_INCOMPAT_CSUM_V2)
    has_v3 = bool(feature_incompat & JBD2_FEATURE_INCOMPAT_CSUM_V3)
    if has_v2 and has_v3:
        raise JBD2ChecksumError("JBD2 checksum v2 and v3 are mutually exclusive")
    if has_v3:
        return 16
    size = 12
    if has_v2:
        size += 2
    if feature_incompat & JBD2_FEATURE_INCOMPAT_64BIT:
        return size
    return size - 4


def parse_tag(
    block: bytes, offset: int, tag_size: int
) -> tuple[int, int, int, int]:
    """Decode one descriptor tag.

    Returns ``(target_block, flags, checksum, next_offset)``.  The v2 layout is
    ``journal_block_tag_s`` (block number, 16-bit checksum, flags, optional high
    word); the v3 layout is ``journal_block_tag3_s`` (block number, 32-bit
    flags, high word, 32-bit checksum).  The extra padding byte pairs of the v2
    profile are skipped through ``tag_size``.
    """
    if tag_size not in (10, 14, 16):
        raise JBD2ChecksumError("unsupported JBD2 tag size")
    if offset < 0 or offset + tag_size > len(block):
        raise JBD2ChecksumError("JBD2 descriptor tag is truncated")
    if tag_size == 16:
        low, flags, high, checksum = struct.unpack_from(">IIII", block, offset)
        target = low | (high << 32)
        return target, flags, checksum, offset + 16
    low = _be32(block, offset)
    checksum = struct.unpack_from(">H", block, offset + 4)[0]
    flags = struct.unpack_from(">H", block, offset + 6)[0]
    high = 0
    if tag_size == 14:
        high = _be32(block, offset + 8)
    target = low | (high << 32)
    return target, flags, checksum, offset + tag_size


@dataclass(frozen=True)
class DescriptorLayout:
    """Validated bounded layout for a v2/v3 descriptor block."""

    block_size: int
    tag_size: int
    has_uuid: bool = True

    def __post_init__(self) -> None:
        if self.block_size < HEADER_SIZE + TAIL_SIZE:
            raise JBD2ChecksumError("descriptor block size is too small")
        if self.tag_size not in (10, 14, 16):
            raise JBD2ChecksumError("unsupported or ambiguous JBD2 tag size")
        if self.has_uuid and self.block_size < HEADER_SIZE + 16 + TAIL_SIZE:
            raise JBD2ChecksumError("descriptor UUID does not fit")

    @property
    def tag_area_end(self) -> int:
        return self.block_size - TAIL_SIZE

    def first_tag_offset(self) -> int:
        return HEADER_SIZE + (16 if self.has_uuid else 0)


__all__ = [
    "DescriptorLayout",
    "JBD2_CRC32C_CHKSUM",
    "JBD2_FEATURE_COMPAT_CHECKSUM",
    "JBD2_FEATURE_INCOMPAT_64BIT",
    "JBD2_FEATURE_INCOMPAT_ASYNC_COMMIT",
    "JBD2_FEATURE_INCOMPAT_CHECKSUMS",
    "JBD2_FEATURE_INCOMPAT_CSUM_V2",
    "JBD2_FEATURE_INCOMPAT_CSUM_V3",
    "JBD2_FEATURE_INCOMPAT_FAST_COMMIT",
    "JBD2_FEATURE_INCOMPAT_REVOKE",
    "JBD2ChecksumError",
    "MAGIC",
    "SUPERBLOCK_CHECKSUM_OFFSET",
    "SUPERBLOCK_CHECKSUM_TYPE_OFFSET",
    "TAG_FLAG_ESCAPE",
    "TAG_FLAG_LAST",
    "TAG_FLAG_SAME_UUID",
    "TAG_FLAG_DELETED",
    "commit_checksum",
    "descriptor_checksum",
    "journal_superblock_checksum",
    "parse_tag",
    "tag_bytes_for_features",
    "tag_checksum",
    "tag_checksum_matches",
    "tid_after_or_equal",
    "uuid_seed",
    "verify_commit_checksum",
    "verify_descriptor_checksum",
    "verify_journal_superblock_checksum",
]
