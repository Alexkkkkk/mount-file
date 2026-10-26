# SPDX-License-Identifier: MIT
"""Conservative read-only ext3/ext4 profiles.

This module intentionally supports only the classic ext block-pointer layout.
Extent trees, 64-bit group descriptors, metadata checksums, fast commits and
other layouts are rejected until their complete validators exist. Journal
replay is never performed; a journal that is not provably empty is rejected.
"""
from __future__ import annotations

import struct

from mountfile.ext2_reader import (
    _COMPAT_EXT_ATTR,
    _COMPAT_HAS_JOURNAL,
    _COMPAT_RESIZE_INODE,
    _EXT4_EA_INODE_FL,
    _EXT4_ENCRYPT_FL,
    _EXT4_EXTENTS_FL,
    _EXT4_INLINE_DATA_FL,
    _EXT4_VERITY_FL,
    _INCOMPAT_FILETYPE,
    _RO_COMPAT_LARGE_FILE,
    _RO_COMPAT_SPARSE_SUPER,
    _S_IFREG,
    Ext2ReadError,
    NativeExt2Image,
    _GroupDescriptor,
    _Inode,
)

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_MAX_JOURNAL_BLOCKS = 1 << 20
_RO_COMPAT_EXTRA_ISIZE = 0x0040


class Ext3ReadError(Ext2ReadError):
    """The conservative ext3/ext4 read-only profile was rejected."""


