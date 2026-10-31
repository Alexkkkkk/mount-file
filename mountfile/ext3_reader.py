# SPDX-License-Identifier: MIT
"""Conservative read-only ext3/ext4 profiles.

This module intentionally supports only the classic ext block-pointer layout
plus the bounded ext4 extent profile. 64-bit group descriptors, metadata
checksums, fast commits and other layouts are rejected until their complete
validators exist. Journal replay is never written back: it is rejected by
default and can only be enabled as an explicit in-memory read-only overlay.
"""

from __future__ import annotations

import math
import struct

from mountfile.ext2_reader import (
    _COMPAT_EXT_ATTR,
    _COMPAT_HAS_JOURNAL,
    _COMPAT_RESIZE_INODE,
    _EXT2_COMPR_FL,
    _EXT2_INDEX_FL,
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
from mountfile.ext_probe import (
    _COMPAT_FAST_COMMIT,
    _COMPAT_ORPHAN_FILE,
    _COMPAT_SPARSE_SUPER2,
    _INCOMPAT_64BIT,
    _INCOMPAT_FLEX_BG,
    _RO_COMPAT_BIGALLOC,
    _RO_COMPAT_METADATA_CSUM,
)

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_JBD2_TAG_LAST = 0x00000008
_JBD2_TAG_SAME_UUID = 0x00000002
_MAX_JOURNAL_BLOCKS = 1 << 20
_RO_COMPAT_EXTRA_ISIZE = 0x0040
_EXT4_EXTENTS_MAGIC = 0xF30A
_MAX_EXTENT_TREE_DEPTH = 5
_EXT4_METADATA_CSUM_SUPPORTED = False


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
                "metadata checksums (metadata_csum) are fail-closed: superblock "
                "CRC32C is validated, "
                "but UUID-seeded group-descriptor, inode/bitmap, extent and "
                "checksummed-JBD2 structures are not validated"
            )

    def __init__(self, image_path, offset=0, volume_size=None, *, replay_journal=False):
        self._extent_cache_last = {}
        self._journal_overlay: dict[int, bytes] = {}
        self.journal_replayed = False
        super().__init__(image_path, offset=offset, volume_size=volume_size)
        try:
            self.has_journal = bool(self.feature_compat & _COMPAT_HAS_JOURNAL)
            if self.has_journal:
                self._validate_empty_journal(replay_journal=replay_journal)
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

    def _journal_logical_block(self, inode, logical, journal_blocks):
        """Read one journal block after checking both ring and filesystem bounds."""
        if logical < 0 or logical >= journal_blocks:
            raise Ext3ReadError("JBD2 logical block is outside journal inode")
        return self._journal_block(inode, logical)

    def _journal_data_block_numbers(self, inode, journal_blocks) -> set[int]:
        """Return all physical data blocks owned by the bounded journal inode."""
        physical_blocks = set()
        for logical in range(journal_blocks):
            physical = self._logical_block(inode, logical)
            if not 0 < physical < self.blocks_count:
                raise Ext3ReadError(
                    "JBD2 journal inode contains a sparse or out-of-range data block"
                )
            if physical in physical_blocks:
                raise Ext3ReadError("JBD2 journal inode has duplicate data blocks")
            physical_blocks.add(physical)
        return physical_blocks

    @staticmethod
    def _jbd2_header(raw):
        if len(raw) < 12:
            raise Ext3ReadError("short JBD2 record header")
        return struct.unpack_from(">III", raw, 0)

    def _validate_jbd2_transaction(
        self,
        inode,
        journal_blocks,
        logical,
        expected_sequence,
        journal_data_blocks: set[int] | None = None,
    ) -> int:
        """Validate one bounded descriptor/data/commit chain.

        This deliberately supports only the classic, no-checksum JBD2 tag
        layout.  It never applies data blocks and rejects revoke, checksum and
        otherwise unknown records rather than guessing their extent.
        """
        descriptor = self._journal_logical_block(inode, logical, journal_blocks)
        magic, record_type, sequence = self._jbd2_header(descriptor)
        if magic != _JBD2_MAGIC or record_type != _JBD2_DESCRIPTOR:
            raise Ext3ReadError("invalid JBD2 transaction descriptor")
        if sequence == 0 or (
            expected_sequence is not None and sequence != expected_sequence
        ):
            raise Ext3ReadError("JBD2 descriptor sequence mismatch")

        cursor = 12
        tag_count = 0
        last = False
        target_blocks: set[int] = set()
        while cursor + 8 <= self.block_size:
            block_number, flags = struct.unpack_from(">II", descriptor, cursor)
            cursor += 8
            tag_count += 1
            if block_number == 0 or block_number >= self.blocks_count:
                raise Ext3ReadError("JBD2 descriptor block is outside filesystem")
            if block_number in target_blocks:
                raise Ext3ReadError("JBD2 transaction targets a block more than once")
            target_blocks.add(block_number)
            if journal_data_blocks is not None and block_number in journal_data_blocks:
                raise Ext3ReadError(
                    "JBD2 descriptor target overlaps journal inode data"
                )
            if flags & ~(_JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID):
                raise Ext3ReadError("unsupported JBD2 descriptor tag flags")
            if not flags & _JBD2_TAG_SAME_UUID:
                if cursor + 16 > self.block_size:
                    raise Ext3ReadError("truncated JBD2 descriptor UUID")
                if descriptor[cursor : cursor + 16] != self.profile.filesystem_uuid:
                    raise Ext3ReadError("JBD2 descriptor UUID mismatch")
                cursor += 16
            if flags & _JBD2_TAG_LAST:
                last = True
                break
        if not last or tag_count == 0:
            raise Ext3ReadError("unterminated or empty JBD2 descriptor")

        # Every tag has one following data block; the next block must be the
        # matching commit record.  Ring arithmetic is explicit and bounded.
        data_start = logical + 1
        commit_logical = data_start + tag_count
        if commit_logical >= journal_blocks:
            raise Ext3ReadError("JBD2 transaction exceeds journal bounds")
        for data_logical in range(data_start, commit_logical):
            self._journal_logical_block(inode, data_logical, journal_blocks)
        commit = self._journal_logical_block(inode, commit_logical, journal_blocks)
        commit_magic, commit_type, commit_sequence = self._jbd2_header(commit)
        if (
            commit_magic != _JBD2_MAGIC
            or commit_type != _JBD2_COMMIT
            or commit_sequence != sequence
        ):
            raise Ext3ReadError("JBD2 commit record sequence/type mismatch")
        return commit_logical

    def _validate_empty_journal(self, *, replay_journal=False) -> None:
        journal_inode = self.profile.journal_inode
        if journal_inode is None:
            raise Ext3ReadError("internal JBD2 journal inode is not declared")
        journal = self._read_inode(journal_inode)
        if not journal.is_regular:
            raise Ext3ReadError("journal inode is not a regular file")
        if journal.size < self.block_size or journal.size % self.block_size:
            raise Ext3ReadError("journal size is not block aligned")
        journal_blocks = journal.size // self.block_size
        if journal_blocks < 2 or journal_blocks > _MAX_JOURNAL_BLOCKS:
            raise Ext3ReadError("journal exceeds conservative scan limit")
        raw = self._journal_block(journal, 0)
        if len(raw) < 0x40:
            raise Ext3ReadError("short JBD2 superblock")
        magic, block_type, sequence = struct.unpack_from(">III", raw, 0)
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
        if start != 0 and not first <= start < max_length:
            raise Ext3ReadError("JBD2 start is outside journal ring")
        journal_data_blocks = self._journal_data_block_numbers(journal, journal_blocks)
        if sequence == 0 and start != 0:
            # Some e2fsprogs-created empty journals retain a nonzero head but
            # encode no transaction sequence.  Prove that the entire bounded
            # post-head range is zero-filled before treating it as empty.
            if any(
                self._journal_logical_block(journal, logical, journal_blocks).strip(
                    b"\x00"
                )
                for logical in range(start, max_length)
            ):
                raise Ext3ReadError("invalid JBD2 journal sequence")
            start = 0
        # A nonzero start means an uncheckpointed transaction.  The default
        # route remains fail-closed; an explicit read-only replay can expose a
        # bounded in-memory overlay without changing the source image.
        if start != 0:
            if not replay_journal:
                raise Ext3ReadError(
                    "JBD2 journal contains an uncheckpointed transaction; "
                    "pass replay_journal=True for read-only overlay replay"
                )
            self._replay_journal(
                journal,
                first=first,
                start=start,
                max_length=max_length,
                journal_blocks=journal_blocks,
                expected_sequence=sequence,
                journal_data_blocks=journal_data_blocks,
            )
            self.journal_replayed = True
            return
        logical = first
        while logical < max_length:
            block = self._journal_logical_block(journal, logical, journal_blocks)
            if not block.strip(b"\x00"):
                logical += 1
                continue
            _magic, block_kind, record_sequence = self._jbd2_header(block)
            if block_kind != _JBD2_DESCRIPTOR:
                raise Ext3ReadError("unexpected JBD2 journal record")
            logical = (
                self._validate_jbd2_transaction(
                    journal,
                    max_length,
                    logical,
                    record_sequence,
                    journal_data_blocks=journal_data_blocks,
                )
                + 1
            )

    def _replay_journal(
        self,
        inode,
        *,
        start,
        max_length,
        journal_blocks,
        expected_sequence,
        journal_data_blocks: set[int] | None = None,
        first: int | None = None,
    ):
        """Replay classic committed JBD2 records into a read-only overlay.

        Only the old descriptor/data/commit layout is accepted. No revoke or
        checksum record is interpreted, and the image is never opened for
        writing. Records may wrap from ``max_length - 1`` to ``first``; the
        bounded ring is consumed at most once, and an explicit empty record
        must terminate it. Unsupported records fail closed instead of being
        guessed.
        """
        if first is None:
            first = 1
        if not 1 <= first < max_length <= journal_blocks:
            raise Ext3ReadError("JBD2 replay ring geometry is invalid")
        if not first <= start < max_length:
            raise Ext3ReadError("JBD2 replay start is outside journal ring")
        logical = start
        ring_length = max_length - first
        consumed = 0
        transactions = 0
        overlay = {}
        terminated = False
        while consumed < ring_length:
            descriptor = self._journal_logical_block(inode, logical, journal_blocks)
            if not descriptor.strip(b"\x00"):
                if transactions == 0:
                    raise Ext3ReadError("first JBD2 replay descriptor is empty")
                terminated = True
                break
            magic, record_type, sequence = self._jbd2_header(descriptor)
            if magic != _JBD2_MAGIC or record_type != _JBD2_DESCRIPTOR:
                raise Ext3ReadError("invalid JBD2 replay descriptor")
            if sequence != expected_sequence:
                raise Ext3ReadError(
                    "JBD2 replay descriptor sequence disagrees with superblock"
                )
            cursor = 12
            targets = []
            seen_targets: set[int] = set()
            last = False
            while cursor + 8 <= self.block_size:
                target, flags = struct.unpack_from(">II", descriptor, cursor)
                cursor += 8
                if target == 0 or target >= self.blocks_count:
                    raise Ext3ReadError("JBD2 replay target is outside filesystem")
                if target in seen_targets:
                    raise Ext3ReadError(
                        "JBD2 replay transaction targets a block more than once"
                    )
                seen_targets.add(target)
                if journal_data_blocks is not None and target in journal_data_blocks:
                    raise Ext3ReadError(
                        "JBD2 replay target overlaps journal inode data"
                    )
                if flags & ~(_JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID):
                    raise Ext3ReadError("unsupported JBD2 replay tag flags")
                if not flags & _JBD2_TAG_SAME_UUID:
                    if cursor + 16 > self.block_size:
                        raise Ext3ReadError("truncated JBD2 replay descriptor UUID")
                    if descriptor[cursor : cursor + 16] != self.profile.filesystem_uuid:
                        raise Ext3ReadError("JBD2 replay descriptor UUID mismatch")
                    cursor += 16
                targets.append(target)
                if flags & _JBD2_TAG_LAST:
                    last = True
                    break
            if not targets or not last:
                raise Ext3ReadError("unterminated JBD2 replay descriptor")
            transaction_blocks = 2 + len(targets)
            if transaction_blocks > ring_length - consumed:
                raise Ext3ReadError("JBD2 replay transaction exceeds journal bounds")

            def ring_position(relative):
                return first + ((logical - first + relative) % ring_length)

            data_blocks = [
                self._journal_logical_block(
                    inode, ring_position(1 + index), journal_blocks
                )
                for index in range(len(targets))
            ]
            commit_logical = ring_position(1 + len(targets))
            commit = self._journal_logical_block(inode, commit_logical, journal_blocks)
            commit_magic, commit_type, commit_sequence = self._jbd2_header(commit)
            if (
                commit_magic != _JBD2_MAGIC
                or commit_type != _JBD2_COMMIT
                or commit_sequence != sequence
            ):
                raise Ext3ReadError("JBD2 replay commit does not match descriptor")
            for target, data in zip(targets, data_blocks):
                overlay[target] = data
            transactions += 1
            if transactions > _MAX_JOURNAL_BLOCKS:
                raise Ext3ReadError("JBD2 replay transaction count is excessive")
            expected_sequence = (expected_sequence + 1) & 0xFFFFFFFF
            consumed += transaction_blocks
            logical = ring_position(transaction_blocks)
        # Do not expose a partially replayed view if a later transaction is
        # malformed. Publish only after the complete supported log prefix has
        # been validated successfully, a committed transaction was found, and
        # an unused-record terminator proves this bounded ring is complete.
        if transactions == 0:
            raise Ext3ReadError("JBD2 replay verified no committed transaction")
        if not terminated:
            raise Ext3ReadError(
                "JBD2 replay reaches end of non-wrapping log without terminator"
            )
        self._journal_overlay = overlay
        # Metadata loaded while resolving the journal inode predates this
        # overlay. Drop every cache whose values are sourced through _read_at
        # so subsequent structural reads observe one coherent replayed view.
        self._group_cache.clear()
        self._block_bitmap_cache.clear()
        self._inode_bitmap_cache.clear()
        self._inode_cache.clear()
        self._directory_cache.clear()
        self._extent_cache_last.clear()

    def _read_at(self, relative_offset: int, size: int) -> bytes:
        """Read the image and overlay only committed journal blocks."""
        data = bytearray(super()._read_at(relative_offset, size))
        if not self._journal_overlay or not data:
            return bytes(data)
        first_block = relative_offset // self.block_size
        last_block = (relative_offset + size - 1) // self.block_size
        for block in range(first_block, last_block + 1):
            replacement = self._journal_overlay.get(block)
            if replacement is None:
                continue
            block_start = block * self.block_size
            overlap_start = max(relative_offset, block_start)
            overlap_end = min(relative_offset + size, block_start + self.block_size)
            source_start = overlap_start - block_start
            target_start = overlap_start - relative_offset
            amount = overlap_end - overlap_start
            data[target_start : target_start + amount] = replacement[
                source_start : source_start + amount
            ]
        return bytes(data)


