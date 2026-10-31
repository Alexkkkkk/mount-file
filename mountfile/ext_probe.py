# SPDX-License-Identifier: MIT
"""Bounded, read-only probe for ext2/ext3/ext4 superblocks.

This module classifies the on-disk profile and reports whether it resembles
Phase-1 ext2's feature allowlist. It does not parse directory or inode data and
never authorizes writes; a writer must repeat complete structural validation.
"""

from __future__ import annotations

import os
import struct
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from mountfile.ext_checksum import ext4_superblock_checksum


class ExtProbeError(ValueError):
    """The selected extent is not a structurally valid ext superblock."""


# ext superblock feature flags from the Linux kernel's on-disk specification.
_COMPAT_HAS_JOURNAL = 0x0004
_COMPAT_EXT_ATTR = 0x0008
_COMPAT_RESIZE_INODE = 0x0010
_COMPAT_SPARSE_SUPER2 = 0x0200
_COMPAT_FAST_COMMIT = 0x0400
_COMPAT_ORPHAN_FILE = 0x1000

_INCOMPAT_FILETYPE = 0x0002
_INCOMPAT_RECOVER = 0x0004
_INCOMPAT_JOURNAL_DEV = 0x0008
_INCOMPAT_EXTENTS = 0x0040
_INCOMPAT_64BIT = 0x0080
_INCOMPAT_FLEX_BG = 0x0200
_INCOMPAT_EA_INODE = 0x0400
_INCOMPAT_CSUM_SEED = 0x2000
_INCOMPAT_LARGEDIR = 0x4000
_INCOMPAT_INLINE_DATA = 0x8000
_INCOMPAT_ENCRYPT = 0x10000
_INCOMPAT_CASEFOLD = 0x20000

_RO_COMPAT_SPARSE_SUPER = 0x0001
_RO_COMPAT_LARGE_FILE = 0x0002
_RO_COMPAT_HUGE_FILE = 0x0008
_RO_COMPAT_GDT_CSUM = 0x0010
_RO_COMPAT_DIR_NLINK = 0x0020
_RO_COMPAT_EXTRA_ISIZE = 0x0040
_RO_COMPAT_BIGALLOC = 0x0200
_RO_COMPAT_METADATA_CSUM = 0x0400
_RO_COMPAT_READONLY = 0x1000
_RO_COMPAT_PROJECT = 0x2000
_RO_COMPAT_SHARED_BLOCKS = 0x4000
_RO_COMPAT_VERITY = 0x8000
_RO_COMPAT_ORPHAN_PRESENT = 0x10000

_KNOWN_INCOMPAT = (
    0x0001
    | _INCOMPAT_FILETYPE
    | _INCOMPAT_RECOVER
    | _INCOMPAT_JOURNAL_DEV
    | 0x0010  # meta block groups
    | _INCOMPAT_EXTENTS
    | _INCOMPAT_64BIT
    | 0x0100  # multiple mount protection
    | _INCOMPAT_FLEX_BG
    | _INCOMPAT_EA_INODE
    | 0x1000  # data in directory entries
    | _INCOMPAT_CSUM_SEED
    | _INCOMPAT_LARGEDIR
    | _INCOMPAT_INLINE_DATA
    | _INCOMPAT_ENCRYPT
    | _INCOMPAT_CASEFOLD
)

_KNOWN_RO_COMPAT = (
    _RO_COMPAT_SPARSE_SUPER
    | _RO_COMPAT_LARGE_FILE
    | _RO_COMPAT_HUGE_FILE
    | _RO_COMPAT_GDT_CSUM
    | _RO_COMPAT_DIR_NLINK
    | _RO_COMPAT_EXTRA_ISIZE
    | _RO_COMPAT_BIGALLOC
    | _RO_COMPAT_METADATA_CSUM
    | _RO_COMPAT_READONLY
    | _RO_COMPAT_PROJECT
    | _RO_COMPAT_SHARED_BLOCKS
    | _RO_COMPAT_VERITY
    | _RO_COMPAT_ORPHAN_PRESENT
)

# These mirrors the strict Phase-1 ext2 writer profile; they are only a
# preliminary classification and are not sufficient authorization to mutate.
_PHASE1_COMPAT = _COMPAT_EXT_ATTR | _COMPAT_RESIZE_INODE
_PHASE1_INCOMPAT = _INCOMPAT_FILETYPE
_PHASE1_RO_COMPAT = _RO_COMPAT_SPARSE_SUPER | _RO_COMPAT_LARGE_FILE

