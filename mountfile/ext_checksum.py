# SPDX-License-Identifier: MIT
"""Small, bounded checksum primitives for read-only ext validation."""
from __future__ import annotations

_CRC32C_REVERSED_POLY = 0x82F63B78


def crc32c_raw(data: bytes, seed: int = 0xFFFFFFFF) -> int:
    """Return the unfinalized reflected CRC32C state used by ext metadata.

    Linux and e2fsprogs accept and return the running register value. They do
    not apply the usual initial/final XOR used by display-oriented CRC32C
    implementations, so retain that primitive separately for metadata checks.
    """
    crc = int(seed) & 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (_CRC32C_REVERSED_POLY if crc & 1 else 0)
    return crc & 0xFFFFFFFF


def crc32c(data: bytes, seed: int = 0) -> int:
    """Return CRC32C/Castagnoli using the ext/JBD2 reflected convention.

    ``seed`` is the already-computed CRC value used by ext4 checksum-seed
    chaining. The function never mutates its input and always returns uint32.
    """
    return (~crc32c_raw(data, (~int(seed)) & 0xFFFFFFFF)) & 0xFFFFFFFF


def ext4_superblock_checksum(superblock: bytes) -> int:
    """Return ``s_checksum`` for a 1024-byte metadata-csum superblock.

    e2fsprogs hashes the on-disk superblock from byte zero through the byte
    immediately before ``s_checksum`` with raw CRC32C seed ``~0``. The UUID is
    already inside that byte range; checksum seeds apply to other ext4 metadata,
    not this structure.
    """
    if len(superblock) != 1024:
        raise ValueError("an ext4 superblock checksum needs exactly 1024 bytes")
    return crc32c_raw(superblock[:0x3FC])


__all__ = ["crc32c", "crc32c_raw", "ext4_superblock_checksum"]