class NativeExt4Image(NativeExt3Image):
    """Bounded classic-layout/extent-tree ext4 read-only profile.

    Alternate group layouts and metadata integrity schemes are deliberately
    rejected. Extent ``i_block`` data is never passed through the classic
    block-pointer mapper.
    """

    backend_name = "native-ext4-ro"

    @staticmethod
    def _validate_profile(profile) -> None:
        if profile.fs_type != "ext4":
            raise Ext3ReadError("native ext4 reader requires an ext4 profile")
        if profile.unknown_incompat_features:
            raise Ext3ReadError(
                "unknown incompatible ext4 feature flags "
                f"0x{profile.unknown_incompat_features:08x}"
            )
        if profile.unknown_ro_compat_features:
            raise Ext3ReadError(
                "unknown read-only-compatible ext4 feature flags "
                f"0x{profile.unknown_ro_compat_features:08x}"
            )
        if profile.feature_incompat & _INCOMPAT_FLEX_BG:
            raise Ext3ReadError("ext4 flex_bg group layout is not supported")
        if profile.feature_ro_compat & _RO_COMPAT_BIGALLOC:
            raise Ext3ReadError("ext4 bigalloc cluster layout is not supported")
        if (
            profile.feature_ro_compat & _RO_COMPAT_METADATA_CSUM
            and not _EXT4_METADATA_CSUM_SUPPORTED
        ):
            raise Ext3ReadError(
                "metadata checksums (metadata_csum) are fail-closed: superblock "
                "CRC32C is validated, "
                "but UUID-seeded group-descriptor, inode/bitmap, extent and "
                "checksummed-JBD2 structures are not validated"
            )
        if profile.feature_compat & (
            _COMPAT_FAST_COMMIT | _COMPAT_ORPHAN_FILE | _COMPAT_SPARSE_SUPER2
        ):
            raise Ext3ReadError(
                "ext4 fast-commit/orphan/sparse-super2 features are not supported"
            )
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
            _EXT2_COMPR_FL
            | _EXT4_EA_INODE_FL
            | _EXT4_ENCRYPT_FL
            | _EXT4_VERITY_FL
            | _EXT4_INLINE_DATA_FL
        )
        if flags & unsupported_flags:
            raise Ext3ReadError("inode uses an unsupported ext4 data layout")
        if mode & 0xF000 == 0x4000 and flags & _EXT2_INDEX_FL:
            raise Ext3ReadError("indexed directories are not supported by this profile")
        if flags & _EXT4_EXTENTS_FL:
            if self.profile.feature_incompat & 0x0040 == 0:
                raise Ext3ReadError("inode extent flag lacks EXTENTS feature")
        elif self.profile.feature_incompat & 0x0040:
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
        if not flags & _EXT4_EXTENTS_FL:
            addressable_blocks = 12 + self.block_size // 4
            addressable_blocks += (self.block_size // 4) ** 2 + (
                self.block_size // 4
            ) ** 3
            if size > addressable_blocks * self.block_size:
                raise Ext3ReadError("inode size exceeds classic block-pointer capacity")
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
        """Decode 64-bit descriptors with exact bounded metadata ranges."""
        if not self.feature_incompat & _INCOMPAT_64BIT:
            return super()._group_descriptor(group)
        cached = self._group_cache.get(group)
        if cached is not None:
            return cached
        if not 0 <= group < self.group_count:
            raise Ext3ReadError("inode or block references an invalid group")
        raw = self._read_at(
            self._gdt_offset + group * self.group_descriptor_size,
            self.group_descriptor_size,
        )
        if len(raw) < 64:
            raise Ext3ReadError("64-bit group descriptor is truncated")
        low_bitmap, low_inode_bitmap, low_inode_table = struct.unpack_from(
            "<III", raw, 0
        )
        high_bitmap, high_inode_bitmap, high_inode_table = struct.unpack_from(
            "<III", raw, 32
        )
        block_bitmap = low_bitmap | (high_bitmap << 32)
        inode_bitmap = low_inode_bitmap | (high_inode_bitmap << 32)
        inode_table = low_inode_table | (high_inode_table << 32)
        first_block = self.first_data_block + group * self.blocks_per_group
        end_block = min(self.blocks_count, first_block + self.blocks_per_group)
        inode_table_blocks = math.ceil(
            self.inodes_per_group * self.inode_size / self.block_size
        )
        if first_block >= end_block:
            raise Ext3ReadError("empty ext block group")
        ranges = (
            (block_bitmap, block_bitmap + 1),
            (inode_bitmap, inode_bitmap + 1),
            (inode_table, inode_table + inode_table_blocks),
        )
        if any(start < first_block or stop > end_block for start, stop in ranges):
            raise Ext3ReadError("64-bit group descriptor points outside its group")
        if any(stop <= start for start, stop in ranges):
            raise Ext3ReadError("64-bit group descriptor metadata range overflows")
        for index, (left_start, left_stop) in enumerate(ranges):
            for right_start, right_stop in ranges[index + 1 :]:
                if left_start < right_stop and right_start < left_stop:
                    raise Ext3ReadError(
                        "64-bit group descriptor metadata ranges overlap"
                    )
        sparse = bool(self.feature_ro_compat & _RO_COMPAT_SPARSE_SUPER)
        if self._has_super_backup(group, sparse):
            backup_end = min(end_block, first_block + 1 + self._gdt_blocks)
            if any(start < backup_end and stop > first_block for start, stop in ranges):
                raise Ext3ReadError(
                    "64-bit group descriptor metadata overlaps backup superblock metadata"
                )
        if group == 0:
            primary_end = min(end_block, self._primary_metadata_end)
            if any(
                start < primary_end and stop > self._primary_metadata_start
                for start, stop in ranges
            ):
                raise Ext3ReadError(
                    "64-bit group descriptor metadata overlaps primary superblock metadata"
                )
        result = _GroupDescriptor(
            block_bitmap,
            inode_bitmap,
            inode_table,
            first_block,
            end_block,
            inode_table_blocks,
        )
        if len(self._group_cache) >= 4096:
            self._group_cache.clear()
        self._group_cache[group] = result
        return result

    def _extent_block(self, block: int) -> bytes:
        if not 0 < block < self.blocks_count:
            raise Ext3ReadError("extent tree block is outside filesystem bounds")
        return self._read_block(block)

    def _extent_map(self, inode: _Inode, logical: int) -> tuple[int, bool]:
        """Map a visible logical block; never expose ext4 preallocation past i_size.

        Extents may legitimately cover preallocated blocks after EOF. The read
        API clamps byte ranges to ``inode.size``; this lower-level mapping API
        also refuses direct requests outside the rounded-up logical file range.
        Unwritten extents inside that range remain zero-filled by the reader.
        """
        if not inode.flags & _EXT4_EXTENTS_FL:
            return super()._logical_block(inode, logical), False
        if logical < 0 or logical >= 1 << 32:
            raise Ext3ReadError("logical extent index is outside uint32")
        logical_blocks = (inode.size + self.block_size - 1) // self.block_size
        if logical >= logical_blocks:
            raise Ext3ReadError("logical extent lookup exceeds inode size")
        cache = getattr(self, "_extent_cache_last", None)
        if cache is None:
            cache = self._extent_cache_last = {}
        cached = cache.get(inode.number)
        if cached is not None:
            start, end, physical_start, unwritten = cached
            if start <= logical < end:
                return physical_start + logical - start, unwritten
        node = struct.pack("<15I", *inode.block_pointers)
        visited: set[int] = set()
        expected_depth = None
        expected_lower = None
        expected_upper = None
        for current_depth in range(_MAX_EXTENT_TREE_DEPTH + 1):
            if len(node) < 12:
                raise Ext3ReadError("short extent header")
            magic, entries, maximum, node_depth = struct.unpack_from("<HHHH", node, 0)
            capacity = (len(node) - 12) // 12
            if (
                magic != _EXT4_EXTENTS_MAGIC
                or entries > maximum
                or maximum > capacity
                or maximum == 0
                or entries == 0
                and node_depth == 0
            ):
                raise Ext3ReadError("invalid extent header bounds")
            if node_depth > _MAX_EXTENT_TREE_DEPTH or (
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
                    if end <= start or end > 1 << 32:
                        raise Ext3ReadError("extent logical range overflow")
                    if (
                        pos == 0
                        and expected_lower is not None
                        and start != expected_lower
                    ):
                        raise Ext3ReadError(
                            "extent leaf does not start at its index key"
                        )
                    if expected_upper is not None and end > expected_upper:
                        raise Ext3ReadError("extent leaf exceeds its index child range")
                    physical_start = (start_hi << 32) | start_lo
                    if (
                        physical_start <= 0
                        or physical_start + length > self.blocks_count
                    ):
                        raise Ext3ReadError("extent physical range is out of bounds")
                    previous_end = end
                    if start <= logical < end:
                        physical = physical_start + (logical - start)
                        if len(cache) >= 4096:
                            cache.clear()
                        cache[inode.number] = (
                            start,
                            end,
                            physical_start,
                            unwritten,
                        )
                        return physical, unwritten
                return 0, False
            expected_depth = node_depth - 1
            previous_key = -1
            selected = None
            index_entries = []
            for pos in range(entries):
                off = 12 + pos * 12
                logical_start, child_lo, child_hi, reserved = struct.unpack_from(
                    "<IIHH", node, off
                )
                if logical_start >= 1 << 32 or logical_start <= previous_key:
                    raise Ext3ReadError(
                        "extent index logical keys are not strictly increasing"
                    )
                if reserved:
                    raise Ext3ReadError("extent index reserved field is nonzero")
                previous_key = logical_start
                if (
                    expected_lower is not None
                    and pos == 0
                    and logical_start != expected_lower
                ):
                    raise Ext3ReadError(
                        "extent child does not start at its parent index key"
                    )
                if expected_upper is not None and logical_start >= expected_upper:
                    raise Ext3ReadError("extent child index exceeds its parent range")
                child = child_lo | (child_hi << 32)
                if not 0 < child < self.blocks_count:
                    raise Ext3ReadError(
                        "extent index block is outside filesystem bounds"
                    )
                index_entries.append((logical_start, child))
                if logical >= logical_start:
                    selected = pos
            if selected is None:
                return 0, False
            selected_lower, selected_block = index_entries[selected]
            selected_upper = (
                index_entries[selected + 1][0]
                if selected + 1 < len(index_entries)
                else expected_upper
            )
            if selected_block in visited:
                raise Ext3ReadError("cyclic extent tree")
            visited.add(selected_block)
            expected_lower = selected_lower
            expected_upper = selected_upper
            node = self._extent_block(selected_block)
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
            physical, unwritten = self._extent_map(inode, logical)
            blocks_needed = (
                in_block + remaining + self.block_size - 1
            ) // self.block_size
            if physical == 0:
                amount = min(remaining, self.block_size - in_block)
                output.extend(b"\x00" * amount)
            elif unwritten:
                run_blocks = 1
                while run_blocks < blocks_needed:
                    next_physical, next_unwritten = self._extent_map(
                        inode, logical + run_blocks
                    )
                    if not next_unwritten or next_physical == 0:
                        break
                    run_blocks += 1
                amount = min(remaining, run_blocks * self.block_size - in_block)
                output.extend(b"\x00" * amount)
            else:
                run_blocks = 1
                while run_blocks < blocks_needed:
                    next_physical, next_unwritten = self._extent_map(
                        inode, logical + run_blocks
                    )
                    if next_unwritten or next_physical != physical + run_blocks:
                        break
                    run_blocks += 1
                amount = min(remaining, run_blocks * self.block_size - in_block)
                output.extend(
                    self._read_at(
                        physical * self.block_size + in_block,
                        amount,
                    )
                )
            position += amount
            remaining -= amount
        return bytes(output)