_EXT4_MARKERS_INCOMPAT = (
    _INCOMPAT_EXTENTS
    | _INCOMPAT_64BIT
    | _INCOMPAT_FLEX_BG
    | _INCOMPAT_EA_INODE
    | _INCOMPAT_CSUM_SEED
    | _INCOMPAT_LARGEDIR
    | _INCOMPAT_INLINE_DATA
    | _INCOMPAT_ENCRYPT
    | _INCOMPAT_CASEFOLD
)
_EXT4_MARKERS_COMPAT = _COMPAT_SPARSE_SUPER2 | _COMPAT_FAST_COMMIT | _COMPAT_ORPHAN_FILE
_EXT4_MARKERS_RO_COMPAT = (
    _RO_COMPAT_HUGE_FILE
    | _RO_COMPAT_GDT_CSUM
    | _RO_COMPAT_DIR_NLINK
    | _RO_COMPAT_EXTRA_ISIZE
    | _RO_COMPAT_BIGALLOC
    | _RO_COMPAT_METADATA_CSUM
    | _RO_COMPAT_READONLY
    | _RO_COMPAT_PROJECT
    | _RO_COMPAT_SHARED_BLOCKS
    | _RO_COMPAT_VERITY
    | _RO_COMPAT_ORPHAN_PRESENT
)