class NativeExt3Image(NativeExt2Image):
    """Read classic-pointer ext3/ext4 images without journal replay.

    The inherited block, inode, directory and bounded file readers are safe for
    the classic layout. This subclass changes only the profile gate and adds a
    strict internal-JBD2 empty-journal check.
    """

    backend_name = "native-ext3-ro"

    @staticmethod
    def _validate_profile(profile) -> None:
        if profile.fs_type not in ("ext3", "ext4"):
            raise Ext3ReadError(
                "native ext3/ext4 reader requires an ext3 or ext4 profile"
            )
        if not profile.clean_unmounted or profile.state != 0x0001:
            raise Ext3ReadError("dirty or recovery-required ext volume")
        if profile.journal_recovery_needed:
            raise Ext3ReadError("superblock requires journal recovery")
        if profile.revision not in (0, 1):
            raise Ext3ReadError("unsupported ext superblock revision")
        if profile.block_size not in (1024, 2048, 4096):
            raise Ext3ReadError("reader supports only 1, 2 or 4 KiB blocks")
        if profile.first_data_block != (1 if profile.block_size == 1024 else 0):
            raise Ext3ReadError("invalid first-data-block value")
        compat_allowed = _COMPAT_EXT_ATTR | _COMPAT_RESIZE_INODE | _COMPAT_HAS_JOURNAL
        if profile.feature_compat & ~compat_allowed:
            raise Ext3ReadError("unsupported compatible ext feature")
        if profile.feature_incompat & 0x0008:  # JOURNAL_DEV
            raise Ext3ReadError("external JBD2 journal devices are not supported")
        incompat_allowed = _INCOMPAT_FILETYPE
        if profile.fs_type == "ext4":
            incompat_allowed |= 0x00C0  # EXTENTS | 64BIT
        if profile.feature_incompat & ~incompat_allowed:
            raise Ext3ReadError("unsupported incompatible ext feature")
        ro_allowed = _RO_COMPAT_SPARSE_SUPER | _RO_COMPAT_LARGE_FILE
        if profile.fs_type == "ext4":
            ro_allowed |= _RO_COMPAT_EXTRA_ISIZE | 0x0020  # DIR_NLINK
        if profile.feature_ro_compat & ~ro_allowed:
            raise Ext3ReadError("unsupported read-only-compatible ext feature")
        if profile.fs_type == "ext3" and not profile.has_journal:
            raise Ext3ReadError("ext3 profile has no journal")
        if profile.feature_compat & _COMPAT_HAS_JOURNAL and not profile.has_journal:
            raise Ext3ReadError("journal feature is inconsistent")
        if profile.feature_incompat & 0x0040 and profile.fs_type != "ext4":  # EXTENTS
            raise Ext3ReadError("extent layout is not enabled in classic profile")
        if profile.feature_incompat & 0x0080 and profile.fs_type != "ext4":  # 64BIT
            raise Ext3ReadError("64-bit group layout is not enabled in classic profile")
        if profile.feature_ro_compat & 0x0400:  # METADATA_CSUM
            raise Ext3ReadError(
                "metadata checksums are not implemented in this profile"
            )

    def __init__(self, image_path, offset=0, volume_size=None):
        super().__init__(image_path, offset=offset, volume_size=volume_size)
        try:
            self.has_journal = bool(self.feature_compat & _COMPAT_HAS_JOURNAL)
            if self.has_journal:
                self._validate_empty_journal()
        except Ext2ReadError as exc:
            self.close()
            raise Ext3ReadError(f"journal inode/JBD2 validation failed: {exc}") from exc
        except Exception:
            self.close()
            raise

    def _journal_block(self, inode, logical):
        if logical < 0:
            raise Ext3ReadError("negative journal block")
        physical = self._logical_block(inode, logical)
        if physical == 0:
            raise Ext3ReadError("journal contains a sparse block")
        return self._read_block(physical)

    def _validate_empty_journal(self) -> None:
        journal = self._read_inode(8)
        if not journal.is_regular:
            raise Ext3ReadError("journal inode is not a regular file")
        if journal.size < self.block_size or journal.size % self.block_size:
            raise Ext3ReadError("journal size is not block aligned")
        journal_blocks = journal.size // self.block_size
        if journal_blocks > _MAX_JOURNAL_BLOCKS:
            raise Ext3ReadError("journal exceeds conservative scan limit")
        raw = self._journal_block(journal, 0)
        if len(raw) < 0x24:
            raise Ext3ReadError("short JBD2 superblock")
        magic, block_type, _sequence = struct.unpack_from(">III", raw, 0)
        if magic != _JBD2_MAGIC or block_type not in (3, 4):
            raise Ext3ReadError("invalid JBD2 journal superblock")
        if raw[0x30:0x40] != self.profile.filesystem_uuid:
            raise Ext3ReadError("JBD2 journal UUID does not match filesystem UUID")
        journal_block_size, max_length = struct.unpack_from(">II", raw, 0x0C)
        first, start = struct.unpack_from(">II", raw, 0x14)
        journal_compat, journal_incompat, journal_ro_compat = struct.unpack_from(
            ">III", raw, 0x24
        )
        if journal_compat or journal_incompat or journal_ro_compat:
            raise Ext3ReadError("unsupported or unchecked JBD2 journal feature")
        if journal_block_size != self.block_size:
            raise Ext3ReadError("JBD2 block size disagrees with filesystem")
        if not 1 <= first < max_length <= journal_blocks:
            raise Ext3ReadError("JBD2 ring geometry exceeds journal inode")
        # s_start == 0 is not sufficient proof of cleanliness. Scan the whole
        # ring and accept only an all-zero unused area. We do not replay or
        # interpret stale transactions.
        if start != 0:
            raise Ext3ReadError("JBD2 journal contains an uncheckpointed transaction")
        for logical in range(first, max_length):
            block = self._journal_block(journal, logical)
            if not block.strip(b"\x00"):
                continue
            candidate = struct.unpack_from(">I", block, 0)[0]
            if candidate != _JBD2_MAGIC:
                raise Ext3ReadError("non-empty or malformed JBD2 journal area")
            block_kind = struct.unpack_from(">I", block, 4)[0]
            if block_kind in (_JBD2_DESCRIPTOR, _JBD2_COMMIT, _JBD2_REVOKE):
                raise Ext3ReadError("JBD2 journal contains transaction records")
            raise Ext3ReadError("unsupported JBD2 journal record")


