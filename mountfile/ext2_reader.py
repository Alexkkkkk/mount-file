# SPDX-License-Identifier: MIT
"""Bounded, read-only ext2 provider for raw image extents.

Supported profiles are deliberately narrow: clean ext2, 1/2/4 KiB blocks,
revision 0/1, a small explicit feature allowlist, ordinary inode block maps,
and linear directory records. Unknown features, indexed directories, dirty
volumes, journals, malformed metadata, and out-of-extent reads fail closed.
"""

from __future__ import annotations

import math
import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mountfile.ext_probe import (
    ExtProbeError,
    ExtSuperblockProfile,
    probe_ext_superblock,
)


class Ext2ReadError(ValueError):
    """The ext2 profile or requested metadata is unsupported or malformed."""


_COMPAT_HAS_JOURNAL = 0x0004
_COMPAT_EXT_ATTR = 0x0008
_COMPAT_RESIZE_INODE = 0x0010
_COMPAT_DIR_INDEX = 0x0020
_INCOMPAT_FILETYPE = 0x0002
_RO_COMPAT_SPARSE_SUPER = 0x0001
_RO_COMPAT_LARGE_FILE = 0x0002

_S_IFMT = 0xF000
_S_IFREG = 0x8000
_S_IFDIR = 0x4000
_S_IFLNK = 0xA000

_EXT2_INDEX_FL = 0x00001000
_EXT2_COMPR_FL = 0x00000004
_EXT4_ENCRYPT_FL = 0x00000800
_EXT4_EXTENTS_FL = 0x00080000
_EXT4_VERITY_FL = 0x00100000
_EXT4_EA_INODE_FL = 0x00200000
_EXT4_INLINE_DATA_FL = 0x10000000

_MAX_GROUPS = 1_000_000
_MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 1_000_000
_MAX_CACHE_ITEMS = 4096
_MAX_READ_RANGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _GroupDescriptor:
    block_bitmap: int
    inode_bitmap: int
    inode_table: int
    first_block: int
    end_block: int
    inode_table_blocks: int


@dataclass(frozen=True)
class _Inode:
    number: int
    mode: int
    size: int
    flags: int
    mtime: int
    block_pointers: tuple[int, ...]

    @property
    def mode_type(self) -> int:
        return self.mode & _S_IFMT

    @property
    def is_dir(self) -> bool:
        return self.mode_type == _S_IFDIR

    @property
    def is_regular(self) -> bool:
        return self.mode_type == _S_IFREG

    @property
    def is_symlink(self) -> bool:
        return self.mode_type == _S_IFLNK


@dataclass(frozen=True)
class _DirectoryEntry:
    name: str
    inode: int
    file_type: int