@dataclass(frozen=True)
class ExtSuperblockProfile:
    """Parsed superblock facts and a conservative Phase-1 profile decision."""

    fs_type: str
    offset: int
    volume_size: int
    filesystem_size: int
    block_size: int
    blocks_count: int
    inodes_count: int
    first_data_block: int
    blocks_per_group: int
    inodes_per_group: int
    inode_size: int
    group_descriptor_size: int
    filesystem_uuid: bytes
    revision: int
    state: int
    feature_compat: int
    feature_incompat: int
    feature_ro_compat: int
    label: str
    clean_unmounted: bool
    has_journal: bool
    journal_inode: int | None
    journal_recovery_needed: bool
    unknown_incompat_features: int
    unknown_ro_compat_features: int
    phase1_ext2_profile_candidate: bool
    write_gate_reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _u16(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<H", buf, offset)[0]


def _u32(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def _checked_add(left: int, right: int, limit: int, message: str) -> int:
    if left < 0 or right < 0 or left > limit - right:
        raise ExtProbeError(message)
    return left + right


def _checked_mul(left: int, right: int, limit: int, message: str) -> int:
    if left < 0 or right < 0 or (left and right > limit // left):
        raise ExtProbeError(message)
    return left * right


def _validate_superblock_checksum(
    superblock: bytes, feature_incompat: int, feature_ro_compat: int
) -> None:
    """Validate the ext4 superblock checksum before exposing its profile."""
    if not feature_ro_compat & _RO_COMPAT_METADATA_CSUM:
        return
    if superblock[0x175] != 1:
        raise ExtProbeError("unsupported ext superblock checksum type")
    expected = _u32(superblock, 0x3FC)
    actual = ext4_superblock_checksum(superblock)
    if actual != expected:
        raise ExtProbeError("ext superblock checksum mismatch")


def _stream_size(stream: BinaryIO) -> int:
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(position)
    except (AttributeError, OSError, ValueError) as exc:
        raise ExtProbeError("source must be a seekable binary stream") from exc
    if size < 0:
        raise ExtProbeError("invalid image size")
    return size


def _probe_stream(
    stream: BinaryIO, offset: int, volume_size: int | None
) -> ExtSuperblockProfile:
    if offset < 0:
        raise ExtProbeError("negative filesystem offset")
    image_size = _stream_size(stream)
    if offset > image_size:
        raise ExtProbeError("filesystem offset is outside the image")
    extent_size = image_size - offset if volume_size is None else int(volume_size)
    _checked_add(
        offset,
        extent_size,
        image_size,
        "selected filesystem extent is outside the image",
    )
    if extent_size < 2048:
        raise ExtProbeError("selected extent is too small for an ext superblock")

    superblock_offset = offset + 1024
    position = None
    try:
        position = stream.tell()
        stream.seek(superblock_offset)
        superblock = stream.read(1024)
    except (AttributeError, OSError, ValueError) as exc:
        raise ExtProbeError("cannot read ext superblock") from exc
    finally:
        if position is not None:
            with suppress(AttributeError, OSError, ValueError):
                stream.seek(position)
    if len(superblock) != 1024:
        raise ExtProbeError("short ext superblock read")
    if _u16(superblock, 0x38) != 0xEF53:
        raise ExtProbeError("no ext superblock at the selected offset")

    revision = _u32(superblock, 0x4C)
    block_log = _u32(superblock, 0x18)
    if block_log > 6:
        raise ExtProbeError("unsupported ext block-size encoding")
    block_size = 1 << (10 + block_log)
    blocks_low = _u32(superblock, 0x04)
    inodes_count = _u32(superblock, 0x00)
    first_data_block = _u32(superblock, 0x14)
    blocks_per_group = _u32(superblock, 0x20)
    inodes_per_group = _u32(superblock, 0x28)
    feature_compat = _u32(superblock, 0x5C)
    feature_incompat = _u32(superblock, 0x60)
    feature_ro_compat = _u32(superblock, 0x64)
    _validate_superblock_checksum(superblock, feature_incompat, feature_ro_compat)
    if feature_incompat & _INCOMPAT_64BIT:
        blocks_count = blocks_low | (_u32(superblock, 0x150) << 32)
    else:
        blocks_count = blocks_low
    inode_size = _u16(superblock, 0x58) if revision else 128
    group_descriptor_size = (_u16(superblock, 0xFE) or 32) if revision else 32
    state = _u16(superblock, 0x3A)

    if blocks_count <= first_data_block or inodes_count == 0:
        raise ExtProbeError("invalid ext block or inode count")
    if blocks_per_group == 0 or inodes_per_group == 0:
        raise ExtProbeError("invalid ext block-group geometry")
    if inode_size < 128 or inode_size > block_size or inode_size % 4:
        raise ExtProbeError("invalid ext inode size")
    filesystem_size = _checked_mul(
        blocks_count,
        block_size,
        extent_size,
        "ext filesystem exceeds selected partition bounds",
    )
    if filesystem_size > extent_size:
        raise ExtProbeError("ext filesystem exceeds selected partition bounds")
    if offset + filesystem_size > image_size:
        raise ExtProbeError("ext filesystem exceeds image bounds")
    if (
        group_descriptor_size < 32
        or group_descriptor_size > block_size
        or group_descriptor_size % 8
    ):
        raise ExtProbeError("invalid ext group-descriptor size")
    if feature_incompat & _INCOMPAT_64BIT and group_descriptor_size < 64:
        raise ExtProbeError("64-bit ext profile requires 64-byte group descriptors")
    if not feature_incompat & _INCOMPAT_64BIT and group_descriptor_size != 32:
        raise ExtProbeError("non-64-bit ext profile has a non-classic descriptor size")

    has_journal = bool(
        feature_compat & _COMPAT_HAS_JOURNAL or feature_incompat & _INCOMPAT_JOURNAL_DEV
    )
    journal_inode = None
    if has_journal and not feature_incompat & _INCOMPAT_JOURNAL_DEV:
        journal_inode_value = _u32(superblock, 0xE0)
        if not 1 <= journal_inode_value <= inodes_count:
            raise ExtProbeError("internal journal inode is outside inode table")
        journal_inode = journal_inode_value
    recovery_needed = bool(feature_incompat & _INCOMPAT_RECOVER)
    ext4_markers = bool(
        feature_incompat & _EXT4_MARKERS_INCOMPAT
        or feature_compat & _EXT4_MARKERS_COMPAT
        or feature_ro_compat & _EXT4_MARKERS_RO_COMPAT
    )
    fs_type = "ext4" if ext4_markers else "ext3" if has_journal else "ext2"
    state_clean = state == 0x0001
    unknown_incompat = feature_incompat & ~_KNOWN_INCOMPAT
    unknown_ro_compat = feature_ro_compat & ~_KNOWN_RO_COMPAT

    reasons = []
    if fs_type != "ext2":
        reasons.append(
            "journaled or ext4-featured profiles are read-only in this phase"
        )
    if has_journal:
        reasons.append("JBD2 journaled profiles are outside the Phase-1 ext2 writer")
    if recovery_needed:
        reasons.append("superblock marks the filesystem as needing journal recovery")
    if not state_clean:
        reasons.append("superblock state is not exactly cleanly unmounted")
    if unknown_incompat:
        reasons.append(f"unknown incompatible feature flags 0x{unknown_incompat:08x}")
    if unknown_ro_compat:
        reasons.append(
            f"unknown read-only-compatible feature flags 0x{unknown_ro_compat:08x}"
        )
    if revision > 1:
        reasons.append("unsupported ext superblock revision")
    if blocks_per_group > block_size * 8 and not (
        feature_ro_compat & _RO_COMPAT_BIGALLOC
    ):
        reasons.append("block-group size exceeds the ext block bitmap capacity")
    if inodes_per_group > block_size * 8:
        reasons.append("inode-group size exceeds the ext inode bitmap capacity")
    group_numerator = _checked_add(
        blocks_count - first_data_block,
        blocks_per_group - 1,
        blocks_count + blocks_per_group,
        "ext block-group geometry arithmetic overflow",
    )
    inode_numerator = _checked_add(
        inodes_count,
        inodes_per_group - 1,
        inodes_count + inodes_per_group,
        "ext inode-group geometry arithmetic overflow",
    )
    group_count = group_numerator // blocks_per_group
    inode_group_count = inode_numerator // inodes_per_group
    if not 1 <= group_count <= 1_000_000:
        reasons.append("ext block-group count is outside the bounded reader limit")
    if inode_group_count > group_count:
        reasons.append("inode groups exceed ext block groups")
    gdt_offset = _checked_mul(
        _checked_add(
            first_data_block,
            1,
            blocks_count + 1,
            "group descriptor offset arithmetic overflow",
        ),
        block_size,
        filesystem_size,
        "group descriptor offset exceeds filesystem bounds",
    )
    gdt_bytes = _checked_mul(
        group_count,
        group_descriptor_size,
        filesystem_size,
        "group descriptor table size exceeds filesystem bounds",
    )
    if (
        _checked_add(
            gdt_offset,
            gdt_bytes,
            filesystem_size,
            "group descriptor table exceeds filesystem bounds",
        )
        > filesystem_size
    ):
        reasons.append("group descriptor table exceeds filesystem bounds")
    if block_size not in (1024, 2048, 4096):
        reasons.append("Phase-1 ext2 writer supports only 1, 2 or 4 KiB blocks")
    if feature_compat & ~_PHASE1_COMPAT:
        reasons.append("compatible features exceed the Phase-1 ext2 allowlist")
    if feature_incompat != _PHASE1_INCOMPAT:
        reasons.append("incompatible features exceed the Phase-1 ext2 allowlist")
    if feature_ro_compat & ~_PHASE1_RO_COMPAT:
        reasons.append(
            "read-only-compatible features exceed the Phase-1 ext2 allowlist"
        )

    candidate = not reasons
    return ExtSuperblockProfile(
        fs_type=fs_type,
        offset=offset,
        volume_size=extent_size,
        filesystem_size=filesystem_size,
        block_size=block_size,
        blocks_count=blocks_count,
        inodes_count=inodes_count,
        first_data_block=first_data_block,
        blocks_per_group=blocks_per_group,
        inodes_per_group=inodes_per_group,
        inode_size=inode_size,
        group_descriptor_size=group_descriptor_size,
        filesystem_uuid=bytes(superblock[0x68:0x78]),
        revision=revision,
        state=state,
        feature_compat=feature_compat,
        feature_incompat=feature_incompat,
        feature_ro_compat=feature_ro_compat,
        label=superblock[0x78:0x88].split(b"\x00", 1)[0].decode("utf-8", "replace"),
        clean_unmounted=state_clean and not recovery_needed,
        has_journal=has_journal,
        journal_inode=journal_inode,
        journal_recovery_needed=recovery_needed,
        unknown_incompat_features=unknown_incompat,
        unknown_ro_compat_features=unknown_ro_compat,
        phase1_ext2_profile_candidate=candidate,
        write_gate_reasons=tuple(reasons),
    )


def probe_ext_superblock(
    source: str | os.PathLike[str] | BinaryIO,
    offset: int = 0,
    volume_size: int | None = None,
) -> ExtSuperblockProfile:
    """Read only the primary ext superblock and conservatively classify it.

    ``phase1_ext2_profile_candidate`` is a preflight hint, not write
    authorization. The selected writer must still validate all metadata and
    partition bounds before every operation.
    """
    offset = int(offset)
    if isinstance(source, (str, os.PathLike, Path)):
        try:
            with Path(source).open("rb") as stream:
                return _probe_stream(stream, offset, volume_size)
        except OSError as exc:
            raise ExtProbeError("cannot open image for read-only probe") from exc
    return _probe_stream(source, offset, volume_size)
