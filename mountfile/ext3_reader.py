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
    _INCOMPAT_FILETYPE,
    _RO_COMPAT_LARGE_FILE,
    _RO_COMPAT_SPARSE_SUPER,
    Ext2ReadError,
    NativeExt2Image,
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
            raise Ext3ReadError("native ext3/ext4 reader requires an ext3 or ext4 profile")
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
        if profile.feature_incompat & ~_INCOMPAT_FILETYPE:
            raise Ext3ReadError("unsupported incompatible ext feature")
        ro_allowed = _RO_COMPAT_SPARSE_SUPER | _RO_COMPAT_LARGE_FILE
        if profile.fs_type == "ext4":
            ro_allowed |= _RO_COMPAT_EXTRA_ISIZE
        if profile.feature_ro_compat & ~ro_allowed:
            raise Ext3ReadError("unsupported read-only-compatible ext feature")
        if profile.fs_type == "ext3" and not profile.has_journal:
            raise Ext3ReadError("ext3 profile has no journal")
        if profile.feature_compat & _COMPAT_HAS_JOURNAL and not profile.has_journal:
            raise Ext3ReadError("journal feature is inconsistent")
        if profile.feature_incompat & 0x0040:  # EXTENTS
            raise Ext3ReadError("extent layout is not enabled in classic profile")
        if profile.feature_incompat & 0x0080:  # 64BIT
            raise Ext3ReadError("64-bit group layout is not enabled in classic profile")
        if profile.feature_ro_compat & 0x0400:  # METADATA_CSUM
            raise Ext3ReadError("metadata checksums are not implemented in this profile")

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
        journal_block_size, max_length = struct.unpack_from(">II", raw, 0x0C)
        first, start = struct.unpack_from(">II", raw, 0x14)
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
    """Classic-layout ext4 read-only profile with an explicit backend name."""

    backend_name = "native-ext4-ro"