class NativeExt2Image:
    """Read-only VFS-compatible provider for a bounded ext2 image extent.

    ``volume_size`` is mandatory so every filesystem read can be proven to stay
    inside the selected partition. The provider never opens the image for write.
    """

    backend_name = "native-ext2"

    def __init__(self, image_path, offset=0, volume_size=None):
        if volume_size is None:
            raise Ext2ReadError("an explicit partition volume_size is required")
        self.image_path = str(image_path)
        self.offset = int(offset)
        self.volume_size = int(volume_size)
        self.root_name = self.image_path
        self._stream = None
        self._lock = threading.RLock()
        self._group_cache: dict[int, _GroupDescriptor] = {}
        self._block_bitmap_cache: dict[int, bytes] = {}
        self._inode_bitmap_cache: dict[int, bytes] = {}
        self._inode_cache: dict[int, _Inode] = {}

        try:
            # The provider owns this read-only handle until close(); it cannot
            # be scoped to a constructor-local `with` block.
            self._stream = Path(self.image_path).open("rb")  # noqa: SIM115
            self._image_size = os.fstat(self._stream.fileno()).st_size
            self.profile = probe_ext_superblock(
                self._stream, offset=self.offset, volume_size=self.volume_size
            )
            self._validate_profile(self.profile)
            self.block_size = self.profile.block_size
            self.blocks_count = self.profile.blocks_count
            self.inodes_count = self.profile.inodes_count
            self.first_data_block = self.profile.first_data_block
            self.blocks_per_group = self.profile.blocks_per_group
            self.inodes_per_group = self.profile.inodes_per_group
            self.inode_size = self.profile.inode_size
            self.group_descriptor_size = self.profile.group_descriptor_size
            self.filesystem_size = self.profile.filesystem_size
            self.feature_compat = self.profile.feature_compat
            self.feature_incompat = self.profile.feature_incompat
            self.feature_ro_compat = self.profile.feature_ro_compat
            self.has_filetype = bool(self.feature_incompat & _INCOMPAT_FILETYPE)
            self.group_count = math.ceil(
                (self.blocks_count - self.first_data_block) / self.blocks_per_group
            )
            inode_groups = math.ceil(self.inodes_count / self.inodes_per_group)
            if not 1 <= self.group_count <= _MAX_GROUPS:
                raise Ext2ReadError("unsupported or invalid ext block-group count")
            if inode_groups > self.group_count:
                raise Ext2ReadError("inode groups exceed ext block groups")
            self._gdt_offset = (self.first_data_block + 1) * self.block_size
            self._gdt_blocks = math.ceil(
                self.group_count * self.group_descriptor_size / self.block_size
            )
            if self._gdt_blocks >= self.blocks_per_group:
                raise Ext2ReadError("group descriptor table exceeds a block group")
            if (
                self._gdt_offset + self.group_count * self.group_descriptor_size
                > self.filesystem_size
            ):
                raise Ext2ReadError("group descriptor table exceeds filesystem bounds")
            self._primary_metadata_start = self.first_data_block
            self._primary_metadata_end = self.first_data_block + 1 + self._gdt_blocks
        except (OSError, ExtProbeError) as exc:
            self.close()
            raise Ext2ReadError(str(exc)) from exc
        except Exception:
            self.close()
            raise

    @staticmethod
    def _validate_profile(profile: ExtSuperblockProfile) -> None:
        if profile.fs_type != "ext2":
            raise Ext2ReadError("native reader supports ext2 profiles only")
        if not profile.clean_unmounted or profile.state != 0x0001:
            raise Ext2ReadError("dirty or recovery-required ext volumes are read-only")
        if profile.has_journal or profile.journal_recovery_needed:
            raise Ext2ReadError(
                "journaled ext profiles are not supported by this reader"
            )
        if profile.revision not in (0, 1):
            raise Ext2ReadError("unsupported ext superblock revision")
        if profile.block_size not in (1024, 2048, 4096):
            raise Ext2ReadError("native reader supports only 1, 2 or 4 KiB blocks")
        if profile.first_data_block != (1 if profile.block_size == 1024 else 0):
            raise Ext2ReadError("invalid first-data-block value for ext block size")
        compat_allowed = _COMPAT_EXT_ATTR | _COMPAT_RESIZE_INODE
        if profile.feature_compat & _COMPAT_HAS_JOURNAL:
            raise Ext2ReadError("JBD2 journal support is not implemented")
        if profile.feature_compat & _COMPAT_DIR_INDEX:
            raise Ext2ReadError("indexed directories are not supported by this profile")
        if profile.feature_compat & ~compat_allowed:
            raise Ext2ReadError(
                "compatible features exceed the native ext2 read allowlist"
            )
        if profile.feature_incompat & ~_INCOMPAT_FILETYPE:
            raise Ext2ReadError(
                "incompatible features exceed the native ext2 read allowlist"
            )
        ro_allowed = _RO_COMPAT_SPARSE_SUPER | _RO_COMPAT_LARGE_FILE
        if profile.feature_ro_compat & ~ro_allowed:
            raise Ext2ReadError(
                "read-only-compatible features exceed the native ext2 read allowlist"
            )
        if profile.feature_compat & _COMPAT_RESIZE_INODE and not (
            profile.feature_ro_compat & _RO_COMPAT_SPARSE_SUPER
        ):
            raise Ext2ReadError("resize-inode feature requires sparse-super support")
        if profile.revision == 0 and (
            profile.feature_compat
            or profile.feature_incompat
            or profile.feature_ro_compat
        ):
            raise Ext2ReadError(
                "revision-0 ext superblock has revision-1 feature flags"
            )
        if profile.blocks_per_group > profile.block_size * 8:
            raise Ext2ReadError("blocks per group exceed block-bitmap capacity")
        if profile.inodes_per_group > profile.block_size * 8:
            raise Ext2ReadError("inodes per group exceed inode-bitmap capacity")

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def reopen(self) -> None:
        if self._stream is None:
            self.__init__(self.image_path, self.offset, self.volume_size)

    def _read_at(self, relative_offset: int, size: int) -> bytes:
        if self._stream is None:
            raise Ext2ReadError("filesystem image is closed")
        if relative_offset < 0 or size < 0:
            raise Ext2ReadError("negative filesystem read range")
        end = relative_offset + size
        if end < relative_offset or end > self.filesystem_size:
            raise Ext2ReadError("filesystem read exceeds selected partition bounds")
        if self.offset + end > self._image_size:
            raise Ext2ReadError("filesystem read exceeds image bounds")
        with self._lock:
            self._stream.seek(self.offset + relative_offset)
            data = self._stream.read(size)
        if len(data) != size:
            raise Ext2ReadError("short read from filesystem image")
        return data

    @staticmethod
    def _has_super_backup(group: int, sparse_super: bool) -> bool:
        if not sparse_super:
            return True
        if group in (0, 1):
            return True
        for base in (3, 5, 7):
            value = group
            while value > 1 and value % base == 0:
                value //= base
            if value == 1:
                return True
        return False

    def _group_descriptor(self, group: int) -> _GroupDescriptor:
        if not 0 <= group < self.group_count:
            raise Ext2ReadError("inode or block references an invalid group")
        cached = self._group_cache.get(group)
        if cached is not None:
            return cached
        raw = self._read_at(
            self._gdt_offset + group * self.group_descriptor_size,
            self.group_descriptor_size,
        )
        block_bitmap, inode_bitmap, inode_table = struct.unpack_from("<III", raw, 0)
        first_block = self.first_data_block + group * self.blocks_per_group
        end_block = min(self.blocks_count, first_block + self.blocks_per_group)
        inode_table_blocks = math.ceil(
            self.inodes_per_group * self.inode_size / self.block_size
        )
        if first_block >= end_block:
            raise Ext2ReadError("empty ext block group")
        if not (first_block <= block_bitmap < end_block):
            raise Ext2ReadError("block bitmap is outside its block group")
        if not (first_block <= inode_bitmap < end_block):
            raise Ext2ReadError("inode bitmap is outside its block group")
        if block_bitmap == inode_bitmap:
            raise Ext2ReadError("block and inode bitmaps overlap")
        if not (first_block <= inode_table < end_block):
            raise Ext2ReadError("inode table is outside its block group")
        if inode_table + inode_table_blocks > end_block:
            raise Ext2ReadError("inode table exceeds its block group")
        if inode_table <= block_bitmap < inode_table + inode_table_blocks:
            raise Ext2ReadError("inode table overlaps the block bitmap")
        if inode_table <= inode_bitmap < inode_table + inode_table_blocks:
            raise Ext2ReadError("inode table overlaps the inode bitmap")
        sparse = bool(self.feature_ro_compat & _RO_COMPAT_SPARSE_SUPER)
        if self._has_super_backup(group, sparse):
            backup_end = min(end_block, first_block + 1 + self._gdt_blocks)
            metadata_ranges = (
                (block_bitmap, block_bitmap + 1),
                (inode_bitmap, inode_bitmap + 1),
                (inode_table, inode_table + inode_table_blocks),
            )
            if any(
                start < backup_end and stop > first_block
                for start, stop in metadata_ranges
            ):
                raise Ext2ReadError(
                    "group metadata overlaps backup superblock metadata"
                )
        result = _GroupDescriptor(
            block_bitmap,
            inode_bitmap,
            inode_table,
            first_block,
            end_block,
            inode_table_blocks,
        )
        if len(self._group_cache) >= _MAX_CACHE_ITEMS:
            self._group_cache.clear()
        self._group_cache[group] = result
        return result

    def _bitmap(self, group: int, inode: bool) -> bytes:
        cache = self._inode_bitmap_cache if inode else self._block_bitmap_cache
        cached = cache.get(group)
        if cached is not None:
            return cached
        descriptor = self._group_descriptor(group)
        block = descriptor.inode_bitmap if inode else descriptor.block_bitmap
        data = self._read_at(block * self.block_size, self.block_size)
        if len(cache) >= _MAX_CACHE_ITEMS:
            cache.clear()
        cache[group] = data
        return data

    @staticmethod
    def _bit_is_set(bitmap: bytes, bit: int) -> bool:
        if bit < 0 or bit // 8 >= len(bitmap):
            return False
        return bool(bitmap[bit // 8] & (1 << (bit % 8)))

    def _validate_data_block(self, block: int) -> None:
        if block <= self.first_data_block or block >= self.blocks_count:
            raise Ext2ReadError(
                "data block pointer is outside allocatable filesystem bounds"
            )
        group = (block - self.first_data_block) // self.blocks_per_group
        descriptor = self._group_descriptor(group)
        if block == descriptor.block_bitmap or block == descriptor.inode_bitmap:
            raise Ext2ReadError("data block pointer references a block bitmap")
        if (
            descriptor.inode_table
            <= block
            < descriptor.inode_table + descriptor.inode_table_blocks
        ):
            raise Ext2ReadError("data block pointer references an inode table")
        if self._primary_metadata_start <= block < self._primary_metadata_end:
            raise Ext2ReadError(
                "data block pointer references primary superblock metadata"
            )
        group_index = block - descriptor.first_block
        if not self._bit_is_set(self._bitmap(group, inode=False), group_index):
            raise Ext2ReadError("data block pointer references a free block")
        sparse = bool(self.feature_ro_compat & _RO_COMPAT_SPARSE_SUPER)
        if self._has_super_backup(group, sparse):
            backup_end = min(
                descriptor.end_block,
                descriptor.first_block + 1 + self._gdt_blocks,
            )
            if descriptor.first_block <= block < backup_end:
                raise Ext2ReadError(
                    "data block pointer references backup superblock metadata"
                )

    def _read_block(self, block: int) -> bytes:
        if block == 0:
            raise Ext2ReadError("block zero is a hole, not a readable metadata block")
        self._validate_data_block(block)
        return self._read_at(block * self.block_size, self.block_size)

    def _inode_is_allocated(self, number: int, group: int, index: int) -> bool:
        return self._bit_is_set(self._bitmap(group, inode=True), index)

    def _read_inode(self, number: int) -> _Inode:
        if not 1 <= number <= self.inodes_count:
            raise Ext2ReadError("directory entry references an invalid inode number")
        cached = self._inode_cache.get(number)
        if cached is not None:
            return cached
        group = (number - 1) // self.inodes_per_group
        index = (number - 1) % self.inodes_per_group
        descriptor = self._group_descriptor(group)
        if not self._inode_is_allocated(number, group, index):
            raise Ext2ReadError("directory entry references a free inode")
        offset = descriptor.inode_table * self.block_size + index * self.inode_size
        raw = self._read_at(offset, self.inode_size)
        mode = struct.unpack_from("<H", raw, 0)[0]
        if mode == 0:
            raise Ext2ReadError("allocated inode has no valid file mode")
        flags = struct.unpack_from("<I", raw, 32)[0]
        size_low = struct.unpack_from("<I", raw, 4)[0]
        size_high = struct.unpack_from("<I", raw, 108)[0]
        if mode & _S_IFMT == _S_IFREG:
            if size_high and not (self.feature_ro_compat & _RO_COMPAT_LARGE_FILE):
                raise Ext2ReadError("large regular-file size lacks LARGE_FILE feature")
            size = size_low | (
                size_high << 32 if self.feature_ro_compat & _RO_COMPAT_LARGE_FILE else 0
            )
        else:
            size = size_low
        unsupported_flags = (
            _EXT2_COMPR_FL
            | _EXT4_ENCRYPT_FL
            | _EXT4_EXTENTS_FL
            | _EXT4_VERITY_FL
            | _EXT4_EA_INODE_FL
            | _EXT4_INLINE_DATA_FL
        )
        if flags & unsupported_flags:
            raise Ext2ReadError(
                "inode uses unsupported compression, encryption or ext4 layout"
            )
        if mode & _S_IFMT == _S_IFDIR and flags & _EXT2_INDEX_FL:
            raise Ext2ReadError("indexed directories are not supported by this profile")
        block_pointers = struct.unpack_from("<15I", raw, 40)
        addressable_blocks = 12 + self.block_size // 4
        addressable_blocks += (self.block_size // 4) ** 2 + (self.block_size // 4) ** 3
        if size > addressable_blocks * self.block_size:
            raise Ext2ReadError("inode size exceeds ext2 block-pointer capacity")
        inode = _Inode(
            number=number,
            mode=mode,
            size=size,
            flags=flags,
            mtime=struct.unpack_from("<I", raw, 16)[0],
            block_pointers=block_pointers,
        )
        if len(self._inode_cache) >= _MAX_CACHE_ITEMS:
            self._inode_cache.clear()
        self._inode_cache[number] = inode
        return inode

    def _indirect_block(self, root: int, level: int, index: int) -> int:
        if root == 0:
            return 0
        entries_per_block = self.block_size // 4
        if index < 0 or index >= entries_per_block**level:
            raise Ext2ReadError("logical block exceeds ext2 indirect pointer capacity")
        pointer = root
        remaining = index
        for depth in range(level, 0, -1):
            divisor = entries_per_block ** (depth - 1)
            slot, remaining = divmod(remaining, divisor)
            block = self._read_block(pointer)
            pointer = struct.unpack_from("<I", block, slot * 4)[0]
            if pointer == 0:
                return 0
        return pointer

    def _logical_block(self, inode: _Inode, logical: int) -> int:
        if logical < 0:
            raise Ext2ReadError("negative logical block index")
        pointers = inode.block_pointers
        if logical < 12:
            return pointers[logical]
        entries = self.block_size // 4
        index = logical - 12
        if index < entries:
            return self._indirect_block(pointers[12], 1, index)
        index -= entries
        if index < entries**2:
            return self._indirect_block(pointers[13], 2, index)
        index -= entries**2
        if index < entries**3:
            return self._indirect_block(pointers[14], 3, index)
        raise Ext2ReadError("logical block exceeds ext2 pointer capacity")

    @staticmethod
    def _path_parts(path: str) -> list[str]:
        if not isinstance(path, str):
            raise Ext2ReadError("filesystem path must be a string")
        if "\x00" in path or "\\" in path:
            raise Ext2ReadError("filesystem path contains an unsafe separator")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise Ext2ReadError("dot components are not supported in filesystem paths")
        return parts

    def _directory_entries(self, inode: _Inode) -> list[_DirectoryEntry]:
        if not inode.is_dir:
            raise Ext2ReadError("requested path is not a directory")
        if inode.size % self.block_size:
            raise Ext2ReadError("directory size is not block aligned")
        if inode.size > _MAX_DIRECTORY_BYTES:
            raise Ext2ReadError("directory exceeds the native reader size limit")
        result = []
        seen = set()
        entries_seen = 0
        for logical in range(inode.size // self.block_size):
            physical = self._logical_block(inode, logical)
            if physical == 0:
                raise Ext2ReadError("directory contains a sparse block")
            block = self._read_block(physical)
            position = 0
            while position < self.block_size:
                if position + 8 > self.block_size:
                    raise Ext2ReadError("truncated directory entry header")
                entry_inode, record_length = struct.unpack_from("<IH", block, position)
                if self.has_filetype:
                    name_length = block[position + 6]
                    file_type = block[position + 7]
                else:
                    name_length = struct.unpack_from("<H", block, position + 6)[0]
                    file_type = 0
                if (
                    record_length < 8
                    or record_length % 4
                    or position + record_length > self.block_size
                ):
                    raise Ext2ReadError("invalid directory record length")
                if name_length > 255 or name_length > record_length - 8:
                    raise Ext2ReadError("invalid directory name length")
                if file_type > 7:
                    raise Ext2ReadError("invalid directory file-type code")
                if entry_inode:
                    if entry_inode > self.inodes_count:
                        raise Ext2ReadError(
                            "directory entry inode exceeds filesystem inode count"
                        )
                    raw_name = block[position + 8 : position + 8 + name_length]
                    if not raw_name or b"\x00" in raw_name or b"/" in raw_name:
                        raise Ext2ReadError("invalid directory entry name")
                    name = raw_name.decode("utf-8", "surrogateescape")
                    if name not in (".", ".."):
                        if name in seen:
                            raise Ext2ReadError(
                                "duplicate name in linear directory listing"
                            )
                        seen.add(name)
                        result.append(_DirectoryEntry(name, entry_inode, file_type))
                        entries_seen += 1
                        if entries_seen > _MAX_DIRECTORY_ENTRIES:
                            raise Ext2ReadError(
                                "directory exceeds the entry-count limit"
                            )
                position += record_length
            if position != self.block_size:
                raise Ext2ReadError("directory records do not fill their data block")
        return result

    def _resolve(self, path: str) -> _Inode:
        inode = self._read_inode(2)
        for component in self._path_parts(path):
            if not inode.is_dir:
                raise Ext2ReadError("path component is not a directory")
            matches = [
                entry
                for entry in self._directory_entries(inode)
                if entry.name == component
            ]
            if not matches:
                raise Ext2ReadError(f"path does not exist: {path}")
            inode = self._read_inode(matches[0].inode)
            if inode.is_symlink:
                raise Ext2ReadError("symbolic-link traversal is not supported")
        return inode

    @staticmethod
    def _format_mtime(timestamp: int) -> str:
        if timestamp <= 0:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def _mode_type_name(mode: int) -> str:
        return {
            _S_IFREG: "file",
            _S_IFDIR: "dir",
            _S_IFLNK: "symlink",
        }.get(mode & _S_IFMT, "special")

    def list_dir(self, path="/"):
        inode = self._resolve(path)
        entries = self._directory_entries(inode)
        result = []
        for entry in entries:
            child = self._read_inode(entry.inode)
            actual_type = self._mode_type_name(child.mode)
            expected_type = {
                1: "file",
                2: "dir",
                3: "special",
                4: "special",
                5: "special",
                6: "special",
                7: "symlink",
            }.get(entry.file_type)
            if expected_type and entry.file_type and expected_type != actual_type:
                raise Ext2ReadError("directory entry type disagrees with inode mode")
            is_dir = child.is_dir
            result.append(
                {
                    "name": entry.name,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else child.size,
                    "date": "" if is_dir else self._format_mtime(child.mtime),
                    "kind": actual_type,
                    "inode": child.number,
                }
            )
        result.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return result

    def file_size(self, path) -> int:
        return self._resolve(path).size

    def read_file_range(self, path, offset=0, length=1 << 20) -> bytes:
        if not isinstance(offset, int) or not isinstance(length, int):
            raise Ext2ReadError("read offset and length must be integers")
        if offset < 0 or length < 0:
            raise Ext2ReadError("negative file read range")
        inode = self._resolve(path)
        if not inode.is_regular:
            raise Ext2ReadError("only regular files can be read as file content")
        if offset >= inode.size or length == 0:
            return b""
        remaining = min(length, inode.size - offset)
        if remaining > _MAX_READ_RANGE_BYTES:
            raise Ext2ReadError(
                "requested file range exceeds the 16 MiB native read limit"
            )
        position = offset
        output = bytearray()
        while remaining:
            logical = position // self.block_size
            in_block = position % self.block_size
            amount = min(remaining, self.block_size - in_block)
            block = self._logical_block(inode, logical)
            if block == 0:
                output.extend(b"\x00" * amount)
            else:
                data = self._read_block(block)
                output.extend(data[in_block : in_block + amount])
            position += amount
            remaining -= amount
        return bytes(output)

    def read_file(self, path) -> bytes:
        return self.read_file_range(path, 0, self.file_size(path))
