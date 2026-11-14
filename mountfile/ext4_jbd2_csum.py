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
TAG_FLAG_DELETED = 0x00000008
TAIL_SIZE = 4
HEADER_SIZE = 12


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
    "JBD2ChecksumError",
    "MAGIC",
    "TAG_FLAG_DELETED",
    "commit_checksum",
    "descriptor_checksum",
    "tag_checksum",
    "tag_checksum_matches",
    "tid_after_or_equal",
    "uuid_seed",
    "verify_commit_checksum",
    "verify_descriptor_checksum",
]