class NativeExt4Image(NativeExt3Image):
    """Classic or extent-tree ext4 read-only profile with strict bounds."""

    backend_name = "native-ext4-ro"

    @staticmethod
    def _validate_profile(profile) -> None:
        if profile.fs_type != "ext4":
            raise Ext3ReadError("native ext4 reader requires an ext4 profile")
        NativeExt3Image._validate_profile(profile)

    def _read_inode(self, number: int) -> _Inode:
        """Read an inode while permitting only the explicitly parsed extents flag."""
        if not 1 <= number <= self.inodes_count:
            raise Ext3ReadError("inode number is outside filesystem bounds")
        cached = self._inode_cache.get(number)
        if cached is not None:
            return cached
        group = (number - 1) // self.inodes_per_group
        index = (number - 1) % self.inodes_per_group
        descriptor = self._group_descriptor(group)
        if not self._inode_is_allocated(number, group, index):
            raise Ext3ReadError("inode is not allocated")
        raw = self._read_at(
            descriptor.inode_table * self.block_size + index * self.inode_size,
            self.inode_size,
        )
        mode = struct.unpack_from("<H", raw, 0)[0]
        if mode == 0:
            raise Ext3ReadError("allocated inode has no valid file mode")
        flags = struct.unpack_from("<I", raw, 32)[0]
        unsupported_flags = (
            _EXT4_EA_INODE_FL
            | _EXT4_ENCRYPT_FL
            | _EXT4_VERITY_FL
            | _EXT4_INLINE_DATA_FL
        )
        if flags & unsupported_flags:
            raise Ext3ReadError("inode uses an unsupported ext4 data layout")
        if flags & _EXT4_EXTENTS_FL:
            if self.profile.feature_incompat & 0x0040 == 0:
                raise Ext3ReadError("inode extent flag lacks EXTENTS feature")
        elif mode & 0xF000 == _S_IFREG and self.profile.feature_incompat & 0x0040:
            raise Ext3ReadError("ext4 EXTENTS profile contains a pointer-layout inode")
        size_low = struct.unpack_from("<I", raw, 4)[0]
        size_high = struct.unpack_from("<I", raw, 108)[0]
        if mode & 0xF000 == _S_IFREG:
            if size_high and not (self.feature_ro_compat & _RO_COMPAT_LARGE_FILE):
                raise Ext3ReadError("large regular-file size lacks LARGE_FILE feature")
            size = size_low | (
                size_high << 32 if self.feature_ro_compat & _RO_COMPAT_LARGE_FILE else 0
            )
        else:
            size = size_low
        pointers = struct.unpack_from("<15I", raw, 40)
        inode = _Inode(
            number=number,
            mode=mode,
            size=size,
            flags=flags,
            mtime=struct.unpack_from("<I", raw, 16)[0],
            block_pointers=pointers,
        )
        if len(self._inode_cache) >= 4096:
            self._inode_cache.clear()
        self._inode_cache[number] = inode
        return inode

    def _group_descriptor(self, group: int) -> _GroupDescriptor:
        """Read high descriptor words for the explicitly gated 64-bit profile."""
        if not self.feature_incompat & 0x0080:
            return super()._group_descriptor(group)
        cached = self._group_cache.get(group)
        if cached is not None and cached.block_bitmap >= (1 << 32):
            return cached
        base = super()._group_descriptor(group)
        raw = self._read_at(
            self._gdt_offset + group * self.group_descriptor_size,
            self.group_descriptor_size,
        )
        block_bitmap = base.block_bitmap | (struct.unpack_from("<I", raw, 32)[0] << 32)
        inode_bitmap = base.inode_bitmap | (struct.unpack_from("<I", raw, 36)[0] << 32)
        inode_table = base.inode_table | (struct.unpack_from("<I", raw, 40)[0] << 32)
        if not (
            base.first_block <= block_bitmap < base.end_block
            and base.first_block <= inode_bitmap < base.end_block
            and base.first_block <= inode_table < base.end_block
            and inode_table + base.inode_table_blocks <= base.end_block
        ):
            raise Ext3ReadError("64-bit group descriptor points outside its group")
        if (
            block_bitmap == inode_bitmap
            or inode_table <= block_bitmap < inode_table + base.inode_table_blocks
        ):
            raise Ext3ReadError("64-bit group descriptor metadata ranges overlap")
        result = _GroupDescriptor(
            block_bitmap,
            inode_bitmap,
            inode_table,
            base.first_block,
            base.end_block,
            base.inode_table_blocks,
        )
        self._group_cache[group] = result
        return result

    def _extent_block(self, block: int) -> bytes:
        if not 0 < block < self.blocks_count:
            raise Ext3ReadError("extent tree block is outside filesystem bounds")
        return self._read_block(block)

    def _extent_map(self, inode: _Inode, logical: int) -> tuple[int, bool]:
        """Return (physical block, unwritten) or (0, False) for a hole."""
        if not inode.flags & _EXT4_EXTENTS_FL:
            return super()._logical_block(inode, logical), False
        if logical < 0 or logical >= 1 << 32:
            raise Ext3ReadError("logical extent index is outside uint32")
        node = struct.pack("<15I", *inode.block_pointers)
        visited: set[int] = set()
        expected_depth = None
        for _depth in range(6):
            if len(node) < 12:
                raise Ext3ReadError("short extent header")
            magic, entries, maximum, node_depth = struct.unpack_from("<HHHH", node, 0)
            if magic != 0xF30A or entries > maximum or maximum > (len(node) - 12) // 12:
                raise Ext3ReadError("invalid extent header bounds")
            if node_depth < 0 or (
                expected_depth is not None and node_depth != expected_depth
            ):
                raise Ext3ReadError("invalid extent tree depth")
            if node_depth == 0:
                previous_end = -1
                for pos in range(entries):
                    off = 12 + pos * 12
                    start, length, start_hi, start_lo = struct.unpack_from(
                        "<IHHI", node, off
                    )
                    unwritten = bool(length & 0x8000)
                    length &= 0x7FFF
                    if length == 0 or start < previous_end:
                        raise Ext3ReadError("empty or overlapping extent")
                    end = start + length
                    if end > 1 << 32:
                        raise Ext3ReadError("extent logical range overflow")
                    physical_start = (start_hi << 32) | start_lo
                    if (
                        physical_start <= 0
                        or physical_start + length > self.blocks_count
                    ):
                        raise Ext3ReadError("extent physical range is out of bounds")
                    previous_end = end
                    if start <= logical < end:
                        physical = physical_start + (logical - start)
                        return physical, unwritten
                return 0, False
            expected_depth = node_depth - 1
            previous_key = -1
            selected = None
            for pos in range(entries):
                off = 12 + pos * 12
                logical_start, child_lo, child_hi = struct.unpack_from(
                    "<IIH", node, off
                )
                if logical_start < previous_key:
                    raise Ext3ReadError("extent index keys are not sorted")
                previous_key = logical_start
                if logical >= logical_start:
                    selected = (logical_start, child_lo | (child_hi << 32))
            if selected is None:
                return 0, False
            if selected[1] in visited:
                raise Ext3ReadError("cyclic extent tree")
            visited.add(selected[1])
            node = self._extent_block(selected[1])
        raise Ext3ReadError("extent tree exceeds conservative depth limit")

    def _logical_block(self, inode: _Inode, logical: int) -> int:
        return self._extent_map(inode, logical)[0]

    def read_file_range(self, path, offset=0, length=1 << 20) -> bytes:
        """Read extents while preserving inherited hole and range semantics."""
        if not isinstance(offset, int) or not isinstance(length, int):
            raise Ext3ReadError("read offset and length must be integers")
        if offset < 0 or length < 0:
            raise Ext3ReadError("negative file read range")
        inode = self._resolve(path)
        if not inode.is_regular:
            raise Ext3ReadError("only regular files can be read as file content")
        if offset >= inode.size or length == 0:
            return b""
        if inode.size > (1 << 32) * self.block_size:
            raise Ext3ReadError("extent file exceeds the logical block address space")
        remaining = min(length, inode.size - offset)
        if remaining > 16 * 1024 * 1024:
            raise Ext3ReadError("requested file range exceeds native read limit")
        position = offset
        output = bytearray()
        while remaining:
            logical = position // self.block_size
            in_block = position % self.block_size
            amount = min(remaining, self.block_size - in_block)
            physical, unwritten = self._extent_map(inode, logical)
            if physical == 0 or unwritten:
                output.extend(b"\x00" * amount)
            else:
                data = self._read_block(physical)
                output.extend(data[in_block : in_block + amount])
            position += amount
            remaining -= amount
        return bytes(output)
