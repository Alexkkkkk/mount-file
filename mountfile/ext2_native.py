# SPDX-License-Identifier: MIT
"""Dependency-free writer for a deliberately small, validated ext2 profile.

The bounded profile supports clean, unjournaled ext2 filesystems using block
bitmaps, 32-byte group descriptors, the FILETYPE directory-entry format, and
direct plus single-indirect data pointers for regular files and directories.
Mutations are staged against a copy of the selected filesystem extent and are
committed only after structural read-back validation. This module does not claim
ext3/ext4 support.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from mountfile.safety import RefusalCode, RefusalError, checked_range


class NativeExt2Error(RuntimeError):
    """A request is unsafe or unsupported by the native ext2 profile."""

    def __init__(self, message: str, code: RefusalCode | None = None):
        super().__init__(message)
        self.code = code


class NativeExt2UnsafeError(NativeExt2Error):
    """The on-disk geometry/state is unsafe; external RW fallback is forbidden."""


_EXT_MAGIC = b"\x53\xef"
_FEATURE_COMPAT_EXT_ATTR = 0x0008
_FEATURE_COMPAT_RESIZE_INODE = 0x0010
_FEATURE_INCOMPAT_FILETYPE = 0x0002
_FEATURE_RO_COMPAT_SPARSE_SUPER = 0x0001
_FEATURE_RO_COMPAT_LARGE_FILE = 0x0002
_ALLOWED_COMPAT = _FEATURE_COMPAT_EXT_ATTR | _FEATURE_COMPAT_RESIZE_INODE
_ALLOWED_INCOMPAT = _FEATURE_INCOMPAT_FILETYPE
_ALLOWED_RO_COMPAT = _FEATURE_RO_COMPAT_SPARSE_SUPER | _FEATURE_RO_COMPAT_LARGE_FILE
_INODE_EXTENTS = 0x00080000
_INODE_INDEX = 0x00001000
_INODE_UNSUPPORTED_FLAGS = _INODE_EXTENTS | _INODE_INDEX | 0x00000024 | 0x00000800
_DIRECT_BLOCKS = 12
_CHUNK = 1024 * 1024


@contextmanager
def _image_process_lock(image_path):
    """Hold an advisory exclusive lock on the image for one mutation lifecycle.

    The lock is taken on the image itself, so it creates no sidecar files and
    coordinates native writers in separate MountFile processes. It is advisory:
    tools that do not participate in this locking protocol are still detected
    where possible by the existing source-hash checks.
    """
    stream = open(image_path, "r+b")
    acquired = False
    try:
        if os.name == "nt":
            import errno
            import msvcrt

            while not acquired:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class _Ext2FS:
    """Bounded ext2 parser/mutator over one regular image file."""

    def __init__(self, path: str | os.PathLike[str], offset: int = 0, volume_size=None):
        self.path = Path(path)
        self.offset = int(offset)
        if self.offset < 0:
            raise NativeExt2Error("negative filesystem offset")
        if not self.path.is_file():
            raise NativeExt2Error("native ext2 writes require a regular image file")
        self.image_size = self.path.stat().st_size
        if volume_size is None:
            if self.offset:
                raise NativeExt2UnsafeError(
                    "an explicit selected partition size is required for a nonzero offset"
                )
            volume_size = self.image_size
        self.volume_size = int(volume_size)
        if self.volume_size < 2048 or self.offset + self.volume_size > self.image_size:
            raise NativeExt2UnsafeError(
                "selected ext2 partition extent is outside the image"
            )
        self.sb_offset = self.offset + 1024
        self.sb = bytearray(self._read_at(self.sb_offset, 1024, absolute=True))
        if self.sb[56:58] != _EXT_MAGIC:
            raise NativeExt2Error("no ext superblock at the selected offset")
        log_block = self._u32(self.sb, 24)
        if log_block > 2:
            raise NativeExt2Error("native ext2 supports 1, 2 or 4 KiB blocks only")
        self.block_size = 1024 << log_block
        self.blocks_count = self._u32(self.sb, 4)
        self.inodes_count = self._u32(self.sb, 0)
        self.first_data_block = self._u32(self.sb, 20)
        self.blocks_per_group = self._u32(self.sb, 32)
        self.inodes_per_group = self._u32(self.sb, 40)
        self.revision = self._u32(self.sb, 76)
        self.inode_size = self._u16(self.sb, 88) if self.revision else 128
        self.first_ino = self._u32(self.sb, 84) if self.revision else 11
        self.compat = self._u32(self.sb, 92)
        self.incompat = self._u32(self.sb, 96)
        self.ro_compat = self._u32(self.sb, 100)
        self.state = self._u16(self.sb, 58)
        self.reserved_gdt_blocks = self._u16(self.sb, 206) if self.revision else 0
        self.filesystem_size = self.blocks_count * self.block_size
        if self.blocks_count <= self.first_data_block or self.inodes_count < 2:
            raise NativeExt2UnsafeError("invalid ext2 geometry")
        if self.revision not in (0, 1):
            raise NativeExt2UnsafeError("unsupported ext2 superblock revision")
        if self.first_data_block != (1 if self.block_size == 1024 else 0):
            raise NativeExt2UnsafeError("invalid ext2 first-data-block value")
        if self.blocks_per_group <= 0 or self.inodes_per_group <= 0:
            raise NativeExt2UnsafeError("invalid ext2 group geometry")
        if self.blocks_per_group > self.block_size * 8:
            raise NativeExt2Error("block group exceeds one ext2 bitmap")
        if self.inodes_per_group > self.block_size * 8:
            raise NativeExt2Error("inode group exceeds one ext2 bitmap")
        if (
            self.inode_size < 128
            or self.inode_size > self.block_size
            or self.inode_size % 4
        ):
            raise NativeExt2Error("unsupported ext2 inode size")
        if self.state != 1 or self.incompat & 0x0004:
            raise NativeExt2UnsafeError(
                "ext2 volume is dirty or requires journal recovery; refusing writes"
            )
        if self.compat & ~_ALLOWED_COMPAT:
            raise NativeExt2Error("unsupported ext2 compatible feature flags")
        if self.incompat != _FEATURE_INCOMPAT_FILETYPE:
            raise NativeExt2Error(
                "native ext2 requires FILETYPE and rejects journal/ext4 incompat features"
            )
        if self.ro_compat & ~_ALLOWED_RO_COMPAT:
            raise NativeExt2Error("unsupported ext2 read-only-compatible feature flags")
        self.groups = math.ceil(
            (self.blocks_count - self.first_data_block) / self.blocks_per_group
        )
        inode_groups = math.ceil(self.inodes_count / self.inodes_per_group)
        if not self.groups or inode_groups > self.groups:
            raise NativeExt2UnsafeError("inconsistent ext2 block/inode group counts")
        self.gdt_offset = (self.first_data_block + 1) * self.block_size
        self.gdt_blocks = math.ceil(self.groups * 32 / self.block_size)
        self.inode_table_blocks = math.ceil(
            self.inodes_per_group * self.inode_size / self.block_size
        )
        if self.filesystem_size > self.volume_size:
            raise NativeExt2UnsafeError("ext2 extent exceeds selected partition bounds")
        if self.offset + self.filesystem_size > self.image_size:
            raise NativeExt2UnsafeError("ext2 extent exceeds image bounds")
        self.free_blocks = self._u32(self.sb, 12)
        self.free_inodes = self._u32(self.sb, 16)
        if self.free_blocks > self.blocks_count or self.free_inodes > self.inodes_count:
            raise NativeExt2UnsafeError("invalid ext2 free-space counters")
        self._descriptors = []
        for group in range(self.groups):
            desc = self._read_at(self.gdt_offset + group * 32, 32)
            block_bitmap = self._u32(desc, 0)
            inode_bitmap = self._u32(desc, 4)
            inode_table = self._u32(desc, 8)
            group_start = self._group_start(group)
            group_end = group_start + self._group_block_count(group)
            if not (self.first_data_block <= block_bitmap < self.blocks_count):
                raise NativeExt2UnsafeError(
                    "ext2 block bitmap is outside the filesystem"
                )
            if not (self.first_data_block <= inode_bitmap < self.blocks_count):
                raise NativeExt2UnsafeError(
                    "ext2 inode bitmap is outside the filesystem"
                )
            if not (
                group_start <= block_bitmap < group_end
                and group_start <= inode_bitmap < group_end
                and group_start <= inode_table
                and inode_table + self.inode_table_blocks <= group_end
            ):
                raise NativeExt2UnsafeError(
                    "ext2 group metadata is outside its block group"
                )
            metadata = [block_bitmap, inode_bitmap] + list(
                range(inode_table, inode_table + self.inode_table_blocks)
            )
            if len(set(metadata)) != len(metadata):
                raise NativeExt2UnsafeError("ext2 group metadata blocks overlap")
            self._descriptors.append(bytearray(desc))
        self._metadata = self._metadata_blocks()
        self._validate_initial_counters()
        root = self._read_inode(2)
        if stat.S_IFMT(self._u16(root, 0)) != stat.S_IFDIR:
            raise NativeExt2UnsafeError("ext2 root inode is not an allocated directory")

    @staticmethod
    def _u16(buf: bytes | bytearray, off: int) -> int:
        return struct.unpack_from("<H", buf, off)[0]

    @staticmethod
    def _u32(buf: bytes | bytearray, off: int) -> int:
        return struct.unpack_from("<I", buf, off)[0]

    @staticmethod
    def _put16(buf: bytearray, off: int, value: int) -> None:
        if not 0 <= value <= 0xFFFF:
            raise NativeExt2Error("ext2 16-bit metadata counter overflow")
        struct.pack_into("<H", buf, off, value)

    @staticmethod
    def _put32(buf: bytearray, off: int, value: int) -> None:
        if not 0 <= value <= 0xFFFFFFFF:
            raise NativeExt2Error("ext2 32-bit metadata counter overflow")
        struct.pack_into("<I", buf, off, value)

    def _read_at(self, offset: int, length: int, absolute: bool = False) -> bytes:
        start = offset if absolute else self.offset + offset
        try:
            checked_range(start, length, upper=self.image_size)
        except RefusalError as exc:
            raise NativeExt2Error(
                "ext2 read is outside image bounds", exc.code
            ) from exc
        if not absolute and hasattr(self, "filesystem_size"):
            try:
                checked_range(offset, length, upper=self.filesystem_size)
            except RefusalError as exc:
                raise NativeExt2Error(
                    "ext2 read is outside filesystem bounds", exc.code
                ) from exc
        with self.path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(length)
        if len(data) != length:
            raise NativeExt2Error("short read inside ext2 image")
        return data

    def _write_at(self, offset: int, data: bytes) -> None:
        try:
            checked_range(offset, len(data), upper=self.filesystem_size)
        except RefusalError as exc:
            raise NativeExt2Error(
                "ext2 write is outside filesystem bounds", exc.code
            ) from exc
        with self.path.open("r+b") as stream:
            stream.seek(self.offset + offset)
            written = stream.write(data)
            if written != len(data):
                raise NativeExt2Error("short write inside ext2 image")

    def _block_offset(self, block: int) -> int:
        if not self.first_data_block <= block < self.blocks_count:
            raise NativeExt2Error("ext2 data block is outside filesystem bounds")
        return block * self.block_size

    def _read_block(self, block: int) -> bytes:
        return self._read_at(self._block_offset(block), self.block_size)

    def _write_block(self, block: int, data: bytes) -> None:
        if len(data) != self.block_size:
            raise NativeExt2Error("ext2 block write has wrong size")
        self._write_at(self._block_offset(block), data)

    def _group_start(self, group: int) -> int:
        return self.first_data_block + group * self.blocks_per_group

    def _group_block_count(self, group: int) -> int:
        return min(
            self.blocks_per_group,
            self.blocks_count - self._group_start(group),
        )

    def _has_super_backup(self, group: int) -> bool:
        if group == 0:
            return True
        if not self.ro_compat & _FEATURE_RO_COMPAT_SPARSE_SUPER:
            return True
        if group == 1:
            return True
        for base in (3, 5, 7):
            value = group
            while value > 1 and value % base == 0:
                value //= base
            if value == 1:
                return True
        return False

    def _backup_gdt_offsets(self):
        for group in range(self.groups):
            if group == 0 or not self._has_super_backup(group):
                continue
            yield (self._group_start(group) + 1) * self.block_size

    def _metadata_blocks(self) -> set[int]:
        reserved = set()
        for group, desc in enumerate(self._descriptors):
            start = self._group_start(group)
            if self._has_super_backup(group):
                end = start + 1 + self.gdt_blocks + self.reserved_gdt_blocks
                reserved.update(range(start, min(end, self.blocks_count)))
            reserved.add(self._u32(desc, 0))
            reserved.add(self._u32(desc, 4))
            table = self._u32(desc, 8)
            reserved.update(range(table, table + self.inode_table_blocks))
        return reserved

    def _validate_initial_counters(self) -> None:
        total_free_blocks = 0
        total_free_inodes = 0
        for group, desc in enumerate(self._descriptors):
            start = self._group_start(group)
            valid_blocks = self._group_block_count(group)
            group_end = start + valid_blocks
            bitmap_block = self._u32(desc, 0)
            bitmap = self._read_at(bitmap_block * self.block_size, self.block_size)
            free_blocks = valid_blocks - sum(
                1 for bit in range(valid_blocks) if self._bit_is_set(bitmap, bit)
            )
            if self._u16(desc, 12) != free_blocks:
                raise NativeExt2UnsafeError(
                    "ext2 group free-block count disagrees with bitmap"
                )
            own_metadata = [self._u32(desc, 0), self._u32(desc, 4)] + list(
                range(
                    self._u32(desc, 8),
                    self._u32(desc, 8) + self.inode_table_blocks,
                )
            )
            backup_reserved = set()
            if self._has_super_backup(group):
                backup_reserved.update(
                    range(
                        start,
                        min(
                            group_end,
                            start + 1 + self.gdt_blocks + self.reserved_gdt_blocks,
                        ),
                    )
                )
            if set(own_metadata) & backup_reserved:
                raise NativeExt2UnsafeError(
                    "ext2 group metadata overlaps backup superblock/GDT"
                )
            for block in own_metadata:
                bit = block - start
                if bit < 0 or bit >= valid_blocks or not self._bit_is_set(bitmap, bit):
                    raise NativeExt2UnsafeError(
                        "ext2 metadata block is not allocated in its bitmap"
                    )
            inode_bitmap_block = self._u32(desc, 4)
            inode_bitmap = self._read_at(
                inode_bitmap_block * self.block_size, self.block_size
            )
            valid_inodes = min(
                self.inodes_per_group,
                self.inodes_count - group * self.inodes_per_group,
            )
            free_inodes = valid_inodes - sum(
                1
                for bit in range(max(0, valid_inodes))
                if self._bit_is_set(inode_bitmap, bit)
            )
            if self._u16(desc, 14) != free_inodes:
                raise NativeExt2UnsafeError(
                    "ext2 group free-inode count disagrees with bitmap"
                )
            total_free_blocks += free_blocks
            total_free_inodes += free_inodes
        if (
            total_free_blocks != self.free_blocks
            or total_free_inodes != self.free_inodes
        ):
            raise NativeExt2UnsafeError(
                "ext2 superblock free counters disagree with group descriptors"
            )

    def _group_desc(self, group: int) -> bytearray:
        return bytearray(self._descriptors[group])

    def _write_group_desc(self, group: int, desc: bytearray) -> None:
        self._write_at(self.gdt_offset + group * 32, desc)
        for backup in self._backup_gdt_offsets():
            self._write_at(backup + group * 32, desc)
        self._descriptors[group] = bytearray(desc)

    def _write_super_counts(self) -> None:
        now = int(time.time()) & 0xFFFFFFFF
        for off in (12, 16, 48):
            value = {
                12: self.free_blocks,
                16: self.free_inodes,
                48: now,
            }[off]
            struct.pack_into("<I", self.sb, off, value)
        self._write_at(1024, self.sb)
        for group in range(1, self.groups):
            if not self._has_super_backup(group):
                continue
            backup_off = self._group_start(group) * self.block_size
            backup = bytearray(self._read_at(backup_off, 1024))
            for off in (12, 16, 48):
                struct.pack_into("<I", backup, off, self._u32(self.sb, off))
            self._write_at(backup_off, backup)

    def _bitmap_location(self, group: int, inode: bool) -> tuple[int, int]:
        desc = self._descriptors[group]
        block = self._u32(desc, 4 if inode else 0)
        return block * self.block_size, (
            self.inodes_per_group if inode else self.blocks_per_group
        )

    @staticmethod
    def _bit_is_set(bitmap: bytes | bytearray, bit: int) -> bool:
        return bool(bitmap[bit >> 3] & (1 << (bit & 7)))

    @staticmethod
    def _set_bit(bitmap: bytearray, bit: int, value: bool) -> None:
        mask = 1 << (bit & 7)
        if value:
            bitmap[bit >> 3] |= mask
        else:
            bitmap[bit >> 3] &= ~mask

    def _allocate_block(self, preferred_group: int = 0) -> int:
        for step in range(self.groups):
            group = (preferred_group + step) % self.groups
            start = self._group_start(group)
            count = self._group_block_count(group)
            if count <= 0:
                continue
            bitmap_off, _ = self._bitmap_location(group, inode=False)
            bitmap = bytearray(self._read_at(bitmap_off, self.block_size))
            for bit in range(count):
                block = start + bit
                if block < self.first_data_block or block in self._metadata:
                    continue
                if not self._bit_is_set(bitmap, bit):
                    self._set_bit(bitmap, bit, True)
                    self._write_at(bitmap_off, bitmap)
                    desc = self._group_desc(group)
                    free_group = self._u16(desc, 12)
                    if free_group == 0 or self.free_blocks == 0:
                        raise NativeExt2Error(
                            "ext2 free-block counters disagree with bitmap"
                        )
                    self._put16(desc, 12, free_group - 1)
                    self._write_group_desc(group, desc)
                    self.free_blocks -= 1
                    self._write_super_counts()
                    return block
        raise NativeExt2Error("ext2 filesystem has no allocatable data blocks")

    def _free_block(self, block: int) -> None:
        if block in self._metadata:
            raise NativeExt2Error("refusing to release an ext2 metadata block")
        if not self.first_data_block <= block < self.blocks_count:
            raise NativeExt2Error("invalid ext2 block to release")
        group = (block - self.first_data_block) // self.blocks_per_group
        bit = block - self._group_start(group)
        bitmap_off, _ = self._bitmap_location(group, inode=False)
        bitmap = bytearray(self._read_at(bitmap_off, self.block_size))
        if not self._bit_is_set(bitmap, bit):
            raise NativeExt2Error("ext2 block is already free")
        self._set_bit(bitmap, bit, False)
        self._write_at(bitmap_off, bitmap)
        desc = self._group_desc(group)
        self._put16(desc, 12, self._u16(desc, 12) + 1)
        self._write_group_desc(group, desc)
        self.free_blocks += 1
        self._write_super_counts()

    def _allocate_inode(self, preferred_group: int = 0, is_dir: bool = False) -> int:
        for step in range(self.groups):
            group = (preferred_group + step) % self.groups
            first = group * self.inodes_per_group + 1
            count = min(self.inodes_per_group, self.inodes_count - first + 1)
            if count <= 0:
                continue
            bitmap_off, _ = self._bitmap_location(group, inode=True)
            bitmap = bytearray(self._read_at(bitmap_off, self.block_size))
            for bit in range(count):
                inode = first + bit
                if inode < self.first_ino or self._bit_is_set(bitmap, bit):
                    continue
                self._set_bit(bitmap, bit, True)
                self._write_at(bitmap_off, bitmap)
                desc = self._group_desc(group)
                free_group = self._u16(desc, 14)
                if free_group == 0 or self.free_inodes == 0:
                    raise NativeExt2Error(
                        "ext2 free-inode counters disagree with bitmap"
                    )
                self._put16(desc, 14, free_group - 1)
                if is_dir:
                    self._put16(desc, 16, self._u16(desc, 16) + 1)
                self._write_group_desc(group, desc)
                self.free_inodes -= 1
                self._write_super_counts()
                return inode
        raise NativeExt2Error("ext2 filesystem has no allocatable inodes")

    def _free_inode(self, inode: int, is_dir: bool = False) -> None:
        if inode < self.first_ino or inode > self.inodes_count:
            raise NativeExt2Error("refusing to release a reserved ext2 inode")
        group = (inode - 1) // self.inodes_per_group
        bit = (inode - 1) % self.inodes_per_group
        bitmap_off, _ = self._bitmap_location(group, inode=True)
        bitmap = bytearray(self._read_at(bitmap_off, self.block_size))
        if not self._bit_is_set(bitmap, bit):
            raise NativeExt2Error("ext2 inode is already free")
        self._write_inode(inode, bytes(self.inode_size))
        self._set_bit(bitmap, bit, False)
        self._write_at(bitmap_off, bitmap)
        desc = self._group_desc(group)
        self._put16(desc, 14, self._u16(desc, 14) + 1)
        if is_dir:
            used_dirs = self._u16(desc, 16)
            if used_dirs == 0:
                raise NativeExt2Error("ext2 used-directory counter underflow")
            self._put16(desc, 16, used_dirs - 1)
        self._write_group_desc(group, desc)
        self.free_inodes += 1
        self._write_super_counts()

    def _read_inode(self, inode: int) -> bytearray:
        if inode < 1 or inode > self.inodes_count:
            raise NativeExt2Error("inode number is outside the filesystem")
        if not self._inode_is_allocated(inode):
            raise NativeExt2UnsafeError("ext2 inode is marked free in the bitmap")
        group = (inode - 1) // self.inodes_per_group
        index = (inode - 1) % self.inodes_per_group
        table = self._u32(self._descriptors[group], 8)
        off = table * self.block_size + index * self.inode_size
        return bytearray(self._read_at(off, self.inode_size))

    def _write_inode(self, inode: int, raw: bytes | bytearray) -> None:
        if len(raw) != self.inode_size:
            raise NativeExt2Error("ext2 inode write has wrong size")
        if not self._inode_is_allocated(inode):
            raise NativeExt2UnsafeError("refusing to write an ext2 inode marked free")
        group = (inode - 1) // self.inodes_per_group
        index = (inode - 1) % self.inodes_per_group
        table = self._u32(self._descriptors[group], 8)
        off = table * self.block_size + index * self.inode_size
        self._write_at(off, raw)

    def _inode_is_allocated(self, inode: int) -> bool:
        if inode < 1 or inode > self.inodes_count:
            return False
        group = (inode - 1) // self.inodes_per_group
        bit = (inode - 1) % self.inodes_per_group
        bitmap_off, _ = self._bitmap_location(group, inode=True)
        bitmap = self._read_at(bitmap_off, self.block_size)
        return self._bit_is_set(bitmap, bit)

    def _max_file_blocks(self) -> int:
        return _DIRECT_BLOCKS + self.block_size // 4

    def _validate_allocated_block(self, block: int, bitmap_cache: dict) -> None:
        if block in self._metadata:
            raise NativeExt2UnsafeError("inode pointer overlaps ext2 metadata")
        self._block_offset(block)
        group = (block - self.first_data_block) // self.blocks_per_group
        bit = block - self._group_start(group)
        bitmap = bitmap_cache.get(group)
        if bitmap is None:
            bitmap_off, _ = self._bitmap_location(group, inode=False)
            bitmap = self._read_at(bitmap_off, self.block_size)
            bitmap_cache[group] = bitmap
        if not self._bit_is_set(bitmap, bit):
            raise NativeExt2UnsafeError(
                "ext2 inode points at a block marked free in the bitmap"
            )

    def _inode_blocks(
        self, inode_raw: bytes | bytearray, *, allow_indirect=False
    ) -> list[int]:
        values = struct.unpack_from("<15I", inode_raw, 40)
        if not allow_indirect and any(values[_DIRECT_BLOCKS:]):
            raise NativeExt2Error(
                "indirect blocks are unsupported for this ext2 object"
            )
        if allow_indirect and any(values[_DIRECT_BLOCKS + 1 :]):
            raise NativeExt2Error("double/triple-indirect ext2 blocks are unsupported")
        size = self._inode_size(inode_raw)
        count = math.ceil(size / self.block_size) if size else 0
        if count > (self._max_file_blocks() if allow_indirect else _DIRECT_BLOCKS):
            raise NativeExt2Error(
                "native ext2 profile exceeds direct plus single-indirect capacity"
            )
        direct_count = min(count, _DIRECT_BLOCKS)
        blocks = list(values[:direct_count])
        if any(values[direct_count:_DIRECT_BLOCKS]):
            raise NativeExt2UnsafeError(
                "ext2 inode has allocated direct pointers beyond its file size"
            )
        indirect_block = values[_DIRECT_BLOCKS] if allow_indirect else 0
        indirect_count = count - direct_count
        bitmap_cache = {}
        if indirect_count:
            if not indirect_block:
                raise NativeExt2Error("ext2 file has a missing indirect block")
            self._validate_allocated_block(indirect_block, bitmap_cache)
            indirect_pointers = struct.unpack(
                "<%dI" % (self.block_size // 4), self._read_block(indirect_block)
            )
            if any(indirect_pointers[indirect_count:]):
                raise NativeExt2UnsafeError(
                    "ext2 indirect block contains pointers beyond the file size"
                )
            blocks.extend(indirect_pointers[:indirect_count])
        elif indirect_block:
            raise NativeExt2UnsafeError(
                "ext2 inode has an indirect pointer beyond its file size"
            )
        if any(block == 0 for block in blocks):
            raise NativeExt2Error("sparse or corrupt ext2 files are not writable")
        if len(set(blocks)) != len(blocks):
            raise NativeExt2Error("duplicate ext2 data block pointers")
        for block in blocks:
            self._validate_allocated_block(block, bitmap_cache)
        if indirect_block and indirect_block in blocks:
            raise NativeExt2UnsafeError("indirect pointer block aliases file data")
        allocated_blocks = (
            len(blocks)
            + int(bool(indirect_block))
            + int(bool(self._u32(inode_raw, 104)))
        )
        expected_sectors = allocated_blocks * (self.block_size // 512)
        if self._u32(inode_raw, 28) != expected_sectors:
            raise NativeExt2UnsafeError(
                "ext2 inode i_blocks disagrees with its data and indirect blocks"
            )
        return blocks

    def _inode_data(self, raw: bytes | bytearray) -> bytes:
        size = self._inode_size(raw)
        if size > self._max_file_blocks() * self.block_size:
            raise NativeExt2Error(
                "native ext2 phase 1 exceeds single-indirect capacity"
            )
        blocks = self._inode_blocks(raw, allow_indirect=True)
        data = b"".join(self._read_block(block) for block in blocks)
        return data[:size]

    def _inode_size(self, raw: bytes | bytearray) -> int:
        size = self._u32(raw, 4)
        if stat.S_IFMT(self._u16(raw, 0)) == stat.S_IFREG:
            high = self._u32(raw, 108)
            if self.ro_compat & _FEATURE_RO_COMPAT_LARGE_FILE:
                size |= high << 32
            elif high:
                raise NativeExt2UnsafeError(
                    "regular inode has high size bits without LARGE_FILE feature"
                )
        return size

    def _split(self, path: str, allow_root=False) -> list[tuple[str, bytes]]:
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise NativeExt2Error("invalid ext2 path")
        parts = [part for part in path.split("/") if part not in ("", ".")]
        if not parts and allow_root:
            return []
        result = []
        for part in parts:
            if part == ".." or "/" in part:
                raise NativeExt2Error("parent traversal is not allowed in ext2 paths")
            try:
                name = part.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise NativeExt2Error("ext2 path is not valid UTF-8") from exc
            if not name or len(name) > 255 or b"\x00" in name or b"/" in name:
                raise NativeExt2Error("ext2 filename must be 1..255 UTF-8 bytes")
            result.append((part, name))
        return result

    def _dir_entries(self, inode_num: int):
        raw = self._read_inode(inode_num)
        mode = self._u16(raw, 0)
        if stat.S_IFMT(mode) != stat.S_IFDIR:
            raise NotADirectoryError("ext2 path component is not a directory")
        flags = self._u32(raw, 32)
        if flags:
            raise NativeExt2Error("indexed or flagged ext2 directories are unsupported")
        if self._u32(raw, 104):
            # Keep support simple: directory ACL blocks are preserved, but
            # structural edits to such a directory are not enabled in phase 1.
            raise NativeExt2Error(
                "directories with external extended attributes are unsupported"
            )
        size = self._u32(raw, 4)
        if size % self.block_size:
            raise NativeExt2Error("ext2 directory size is not block-aligned")
        blocks = self._inode_blocks(raw, allow_indirect=True)
        if len(blocks) * self.block_size != size:
            raise NativeExt2Error("ext2 directory size/pointer count mismatch")
        found = []
        for block in blocks:
            buf = self._read_block(block)
            cursor = 0
            previous = None
            while cursor < self.block_size:
                if cursor + 8 > self.block_size:
                    raise NativeExt2Error("truncated ext2 directory entry")
                ino = self._u32(buf, cursor)
                rec_len = self._u16(buf, cursor + 4)
                name_len = buf[cursor + 6]
                file_type = buf[cursor + 7]
                if rec_len < 8 or rec_len % 4 or cursor + rec_len > self.block_size:
                    raise NativeExt2Error("invalid ext2 directory record length")
                if name_len > rec_len - 8:
                    raise NativeExt2Error("invalid ext2 directory filename length")
                name = bytes(buf[cursor + 8 : cursor + 8 + name_len])
                if ino:
                    if ino > self.inodes_count:
                        raise NativeExt2Error("directory references an invalid inode")
                    if b"\x00" in name or b"/" in name:
                        raise NativeExt2Error("invalid ext2 directory name bytes")
                    found.append(
                        {
                            "inode": ino,
                            "name_bytes": name,
                            "name": name.decode("utf-8", "replace"),
                            "file_type": file_type,
                            "block": block,
                            "offset": cursor,
                            "rec_len": rec_len,
                            "previous": previous,
                        }
                    )
                    previous = cursor
                cursor += rec_len
        return raw, blocks, found

    def _lookup_child(self, parent: int, name: bytes):
        _, _, entries = self._dir_entries(parent)
        matches = [entry for entry in entries if entry["name_bytes"] == name]
        if len(matches) > 1:
            raise NativeExt2Error("duplicate names in ext2 directory")
        return matches[0] if matches else None

    def _resolve(
        self, path: str, allow_root=False
    ) -> tuple[int, bytearray | None, dict | None]:
        parts = self._split(path, allow_root=allow_root)
        if not parts:
            return 2, self._read_inode(2), None
        parent = 2
        for _, name in parts[:-1]:
            entry = self._lookup_child(parent, name)
            if entry is None:
                raise FileNotFoundError(path)
            child = self._read_inode(entry["inode"])
            if stat.S_IFMT(self._u16(child, 0)) != stat.S_IFDIR:
                raise NotADirectoryError(entry["name"])
            parent = entry["inode"]
        entry = self._lookup_child(parent, parts[-1][1])
        if entry is None:
            if allow_root:
                raise FileNotFoundError(path)
            return parent, None, None
        return parent, self._read_inode(entry["inode"]), entry

    def list_dir(self, path="/") -> list[dict]:
        _, inode_raw, entry = self._resolve(path, allow_root=True)
        if inode_raw is None or stat.S_IFMT(self._u16(inode_raw, 0)) != stat.S_IFDIR:
            raise NotADirectoryError(path)
        inode_num = 2 if entry is None else entry["inode"]
        _, _, entries = self._dir_entries(inode_num)
        result = []
        for entry in entries:
            if entry["name_bytes"] in (b".", b".."):
                continue
            child = self._read_inode(entry["inode"])
            mode = self._u16(child, 0)
            result.append(
                {
                    "name": entry["name"],
                    "is_dir": stat.S_IFMT(mode) == stat.S_IFDIR,
                    "size": self._inode_size(child),
                    "date": "",
                    "inode": entry["inode"],
                }
            )
        result.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
        return result

    def _parent_and_name(self, path: str) -> tuple[int, bytes, str]:
        parts = self._split(path)
        if not parts:
            raise NativeExt2Error("filesystem root cannot be changed")
        parent_path = "/" + "/".join(part for part, _ in parts[:-1])
        if parts[:-1]:
            _, parent_raw, parent_entry = self._resolve(parent_path)
            if parent_entry is None or parent_raw is None:
                raise FileNotFoundError(parent_path)
            parent = parent_entry["inode"]
        else:
            parent = 2
            parent_raw = self._read_inode(2)
        if stat.S_IFMT(self._u16(parent_raw, 0)) != stat.S_IFDIR:
            raise NotADirectoryError(parent_path)
        return parent, parts[-1][1], parts[-1][0]

    @staticmethod
    def _minimum_dirent(name: bytes) -> int:
        return (8 + len(name) + 3) & ~3

    def _make_dirent(
        self, inode: int, rec_len: int, name: bytes, file_type: int
    ) -> bytes:
        if rec_len < self._minimum_dirent(name) or rec_len % 4:
            raise NativeExt2Error("invalid ext2 directory entry size")
        out = bytearray(rec_len)
        struct.pack_into("<IHB", out, 0, inode, rec_len, len(name))
        out[7] = file_type
        out[8 : 8 + len(name)] = name
        return bytes(out)

    def _validate_inserted_entry(
        self, parent: int, name: bytes, inode: int, file_type: int
    ) -> None:
        """Reparse a changed directory and confirm the exact inserted entry.

        In particular, this revalidates the complete direct/single-indirect
        directory map from the on-disk bytes before a staged extent can commit.
        """
        entry = self._lookup_child(parent, name)
        if entry is None or entry["inode"] != inode or entry["file_type"] != file_type:
            raise NativeExt2Error("staged ext2 directory entry failed read-back")

    def _insert_entry(
        self, parent: int, name: bytes, inode: int, file_type: int
    ) -> None:
        parent_raw, blocks, entries = self._dir_entries(parent)
        if any(entry["name_bytes"] == name for entry in entries):
            raise FileExistsError(name.decode("utf-8", "replace"))
        need = self._minimum_dirent(name)
        for block in blocks:
            buf = bytearray(self._read_block(block))
            cursor = 0
            while cursor < self.block_size:
                ino = self._u32(buf, cursor)
                rec_len = self._u16(buf, cursor + 4)
                name_len = buf[cursor + 6]
                if rec_len < 8 or rec_len % 4 or cursor + rec_len > self.block_size:
                    raise NativeExt2Error(
                        "invalid ext2 directory record while inserting"
                    )
                if ino == 0 and rec_len >= need:
                    buf[cursor : cursor + rec_len] = self._make_dirent(
                        inode, rec_len, name, file_type
                    )
                    self._write_block(block, bytes(buf))
                    self._touch_inode(parent_raw)
                    self._write_inode(parent, parent_raw)
                    self._validate_inserted_entry(parent, name, inode, file_type)
                    return
                minimum = self._minimum_dirent(
                    bytes(buf[cursor + 8 : cursor + 8 + name_len])
                )
                slack = rec_len - minimum
                if ino and slack >= need:
                    struct.pack_into("<H", buf, cursor + 4, minimum)
                    new_off = cursor + minimum
                    buf[new_off : new_off + slack] = self._make_dirent(
                        inode, slack, name, file_type
                    )
                    self._write_block(block, bytes(buf))
                    self._touch_inode(parent_raw)
                    self._write_inode(parent, parent_raw)
                    self._validate_inserted_entry(parent, name, inode, file_type)
                    return
                cursor += rec_len
        current_blocks = len(blocks)
        if current_blocks >= self._max_file_blocks():
            raise NativeExt2Error(
                "native ext2 directory exceeds single-indirect capacity"
            )
        preferred = (parent - 1) // self.inodes_per_group
        new_block = self._allocate_block(preferred)
        buf = self._make_dirent(inode, self.block_size, name, file_type)
        self._write_block(new_block, buf)

        allocated_blocks = 1
        if current_blocks < _DIRECT_BLOCKS:
            struct.pack_into("<I", parent_raw, 40 + current_blocks * 4, new_block)
        else:
            indirect_block = self._u32(parent_raw, 40 + _DIRECT_BLOCKS * 4)
            pointer_index = current_blocks - _DIRECT_BLOCKS
            if indirect_block:
                pointers = bytearray(self._read_block(indirect_block))
                if self._u32(pointers, pointer_index * 4):
                    raise NativeExt2UnsafeError(
                        "ext2 directory indirect block has an unexpected pointer"
                    )
            else:
                if current_blocks != _DIRECT_BLOCKS:
                    raise NativeExt2UnsafeError(
                        "ext2 directory has no indirect pointer block"
                    )
                indirect_block = self._allocate_block(preferred)
                pointers = bytearray(self.block_size)
                struct.pack_into(
                    "<I", parent_raw, 40 + _DIRECT_BLOCKS * 4, indirect_block
                )
                allocated_blocks += 1
            struct.pack_into("<I", pointers, pointer_index * 4, new_block)
            self._write_block(indirect_block, bytes(pointers))
        self._put32(parent_raw, 4, self._u32(parent_raw, 4) + self.block_size)
        self._put32(
            parent_raw,
            28,
            self._u32(parent_raw, 28) + allocated_blocks * (self.block_size // 512),
        )
        self._touch_inode(parent_raw)
        self._write_inode(parent, parent_raw)
        self._validate_inserted_entry(parent, name, inode, file_type)

    def _remove_entry(self, parent: int, entry: dict) -> None:
        buf = bytearray(self._read_block(entry["block"]))
        off = entry["offset"]
        if entry["previous"] is None:
            struct.pack_into("<I", buf, off, 0)
            buf[off + 6] = 0
            buf[off + 7] = 0
        else:
            prev = entry["previous"]
            prev_len = self._u16(buf, prev + 4)
            struct.pack_into("<H", buf, prev + 4, prev_len + entry["rec_len"])
        self._write_block(entry["block"], bytes(buf))
        parent_raw = self._read_inode(parent)
        self._touch_inode(parent_raw)
        self._write_inode(parent, parent_raw)

    @staticmethod
    def _touch_inode(raw: bytearray) -> None:
        now = int(time.time()) & 0xFFFFFFFF
        struct.pack_into("<I", raw, 8, now)  # atime
        struct.pack_into("<I", raw, 12, now)  # ctime
        struct.pack_into("<I", raw, 16, now)  # mtime

    def _check_mutable_inode(self, raw: bytearray, path: str) -> int:
        mode = self._u16(raw, 0)
        flags = self._u32(raw, 32)
        if flags & _INODE_UNSUPPORTED_FLAGS or flags:
            raise NativeExt2Error(
                "flagged/compressed/indexed ext2 inodes are unsupported"
            )
        if self._u32(raw, 104):
            raise NativeExt2Error(
                "external extended attributes are not modified in phase 1"
            )
        if stat.S_IFMT(mode) not in (stat.S_IFREG, stat.S_IFDIR):
            raise NativeExt2Error(
                "only regular files and directories are mutable in phase 1"
            )
        return mode

    def read_file(self, path: str) -> bytes:
        _, inode_raw, entry = self._resolve(path)
        if entry is None or inode_raw is None:
            raise FileNotFoundError(path)
        mode = self._u16(inode_raw, 0)
        if stat.S_IFMT(mode) != stat.S_IFREG:
            raise IsADirectoryError(path)
        return self._inode_data(inode_raw)

    def write_file(self, path: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            data = bytes(data)
        if len(data) > self._max_file_blocks() * self.block_size:
            raise NativeExt2Error(
                "native ext2 phase 1 exceeds single-indirect capacity"
            )
        parent, name, _ = self._parent_and_name(path)
        existing = self._lookup_child(parent, name)
        if existing is not None:
            inode_num = existing["inode"]
            inode_raw = self._read_inode(inode_num)
            mode = self._check_mutable_inode(inode_raw, path)
            if stat.S_IFMT(mode) != stat.S_IFREG:
                raise IsADirectoryError(path)
            if self._u16(inode_raw, 26) != 1:
                raise NativeExt2Error(
                    "hard-linked files are not overwritten in phase 1"
                )
            old_blocks = self._inode_blocks(inode_raw, allow_indirect=True)
            old_indirect = self._u32(inode_raw, 40 + _DIRECT_BLOCKS * 4)
            preferred = (inode_num - 1) // self.inodes_per_group
        else:
            inode_num = self._allocate_inode((parent - 1) // self.inodes_per_group)
            inode_raw = bytearray(self.inode_size)
            struct.pack_into("<H", inode_raw, 0, stat.S_IFREG | 0o644)
            struct.pack_into("<H", inode_raw, 26, 1)
            old_blocks = []
            old_indirect = 0
            preferred = (parent - 1) // self.inodes_per_group
        needed = math.ceil(len(data) / self.block_size) if data else 0
        new_blocks = [self._allocate_block(preferred) for _ in range(needed)]
        for index, block in enumerate(new_blocks):
            chunk = data[index * self.block_size : (index + 1) * self.block_size]
            self._write_block(block, chunk.ljust(self.block_size, b"\x00"))
        new_indirect = 0
        if needed > _DIRECT_BLOCKS:
            new_indirect = self._allocate_block(preferred)
            pointer_table = bytearray(self.block_size)
            for index, block in enumerate(new_blocks[_DIRECT_BLOCKS:]):
                struct.pack_into("<I", pointer_table, index * 4, block)
            self._write_block(new_indirect, bytes(pointer_table))
        inode_raw[40:100] = b"\x00" * 60
        for index, block in enumerate(new_blocks[:_DIRECT_BLOCKS]):
            struct.pack_into("<I", inode_raw, 40 + index * 4, block)
        if new_indirect:
            struct.pack_into("<I", inode_raw, 40 + _DIRECT_BLOCKS * 4, new_indirect)
        self._put32(inode_raw, 4, len(data))
        allocated_blocks = len(new_blocks) + int(bool(new_indirect))
        self._put32(inode_raw, 28, allocated_blocks * self.block_size // 512)
        struct.pack_into("<I", inode_raw, 20, 0)  # dtime
        self._touch_inode(inode_raw)
        self._write_inode(inode_num, inode_raw)
        if self._inode_data(self._read_inode(inode_num)) != data:
            raise NativeExt2Error("staged ext2 file failed read-back verification")
        if existing is None:
            self._insert_entry(parent, name, inode_num, 1)
        for block in old_blocks:
            self._free_block(block)
        if old_indirect:
            self._free_block(old_indirect)

    def mkdir(self, path: str) -> None:
        parent, name, _ = self._parent_and_name(path)
        if self._lookup_child(parent, name) is not None:
            raise FileExistsError(path)
        parent_raw = self._read_inode(parent)
        links = self._u16(parent_raw, 26)
        if links == 0xFFFF:
            raise NativeExt2Error("ext2 parent link counter overflow")
        inode_num = self._allocate_inode(
            (parent - 1) // self.inodes_per_group, is_dir=True
        )
        block = self._allocate_block((parent - 1) // self.inodes_per_group)
        child = bytearray(self.inode_size)
        struct.pack_into("<H", child, 0, stat.S_IFDIR | 0o755)
        struct.pack_into("<I", child, 4, self.block_size)
        struct.pack_into("<H", child, 26, 2)
        struct.pack_into("<I", child, 28, self.block_size // 512)
        struct.pack_into("<I", child, 40, block)
        self._touch_inode(child)
        dot = self._make_dirent(inode_num, 12, b".", 2)
        dotdot = self._make_dirent(parent, self.block_size - 12, b"..", 2)
        self._write_block(block, dot + dotdot)
        self._write_inode(inode_num, child)
        self._insert_entry(parent, name, inode_num, 2)
        parent_raw = self._read_inode(parent)
        if self._u16(parent_raw, 26) != links:
            raise NativeExt2Error("ext2 parent link count changed during mkdir")
        struct.pack_into("<H", parent_raw, 26, links + 1)
        self._touch_inode(parent_raw)
        self._write_inode(parent, parent_raw)

    def delete(self, path: str, recursive: bool = False) -> None:
        parent, name, _ = self._parent_and_name(path)
        entry = self._lookup_child(parent, name)
        if entry is None:
            raise FileNotFoundError(path)
        inode_num = entry["inode"]
        raw = self._read_inode(inode_num)
        mode = self._check_mutable_inode(raw, path)
        kind = stat.S_IFMT(mode)
        if self._u32(raw, 104):
            raise NativeExt2Error(
                "external extended attributes are not deleted in phase 1"
            )
        is_dir = kind == stat.S_IFDIR
        if is_dir:
            _, _, children = self._dir_entries(inode_num)
            if any(item["name_bytes"] not in (b".", b"..") for item in children):
                raise NativeExt2Error(
                    "native ext2 phase 1 deletes only empty directories; "
                    "recursive delete is unsupported"
                )
            if self._u16(raw, 26) != 2:
                raise NativeExt2Error("unexpected ext2 directory link count")
        else:
            if self._u16(raw, 26) != 1:
                raise NativeExt2Error("hard-linked files are not deleted in phase 1")
        blocks = self._inode_blocks(raw, allow_indirect=True)
        indirect_block = self._u32(raw, 40 + _DIRECT_BLOCKS * 4)
        self._remove_entry(parent, entry)
        for block in blocks:
            self._free_block(block)
        if indirect_block:
            self._free_block(indirect_block)
        parent_raw = self._read_inode(parent)
        if is_dir:
            links = self._u16(parent_raw, 26)
            if links <= 2:
                raise NativeExt2Error("ext2 parent link counter underflow")
            struct.pack_into("<H", parent_raw, 26, links - 1)
            self._write_inode(parent, parent_raw)
        self._free_inode(inode_num, is_dir=is_dir)

    def rename(self, source: str, destination: str) -> None:
        src_parent, src_name, _ = self._parent_and_name(source)
        src_entry = self._lookup_child(src_parent, src_name)
        if src_entry is None:
            raise FileNotFoundError(source)
        dst_parent, dst_name, _ = self._parent_and_name(destination)
        if self._lookup_child(dst_parent, dst_name) is not None:
            raise FileExistsError(destination)
        inode_num = src_entry["inode"]
        inode_raw = self._read_inode(inode_num)
        mode = self._check_mutable_inode(inode_raw, source)
        is_dir = stat.S_IFMT(mode) == stat.S_IFDIR
        if is_dir and src_parent != dst_parent:
            # Reject moving a directory into itself or one of its descendants.
            current = dst_parent
            seen = set()
            while current != 2:
                if current == inode_num:
                    raise NativeExt2Error("cannot move a directory into itself")
                if current in seen:
                    raise NativeExt2Error("cycle detected in ext2 parent directories")
                seen.add(current)
                parent_entry = self._lookup_child(current, b"..")
                if parent_entry is None:
                    raise NativeExt2Error("directory has no '..' entry")
                current = parent_entry["inode"]
        self._remove_entry(src_parent, src_entry)
        self._insert_entry(dst_parent, dst_name, inode_num, 2 if is_dir else 1)
        if is_dir and src_parent != dst_parent:
            # Update the moved directory's '..' entry.
            _, _, dotdot = self._dir_entries(inode_num)
            entry = next((item for item in dotdot if item["name_bytes"] == b".."), None)
            if entry is None:
                raise NativeExt2Error("directory has no '..' entry")
            buf = bytearray(self._read_block(entry["block"]))
            struct.pack_into("<I", buf, entry["offset"], dst_parent)
            self._write_block(entry["block"], bytes(buf))
            old_parent = self._read_inode(src_parent)
            new_parent = self._read_inode(dst_parent)
            old_links = self._u16(old_parent, 26)
            if old_links <= 2:
                raise NativeExt2Error("ext2 parent link counter underflow")
            struct.pack_into("<H", old_parent, 26, old_links - 1)
            new_links = self._u16(new_parent, 26)
            if new_links == 0xFFFF:
                raise NativeExt2Error("ext2 parent link counter overflow")
            struct.pack_into("<H", new_parent, 26, new_links + 1)
            self._touch_inode(old_parent)
            self._touch_inode(new_parent)
            self._write_inode(src_parent, old_parent)
            self._write_inode(dst_parent, new_parent)

    def free_space(self) -> int:
        return self.free_blocks * self.block_size

    def validate_after_mutation(self) -> None:
        # Reload from disk so every pointer, group descriptor and superblock
        # counter is parsed again from the exact bytes that would be committed.
        fresh = _Ext2FS(self.path, self.offset, self.volume_size)
        if (
            fresh.free_blocks != self.free_blocks
            or fresh.free_inodes != self.free_inodes
        ):
            raise NativeExt2Error("ext2 primary counters did not survive read-back")
        for group, desc in enumerate(fresh._descriptors):
            bitmap_off, _ = fresh._bitmap_location(group, inode=False)
            bitmap = fresh._read_at(bitmap_off, fresh.block_size)
            used = sum(
                1
                for bit in range(fresh._group_block_count(group))
                if fresh._bit_is_set(bitmap, bit)
            )
            if fresh._u16(desc, 12) != fresh._group_block_count(group) - used:
                raise NativeExt2Error("ext2 group free-block count failed validation")
            inode_off, _ = fresh._bitmap_location(group, inode=True)
            inode_bitmap = fresh._read_at(inode_off, fresh.block_size)
            n_inodes = min(
                fresh.inodes_per_group,
                fresh.inodes_count - group * fresh.inodes_per_group,
            )
            used_inodes = sum(
                1
                for bit in range(max(0, n_inodes))
                if fresh._bit_is_set(inode_bitmap, bit)
            )
            if fresh._u16(desc, 14) != n_inodes - used_inodes:
                raise NativeExt2Error("ext2 group free-inode count failed validation")


class NativeExt2ImageWriter:
    """Transactional wrapper for the phase-1 native ext2 profile."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, image_path, offset=0, volume_size=None):
        self.image_path = os.path.realpath(os.fspath(image_path))
        self.offset = int(offset)
        self.volume_size = int(volume_size) if volume_size is not None else None
        with self._locks_guard:
            self._lock = self._locks.setdefault(self.image_path, threading.RLock())
        with self._lock:
            if os.path.lexists(self._recovery_marker_path):
                with _image_process_lock(self.image_path):
                    self._recover_pending_transaction()
            self._fs = _Ext2FS(self.image_path, self.offset, self.volume_size)

    @property
    def _recovery_marker_path(self) -> str:
        return self.image_path + ".mountfile-ext2-recovery.json"

    @staticmethod
    def _hash_extent(path: str, offset: int, size: int) -> bytes:
        digest = hashlib.sha256()
        remaining = size
        with open(path, "rb") as stream:
            stream.seek(offset)
            while remaining:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise NativeExt2Error("short read while hashing recovery extent")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.digest()

    @staticmethod
    def _fsync_parent(path: str) -> None:
        if os.name == "nt":
            return
        directory = os.path.dirname(path) or "."
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _atomic_write_json(path: str, payload: dict) -> None:
        directory = os.path.dirname(path) or "."
        fd, temporary = tempfile.mkstemp(
            prefix=".mountfile-ext2-marker-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            NativeExt2ImageWriter._fsync_parent(path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _preserve_recovery_snapshot(
        self,
        rollback: str,
        original_hash: bytes,
        *,
        phase: str = "commit_in_progress",
    ) -> tuple[str, str]:
        """Persist a verified before-image and recovery marker beside the image."""
        if phase not in {"commit_in_progress", "rollback_failed"}:
            raise NativeExt2Error("invalid ext2 recovery phase")
        directory = os.path.dirname(self.image_path) or "."
        basename = os.path.basename(self.image_path)
        fd, snapshot = tempfile.mkstemp(
            prefix=f".{basename}.mountfile-ext2-recovery-",
            suffix=".bin",
            dir=directory,
        )
        digest = hashlib.sha256()
        snapshot_ready = False
        try:
            with open(rollback, "rb") as src, os.fdopen(fd, "wb") as dst:
                remaining = self.filesystem_size
                while remaining:
                    chunk = src.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise NativeExt2Error("rollback snapshot is truncated")
                    written = dst.write(chunk)
                    if written != len(chunk):
                        raise NativeExt2Error(
                            "short write while preserving recovery snapshot"
                        )
                    digest.update(chunk)
                    remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if digest.digest() != original_hash:
                raise NativeExt2Error("persistent rollback snapshot hash mismatch")
            self._fsync_parent(snapshot)
            snapshot_ready = True
            current_hash = self._hash_extent(
                self.image_path, self.offset, self.filesystem_size
            )
            if phase == "commit_in_progress" and current_hash != original_hash:
                raise NativeExt2Error(
                    "image changed before ext2 recovery marker was established"
                )
            image_stat = os.stat(self.image_path)
            marker = {
                "format": 2,
                "phase": phase,
                "image": self.image_path,
                "offset": self.offset,
                "size": self.filesystem_size,
                "original_sha256": original_hash.hex(),
                "observed_sha256": current_hash.hex(),
                "device": image_stat.st_dev,
                "inode": image_stat.st_ino,
                "image_size": image_stat.st_size,
                "mtime_ns": image_stat.st_mtime_ns,
                "snapshot": os.path.basename(snapshot),
            }
            self._atomic_write_json(self._recovery_marker_path, marker)
            return snapshot, self._recovery_marker_path
        except Exception as exc:
            if snapshot_ready:
                raise NativeExt2Error(
                    "persistent ext2 recovery snapshot retained at %s, but its "
                    "automatic recovery marker could not be safely written: %s"
                    % (snapshot, exc)
                ) from exc
            try:
                os.unlink(snapshot)
            except OSError:
                pass
            raise

    @staticmethod
    def _clear_recovery_snapshot(marker_path: str, snapshot: str) -> None:
        """Remove the marker before its snapshot, preserving crash-safe ordering."""
        if os.path.lexists(marker_path):
            if os.path.islink(marker_path):
                raise NativeExt2Error(
                    "ext2 recovery marker became a symbolic link; refusing cleanup"
                )
            os.unlink(marker_path)
            NativeExt2ImageWriter._fsync_parent(marker_path)
        if os.path.lexists(snapshot):
            if os.path.islink(snapshot):
                raise NativeExt2Error(
                    "ext2 recovery snapshot became a symbolic link; refusing cleanup"
                )
            os.unlink(snapshot)
            NativeExt2ImageWriter._fsync_parent(snapshot)

    def _mark_rollback_failed(self, marker_path: str) -> None:
        """Tighten recovery to the exact post-failure image after rollback fails."""
        with open(marker_path, "r", encoding="utf-8") as stream:
            marker = json.load(stream)
        if not isinstance(marker, dict) or marker.get("format") != 2:
            raise NativeExt2Error("ext2 recovery marker changed during rollback")
        image_stat = os.stat(self.image_path)
        marker["phase"] = "rollback_failed"
        marker["observed_sha256"] = self._hash_extent(
            self.image_path, self.offset, self.filesystem_size
        ).hex()
        marker["mtime_ns"] = image_stat.st_mtime_ns
        self._atomic_write_json(marker_path, marker)

    def _recover_pending_transaction(self) -> None:
        marker_path = self._recovery_marker_path
        if not os.path.lexists(marker_path):
            return
        if os.path.islink(marker_path):
            raise NativeExt2Error(
                "ext2 recovery marker is a symbolic link; refusing recovery"
            )
        try:
            with open(marker_path, "r", encoding="utf-8") as stream:
                marker = json.load(stream)
        except (OSError, ValueError) as exc:
            raise NativeExt2Error(
                "ext2 recovery marker is unreadable; writes are blocked"
            ) from exc
        if not isinstance(marker, dict) or marker.get("format") not in (1, 2):
            raise NativeExt2Error("unknown ext2 recovery marker; writes are blocked")
        marker_format = marker["format"]
        phase = marker.get("phase", "rollback_failed")
        if marker_format == 2 and phase not in {
            "commit_in_progress",
            "rollback_failed",
        }:
            raise NativeExt2Error("unknown ext2 recovery phase; writes are blocked")
        try:
            size = int(marker["size"])
            offset = int(marker["offset"])
            snapshot_name = marker["snapshot"]
            original_hash = bytes.fromhex(marker["original_sha256"])
            observed_hash = bytes.fromhex(marker["observed_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeExt2Error(
                "invalid ext2 recovery marker; writes are blocked"
            ) from exc
        if (
            marker.get("image") != self.image_path
            or offset != self.offset
            or size <= 0
            or (self.volume_size is not None and self.volume_size != size)
            or len(original_hash) != 32
            or len(observed_hash) != 32
            or not isinstance(snapshot_name, str)
            or os.path.basename(snapshot_name) != snapshot_name
            or not snapshot_name.startswith(
                f".{os.path.basename(self.image_path)}.mountfile-ext2-recovery-"
            )
            or not snapshot_name.endswith(".bin")
        ):
            raise NativeExt2Error(
                "ext2 recovery marker does not match this image extent"
            )
        snapshot = os.path.join(os.path.dirname(self.image_path), snapshot_name)
        if os.path.islink(snapshot) or not os.path.isfile(snapshot):
            raise NativeExt2Error(
                "ext2 recovery snapshot is missing or unsafe; writes are blocked"
            )
        try:
            image_stat = os.stat(self.image_path)
            snapshot_stat = os.stat(snapshot)
            if (
                image_stat.st_dev != int(marker["device"])
                or image_stat.st_ino != int(marker["inode"])
                or image_stat.st_size != int(marker["image_size"])
                or snapshot_stat.st_size != size
            ):
                raise NativeExt2Error(
                    "image or recovery snapshot changed; automatic recovery refused"
                )
            if self._hash_extent(snapshot, 0, size) != original_hash:
                raise NativeExt2Error(
                    "ext2 recovery snapshot hash mismatch; writes are blocked"
                )
            current_hash = self._hash_extent(self.image_path, offset, size)
            if current_hash == original_hash:
                # A previous recovery may have fsynced the restored bytes and
                # crashed before removing its marker. Make that path idempotent.
                with open(self.image_path, "r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._hash_extent(self.image_path, offset, size) != original_hash:
                    raise NativeExt2Error(
                        "already-restored ext2 extent changed during recovery"
                    )
                self._clear_recovery_snapshot(marker_path, snapshot)
                return
            if phase == "commit_in_progress" and marker_format == 2:
                # The durable before-image is written before any in-place image
                # mutation. If the process died during commit, restore only the
                # selected filesystem extent; adjacent image bytes are untouched.
                pass
            elif phase == "rollback_failed" and (
                image_stat.st_mtime_ns != int(marker["mtime_ns"])
                or current_hash != observed_hash
            ):
                raise NativeExt2Error(
                    "image changed since ext2 failure; automatic recovery refused"
                )
            elif phase != "rollback_failed":
                raise NativeExt2Error("unknown ext2 recovery phase; writes are blocked")
            remaining = size
            with open(self.image_path, "r+b") as dst, open(snapshot, "rb") as src:
                dst.seek(offset)
                while remaining:
                    chunk = src.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise NativeExt2Error("ext2 recovery snapshot is truncated")
                    written = dst.write(chunk)
                    if written != len(chunk):
                        raise NativeExt2Error(
                            "short write while restoring ext2 recovery snapshot"
                        )
                    remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if self._hash_extent(self.image_path, offset, size) != original_hash:
                raise NativeExt2Error(
                    "restored ext2 recovery extent failed SHA-256 verification"
                )
            self._clear_recovery_snapshot(marker_path, snapshot)
        except NativeExt2Error:
            raise
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise NativeExt2Error(
                "ext2 automatic recovery failed; writes are blocked"
            ) from exc

    @property
    def filesystem_size(self):
        return self._fs.filesystem_size

    @property
    def fs_type(self):
        return "ext2"

    @property
    def write_strategy(self):
        return "native-ext2"

    @property
    def block_size(self):
        return self._fs.block_size

    @classmethod
    def is_supported(cls, image_path, offset=0, volume_size=None) -> tuple[bool, str]:
        try:
            _Ext2FS(image_path, offset, volume_size)
        except (OSError, NativeExt2Error, ValueError) as exc:
            return False, str(exc)
        return True, ""

    def _copy_extent(self, destination: str) -> bytes:
        digest = hashlib.sha256()
        remaining = self.filesystem_size
        with open(self.image_path, "rb") as src, open(destination, "wb") as dst:
            src.seek(self.offset)
            while remaining:
                chunk = src.read(min(_CHUNK, remaining))
                if not chunk:
                    raise NativeExt2Error("short read while staging ext2 extent")
                dst.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        return digest.digest()

    def _hash_source(self) -> bytes:
        digest = hashlib.sha256()
        remaining = self.filesystem_size
        with open(self.image_path, "rb") as stream:
            stream.seek(self.offset)
            while remaining:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise NativeExt2Error("short read while hashing ext2 extent")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.digest()

    def _restore_extent(self, rollback: str, original_hash: bytes) -> None:
        rollback_error = None
        try:
            remaining = self.filesystem_size
            with open(self.image_path, "r+b") as dst, open(rollback, "rb") as src:
                dst.seek(self.offset)
                while remaining:
                    chunk = src.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise NativeExt2Error("rollback ext2 extent is truncated")
                    written = dst.write(chunk)
                    if written != len(chunk):
                        raise NativeExt2Error("short write while restoring ext2 extent")
                    remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
        except Exception as exc:
            rollback_error = exc

        try:
            restored_hash = self._hash_source()
        except Exception as verification_error:
            if rollback_error is not None:
                raise NativeExt2Error(
                    "ext2 rollback write failed and restoration verification failed: %s"
                    % rollback_error
                ) from verification_error
            raise NativeExt2Error(
                "ext2 rollback restoration verification failed"
            ) from verification_error

        if restored_hash != original_hash:
            if rollback_error is not None:
                raise NativeExt2Error(
                    "ext2 rollback write failed and restored extent SHA-256 did not "
                    "match: %s" % rollback_error
                )
            raise NativeExt2Error("restored ext2 extent failed SHA-256 verification")
        if rollback_error is not None:
            raise rollback_error

    def _commit(self, staged: str, original_hash: bytes) -> None:
        if self._hash_source() != original_hash:
            raise NativeExt2Error(
                "image changed concurrently; staged write was discarded"
            )
        with tempfile.TemporaryDirectory(prefix="mountfile-ext2-rollback-") as work:
            rollback = os.path.join(work, "original.ext2")
            current_hash = self._copy_extent(rollback)
            if current_hash != original_hash:
                raise NativeExt2Error("image changed while preparing rollback snapshot")
            snapshot, marker = self._preserve_recovery_snapshot(
                rollback, original_hash, phase="commit_in_progress"
            )
            staged_hash = hashlib.sha256()
            remaining = self.filesystem_size
            try:
                with open(self.image_path, "r+b") as dst, open(staged, "rb") as src:
                    dst.seek(self.offset)
                    while remaining:
                        chunk = src.read(min(_CHUNK, remaining))
                        if not chunk:
                            raise NativeExt2Error("staged ext2 extent is truncated")
                        written = dst.write(chunk)
                        if written != len(chunk):
                            raise NativeExt2Error(
                                "short write while committing ext2 extent"
                            )
                        staged_hash.update(chunk)
                        remaining -= len(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if self._hash_source() != staged_hash.digest():
                    raise NativeExt2Error(
                        "committed ext2 extent failed SHA-256 verification"
                    )
                self._clear_recovery_snapshot(marker, snapshot)
            except Exception as exc:
                try:
                    self._restore_extent(rollback, original_hash)
                except Exception as rollback_exc:
                    try:
                        self._mark_rollback_failed(marker)
                        recovery_detail = (
                            f"durable recovery snapshot: {snapshot}; marker: {marker}"
                        )
                    except Exception as preserve_exc:
                        recovery_detail = (
                            f"durable recovery snapshot retained at {snapshot}; "
                            "recovery marker may need manual inspection: "
                            f"{preserve_exc}"
                        )
                    raise NativeExt2Error(
                        "ext2 commit and rollback both failed; "
                        f"{recovery_detail}; rollback error: {rollback_exc}"
                    ) from exc
                try:
                    self._clear_recovery_snapshot(marker, snapshot)
                except Exception as cleanup_exc:
                    raise NativeExt2Error(
                        "ext2 rollback succeeded but recovery sidecar cleanup failed; "
                        "the image is restored; inspect remaining sidecars before retry: "
                        f"{cleanup_exc}"
                    ) from exc
                raise

    def _mutate(self, method: str, *args, **kwargs):
        with self._lock, _image_process_lock(self.image_path):
            self._recover_pending_transaction()
            current = _Ext2FS(self.image_path, self.offset, self.volume_size)
            if current.filesystem_size != self.filesystem_size:
                raise NativeExt2Error(
                    "ext2 geometry changed since writer initialization"
                )
            self._fs = current
            original_hash = self._hash_source()
            with tempfile.TemporaryDirectory(prefix="mountfile-ext2-stage-") as work:
                staged = os.path.join(work, "filesystem.ext2")
                copied_hash = self._copy_extent(staged)
                if copied_hash != original_hash:
                    raise NativeExt2Error("image changed while staging ext2 mutation")
                fs = _Ext2FS(staged, 0, self.filesystem_size)
                result = getattr(fs, method)(*args, **kwargs)
                with open(staged, "r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                fs.validate_after_mutation()
                self._commit(staged, original_hash)
                self._fs = _Ext2FS(self.image_path, self.offset, self.volume_size)
                return result

    def read_file(self, path: str) -> bytes:
        with self._lock:
            return self._fs.read_file(path)

    def list_dir(self, path="/") -> list[dict]:
        with self._lock:
            return self._fs.list_dir(path)

    def write_file(self, path: str, data: bytes) -> None:
        self._mutate("write_file", path, data)

    def mkdir(self, path: str) -> None:
        self._mutate("mkdir", path)

    def delete(self, path: str, recursive=False) -> None:
        self._mutate("delete", path, recursive=recursive)

    def rename(self, source: str, destination: str) -> None:
        self._mutate("rename", source, destination)

    def free_space(self):
        return self._fs.free_space()

    def close(self):
        return None
