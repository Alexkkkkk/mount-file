# SPDX-License-Identifier: MIT
"""Small, bounded checksum primitives for read-only ext validation."""
from __future__ import annotations

_CRC32C_REVERSED_POLY = 0x82F63B78


def crc32c(data: bytes, seed: int = 0) -> int:
    """Return CRC32C/Castagnoli using the ext/JBD2 reflected convention.

    ``seed`` is the already-computed CRC value used by ext4 checksum-seed
    chaining. The function never mutates its input and always returns uint32.
    """
    crc = (~int(seed)) & 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (
                _CRC32C_REVERSED_POLY if crc & 1 else 0
            )
    return (~crc) & 0xFFFFFFFF


__all__ = ["crc32c"]
