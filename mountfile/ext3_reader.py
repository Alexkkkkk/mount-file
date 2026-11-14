# SPDX-License-Identifier: MIT
"""Conservative read-only ext3/ext4 profiles.

This module intentionally supports only the classic ext block-pointer layout
plus bounded ext4 extent profiles. The accepted ext4 profile includes validated
64-bit group descriptors; metadata checksums, fast commits and other layouts are
rejected until their complete validators exist. Journal replay is never written
back: it is rejected by default and can only be enabled as an explicit in-memory
read-only overlay.
"""

from __future__ import annotations

import math
import struct

from mountfile.ext2_reader import (
    _COMPAT_EXT_ATTR,
    _COMPAT_HAS_JOURNAL,
    _COMPAT_RESIZE_INODE,
)
from mountfile.ext2_reader import _EXT2_COMPR_FL as _EXT2_COMPR_FL
from mountfile.ext2_reader import _EXT2_INDEX_FL
from mountfile.ext2_reader import _EXT4_EA_INODE_FL as _EXT4_EA_INODE_FL
from mountfile.ext2_reader import _EXT4_ENCRYPT_FL as _EXT4_ENCRYPT_FL
from mountfile.ext2_reader import _EXT4_EXTENTS_FL
from mountfile.ext2_reader import _EXT4_INLINE_DATA_FL as _EXT4_INLINE_DATA_FL
from mountfile.ext2_reader import _EXT4_VERITY_FL as _EXT4_VERITY_FL
from mountfile.ext2_reader import (
    _EXT_INODE_READ_FLAGS,
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
from mountfile.safety import RefusalCode

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_JBD2_TAG_ESCAPE = 0x00000001
_JBD2_TAG_LAST = 0x00000008
_JBD2_TAG_SAME_UUID = 0x00000002
_MAX_JOURNAL_BLOCKS = 1 << 20
_RO_COMPAT_EXTRA_ISIZE = 0x0040
_EXT4_EXTENTS_MAGIC = 0xF30A
_MAX_EXTENT_TREE_DEPTH = 5
_EXT4_METADATA_CSUM_SUPPORTED = True

# In addition to the common behavior flags, ext4's extent layout is parsed
# below. EOFBLOCKS only reports preallocation beyond i_size; DAX and
# PROJINHERIT affect access/creation policy, not the stored file bytes.
_EXT4_INODE_READ_FLAGS = (
    _EXT_INODE_READ_FLAGS
    | _EXT4_EXTENTS_FL
    | 0x00400000  # deprecated EOFBLOCKS
    | 0x02000000  # DAX
    | 0x20000000  # PROJINHERIT
)


class Ext3ReadError(Ext2ReadError):
    """The conservative ext3/ext4 read-only profile was rejected."""


class NativeExt3Image(NativeExt2Image):
    """Read classic-pointer ext3/ext4 images with conservative journal policy.

    The inherited block, inode, directory and bounded file readers are safe for
    the classic layout. Journal replay is rejected by default; an explicit
    replay request enables only the bounded in-memory JBD2 overlay.
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
        compat_allowed = (
            _COMPAT_EXT_ATTR
            | _COMPAT_RESIZE_INODE
            | _COMPAT_HAS_JOURNAL
            | 0x0020  # DIR_INDEX: htree directories stay linear-scannable
        )
        if profile.fs_type == "ext4":
            compat_allowed |= _COMPAT_SPARSE_SUPER2
        if profile.feature_compat & ~compat_allowed:
            raise Ext3ReadError("unsupported compatible ext feature")
        if profile.feature_incompat & 0x0008:  # JOURNAL_DEV
            raise Ext3ReadError("external JBD2 journal devices are not supported")
        incompat_allowed = _INCOMPAT_FILETYPE
        if profile.fs_type == "ext4":
            # EXTENTS | 64BIT | FLEX_BG: flex_bg only relocates group metadata
            # inside the filesystem, which the descriptor reader bounds-checks.
            incompat_allowed |= 0x02C0
        if profile.feature_incompat & ~incompat_allowed:
            raise Ext3ReadError("unsupported incompatible ext feature")
        ro_allowed = (
            _RO_COMPAT_SPARSE_SUPER | _RO_COMPAT_LARGE_FILE | _RO_COMPAT_METADATA_CSUM
        )
        if profile.fs_type == "ext4":
            ro_allowed |= (
                _RO_COMPAT_EXTRA_ISIZE
                | 0x0020  # DIR_NLINK
                | 0x0008  # HUGE_FILE: i_size may exceed 2 TiB, still bounded
            )
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
            raise Ext3ReadError(
                f"journal inode/JBD2 validation failed: {exc}", exc.code
            ) from exc
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

    @staticmethod
    def _restore_jbd2_data_block(raw, *, escaped, block_size):
        if len(raw) != block_size:
            raise Ext3ReadError("JBD2 data block has the wrong size")
        magic = struct.pack(">I", _JBD2_MAGIC)
        if escaped:
            if raw[:4] != b"\x00" * 4:
                raise Ext3ReadError(
                    "JBD2 escaped data block has an invalid zero prefix"
                )
            return magic + raw[4:]
        if raw.startswith(magic):
            raise Ext3ReadError("JBD2 data block magic prefix lacks ESCAPE flag")
        return raw

    def _parse_jbd2_revoke(self, record, expected_sequence, journal_data_blocks):
        """Parse one classic 32-bit revoke block without checksum features."""
        magic, record_type, sequence = self._jbd2_header(record)
        if magic != _JBD2_MAGIC or record_type != _JBD2_REVOKE:
            raise Ext3ReadError("invalid JBD2 revoke record")
        if sequence != expected_sequence:
            raise Ext3ReadError("JBD2 revoke sequence disagrees with transaction")
        if len(record) < 16:
            raise Ext3ReadError("short JBD2 revoke record")
        byte_count = struct.unpack_from(">I", record, 12)[0]
        if byte_count < 16 or byte_count > len(record) or (byte_count - 16) % 4:
            raise Ext3ReadError("JBD2 revoke record has invalid byte count")
        targets = []
        seen = set()
        for cursor in range(16, byte_count, 4):
            target = struct.unpack_from(">I", record, cursor)[0]
            if target == 0 or target >= self.blocks_count:
                raise Ext3ReadError("JBD2 revoke target is outside filesystem")
            if target in seen:
                raise Ext3ReadError("JBD2 revoke target is duplicated")
            if journal_data_blocks is not None and target in journal_data_blocks:
                raise Ext3ReadError("JBD2 revoke target overlaps journal inode data")
            seen.add(target)
            targets.append(target)
        if not targets:
            raise Ext3ReadError("JBD2 revoke record contains no targets")
        return targets

    def _validate_jbd2_transaction(
        self,
        inode,
        journal_blocks,
        logical,
        expected_sequence,
        journal_data_blocks: set[int] | None = None,
    ) -> int:
        """Validate one bounded no-checksum transaction in a clean ring.

        The scanner accepts one classic descriptor/data group, or a revoke-only
        transaction, followed by zero or more 32-bit revoke blocks and a commit.
        It never applies the data blocks or guesses unknown record extents.
        """
        record = self._journal_logical_block(inode, logical, journal_blocks)
        magic, record_type, sequence = self._jbd2_header(record)
        if magic != _JBD2_MAGIC or record_type not in (
            _JBD2_DESCRIPTOR,
            _JBD2_REVOKE,
        ):
            raise Ext3ReadError("invalid JBD2 transaction record")
        if sequence == 0 or (
            expected_sequence is not None and sequence != expected_sequence
        ):
            raise Ext3ReadError("JBD2 transaction sequence mismatch")

        target_blocks: set[int] = set()
        revoked_blocks: set[int] = set()
        if record_type == _JBD2_DESCRIPTOR:
            cursor = 12
            tag_count = 0
            last = False
            escaped_tags = []
            while cursor + 8 <= self.block_size:
                block_number, flags = struct.unpack_from(">II", record, cursor)
                cursor += 8
                tag_count += 1
                if block_number == 0 or block_number >= self.blocks_count:
                    raise Ext3ReadError("JBD2 descriptor block is outside filesystem")
                if block_number in target_blocks:
                    raise Ext3ReadError(
                        "JBD2 transaction targets a block more than once"
                    )
                target_blocks.add(block_number)
                if (
                    journal_data_blocks is not None
                    and block_number in journal_data_blocks
                ):
                    raise Ext3ReadError(
                        "JBD2 descriptor target overlaps journal inode data"
                    )
                if flags & ~(_JBD2_TAG_ESCAPE | _JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID):
                    raise Ext3ReadError("unsupported JBD2 descriptor tag flags")
                escaped_tags.append(bool(flags & _JBD2_TAG_ESCAPE))
                if not flags & _JBD2_TAG_SAME_UUID:
                    if cursor + 16 > self.block_size:
                        raise Ext3ReadError("truncated JBD2 descriptor UUID")
                    if record[cursor : cursor + 16] != self.profile.filesystem_uuid:
                        raise Ext3ReadError("JBD2 descriptor UUID mismatch")
                    cursor += 16
                if flags & _JBD2_TAG_LAST:
                    last = True
                    break
            if not last or tag_count == 0:
                raise Ext3ReadError("unterminated or empty JBD2 descriptor")

            control_logical = logical + 1 + tag_count
            if control_logical >= journal_blocks:
                raise Ext3ReadError("JBD2 transaction exceeds journal bounds")
            for index, escaped in enumerate(escaped_tags):
                data = self._journal_logical_block(
                    inode, logical + 1 + index, journal_blocks
                )
                self._restore_jbd2_data_block(
                    data, escaped=escaped, block_size=self.block_size
                )
        else:
            revoked_blocks.update(
                self._parse_jbd2_revoke(record, sequence, journal_data_blocks)
            )
            control_logical = logical + 1

        while control_logical < journal_blocks:
            control = self._journal_logical_block(
                inode, control_logical, journal_blocks
            )
            control_magic, control_type, control_sequence = self._jbd2_header(control)
            if control_magic != _JBD2_MAGIC or control_sequence != sequence:
                break
            if control_type != _JBD2_REVOKE:
                break
            for target in self._parse_jbd2_revoke(
                control, sequence, journal_data_blocks
            ):
                if target in revoked_blocks:
                    raise Ext3ReadError(
                        "JBD2 revoke target is duplicated in transaction"
                    )
                if target in target_blocks:
                    raise Ext3ReadError(
                        "JBD2 transaction both writes and revokes the same block"
                    )
                revoked_blocks.add(target)
            control_logical += 1

        if control_logical >= journal_blocks:
            raise Ext3ReadError("JBD2 transaction exceeds journal bounds")
        commit = self._journal_logical_block(inode, control_logical, journal_blocks)
        commit_magic, commit_type, commit_sequence = self._jbd2_header(commit)
        if (
            commit_magic != _JBD2_MAGIC
            or commit_type != _JBD2_COMMIT
            or commit_sequence != sequence
        ):
            raise Ext3ReadError("JBD2 commit record sequence/type mismatch")
        return control_logical

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
        magic, block_type = struct.unpack_from(">II", raw, 0)
        if magic != _JBD2_MAGIC or block_type not in (3, 4):
            raise Ext3ReadError("invalid JBD2 journal superblock")
        if raw[0x30:0x40] != self.profile.filesystem_uuid:
            raise Ext3ReadError("JBD2 journal UUID does not match filesystem UUID")
        journal_block_size, max_length = struct.unpack_from(">II", raw, 0x0C)
        first = struct.unpack_from(">I", raw, 0x14)[0]
        sequence, start = struct.unpack_from(">II", raw, 0x18)
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
            if block_kind not in (_JBD2_DESCRIPTOR, _JBD2_REVOKE):
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
        """Replay classic descriptor/data/revoke/commit records read-only.

        One descriptor/data group and zero or more 32-bit revoke blocks are
        accepted per transaction. ESCAPE-tagged data has its magic prefix
        restored only in the in-memory overlay. Revoke blocks suppress earlier
        overlay data for their targets; a later transaction can write those
        blocks again. Checksummed and otherwise unknown JBD2 features remain
        unsupported.
        The bounded ring is consumed at most once and must have an empty-record
        terminator. The source image is never opened for writing.
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
        replayed_transactions = []
        terminated = False
        while consumed < ring_length:
            record = self._journal_logical_block(inode, logical, journal_blocks)
            if not record.strip(b"\x00"):
                if transactions == 0:
                    raise Ext3ReadError("first JBD2 replay descriptor is empty")
                terminated = True
                break
            magic, record_type, sequence = self._jbd2_header(record)
            if magic != _JBD2_MAGIC or record_type not in (
                _JBD2_DESCRIPTOR,
                _JBD2_REVOKE,
            ):
                raise Ext3ReadError("invalid JBD2 replay transaction record")
            if sequence != expected_sequence:
                raise Ext3ReadError(
                    "JBD2 replay descriptor sequence disagrees with superblock"
                )

            def ring_position(relative, base=logical):
                return first + ((base - first + relative) % ring_length)

            remaining = ring_length - consumed
            writes = []
            revokes = set()
            if record_type == _JBD2_DESCRIPTOR:
                cursor = 12
                targets = []
                escaped_tags = []
                seen_targets: set[int] = set()
                last = False
                while cursor + 8 <= self.block_size:
                    target, flags = struct.unpack_from(">II", record, cursor)
                    cursor += 8
                    if target == 0 or target >= self.blocks_count:
                        raise Ext3ReadError("JBD2 replay target is outside filesystem")
                    if target in seen_targets:
                        raise Ext3ReadError(
                            "JBD2 replay transaction targets a block more than once"
                        )
                    seen_targets.add(target)
                    if (
                        journal_data_blocks is not None
                        and target in journal_data_blocks
                    ):
                        raise Ext3ReadError(
                            "JBD2 replay target overlaps journal inode data"
                        )
                    if flags & ~(
                        _JBD2_TAG_ESCAPE | _JBD2_TAG_LAST | _JBD2_TAG_SAME_UUID
                    ):
                        raise Ext3ReadError("unsupported JBD2 replay tag flags")
                    if not flags & _JBD2_TAG_SAME_UUID:
                        if cursor + 16 > self.block_size:
                            raise Ext3ReadError("truncated JBD2 replay descriptor UUID")
                        if record[cursor : cursor + 16] != self.profile.filesystem_uuid:
                            raise Ext3ReadError("JBD2 replay descriptor UUID mismatch")
                        cursor += 16
                    targets.append(target)
                    escaped_tags.append(bool(flags & _JBD2_TAG_ESCAPE))
                    if flags & _JBD2_TAG_LAST:
                        last = True
                        break
                if not targets or not last:
                    raise Ext3ReadError("unterminated JBD2 replay descriptor")
                records_before_control = 1 + len(targets)
                if records_before_control >= remaining:
                    raise Ext3ReadError(
                        "JBD2 replay transaction exceeds journal bounds"
                    )
                data_blocks = [
                    self._journal_logical_block(
                        inode, ring_position(1 + index), journal_blocks
                    )
                    for index in range(len(targets))
                ]
                writes = [
                    (
                        target,
                        self._restore_jbd2_data_block(
                            data, escaped=escaped, block_size=self.block_size
                        ),
                    )
                    for target, data, escaped in zip(
                        targets, data_blocks, escaped_tags, strict=True
                    )
                ]
            else:
                revokes.update(
                    self._parse_jbd2_revoke(record, sequence, journal_data_blocks)
                )
                records_before_control = 1

            while True:
                if records_before_control >= remaining:
                    raise Ext3ReadError(
                        "JBD2 replay transaction exceeds journal bounds"
                    )
                control = self._journal_logical_block(
                    inode, ring_position(records_before_control), journal_blocks
                )
                if not control.strip(b"\x00"):
                    raise Ext3ReadError("JBD2 replay transaction is missing commit")
                control_magic, control_type, control_sequence = self._jbd2_header(
                    control
                )
                if control_magic != _JBD2_MAGIC or control_sequence != sequence:
                    raise Ext3ReadError("JBD2 commit does not match descriptor")
                if control_type == _JBD2_REVOKE:
                    for target in self._parse_jbd2_revoke(
                        control, sequence, journal_data_blocks
                    ):
                        if target in revokes:
                            raise Ext3ReadError(
                                "JBD2 revoke target is duplicated in transaction"
                            )
                        revokes.add(target)
                    records_before_control += 1
                    continue
                if control_type != _JBD2_COMMIT:
                    raise Ext3ReadError("unsupported JBD2 replay control record")
                if set(target for target, _data in writes) & revokes:
                    raise Ext3ReadError(
                        "JBD2 transaction both writes and revokes the same block"
                    )
                transaction_blocks = records_before_control + 1
                if transaction_blocks > remaining:
                    raise Ext3ReadError(
                        "JBD2 replay transaction exceeds journal bounds"
                    )
                break

            replayed_transactions.append((writes, revokes))
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
        overlay = {}
        for writes, revokes in replayed_transactions:
            for target in revokes:
                overlay.pop(target, None)
            for target, data in writes:
                overlay[target] = data
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
        if profile.feature_ro_compat & _RO_COMPAT_BIGALLOC:
            raise Ext3ReadError("ext4 bigalloc cluster layout is not supported")
        if (
            profile.feature_ro_compat & _RO_COMPAT_EXTRA_ISIZE
            and profile.inode_size < 132
        ):
            raise Ext3ReadError("ext4 EXTRA_ISIZE requires extended inode records")
        if profile.feature_compat & (
            _COMPAT_FAST_COMMIT | _COMPAT_ORPHAN_FILE | _COMPAT_SPARSE_SUPER2
        ):
            raise Ext3ReadError(
                "ext4 fast-commit/orphan/sparse-super2 features are not supported"
            )
        NativeExt3Image._validate_profile(profile)

    def _read_inode(self, number: int) -> _Inode:
        """Read an inode with an explicit allowlist and checked extent layout."""
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
        if self.feature_ro_compat & _RO_COMPAT_METADATA_CSUM:
            self._validate_inode_checksum(number, raw)
        mode = struct.unpack_from("<H", raw, 0)[0]
        if mode == 0:
            raise Ext3ReadError("allocated inode has no valid file mode")
        flags = struct.unpack_from("<I", raw, 32)[0]
        unsupported_flags = flags & ~_EXT4_INODE_READ_FLAGS
        if unsupported_flags:
            raise Ext3ReadError(
                f"inode uses unsupported or unknown inode flags: 0x{unsupported_flags:08x}",
                RefusalCode.UNSUPPORTED_PROFILE,
            )
        if mode & 0xF000 == 0x4000 and flags & _EXT2_INDEX_FL:
            raise Ext3ReadError(
                "indexed directories are not supported by this profile",
                RefusalCode.UNSUPPORTED_PROFILE,
            )
        if flags & _EXT4_EXTENTS_FL:
            if self.profile.feature_incompat & 0x0040 == 0:
                raise Ext3ReadError("inode extent flag lacks EXTENTS feature")
        elif self.profile.feature_incompat & 0x0040:
            raise Ext3ReadError(
                "ext4 EXTENTS profile contains a pointer-layout inode",
                RefusalCode.UNSUPPORTED_PROFILE,
            )
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
        uid, gid = self._decode_inode_ownership(raw)
        inode = _Inode(
            number=number,
            mode=mode,
            size=size,
            flags=flags,
            mtime=struct.unpack_from("<I", raw, 16)[0],
            block_pointers=pointers,
            uid=uid,
            gid=gid,
            atime=struct.unpack_from("<I", raw, 8)[0],
            ctime=struct.unpack_from("<I", raw, 12)[0],
            links_count=struct.unpack_from("<H", raw, 26)[0],
            generation=struct.unpack_from("<I", raw, 100)[0],
            **self._inode_timestamp_extras(raw),
        )
        if len(self._inode_cache) >= 4096:
            self._inode_cache.clear()
        self._inode_cache[number] = inode
        return inode

    def _checksum_seed(self) -> int:
        from mountfile.ext_checksum import crc32c_raw

        return crc32c_raw(self.profile.filesystem_uuid)

    def _validate_inode_checksum(self, number: int, raw: bytes) -> None:
        from mountfile.ext_checksum import crc32c_raw

        if len(raw) < 128:
            raise Ext3ReadError("inode is too short for metadata checksum")
        candidate = bytearray(raw)
        expected = struct.unpack_from("<H", raw, 0x7C)[0]
        struct.pack_into("<H", candidate, 0x7C, 0)
        expected_high = None
        if len(raw) >= 0x84:
            expected_high = struct.unpack_from("<H", raw, 0x82)[0]
            struct.pack_into("<H", candidate, 0x82, 0)
        generation = struct.unpack_from("<I", raw, 100)[0]
        seed = crc32c_raw(struct.pack("<II", number, generation), self._checksum_seed())
        actual = crc32c_raw(bytes(candidate), seed)
        if (actual & 0xFFFF) != expected or (
            expected_high is not None and actual >> 16 != expected_high
        ):
            raise Ext3ReadError(f"inode {number} checksum mismatch")

    def _directory_block_data_end(
        self, inode: _Inode, logical: int, block: bytes
    ) -> int:
        from mountfile.ext_checksum import crc32c_raw

        if not self.feature_ro_compat & _RO_COMPAT_METADATA_CSUM:
            return super()._directory_block_data_end(inode, logical, block)
        tail = self.block_size - 12
        if tail < 0:
            raise Ext3ReadError("directory block is too small for checksum tail")
        reserved, record_length, name_length, file_type = struct.unpack_from(
            "<I H B B", block, tail
        )
        if reserved or record_length != 12 or name_length or file_type != 0xDE:
            raise Ext3ReadError("directory checksum tail is missing or malformed")
        expected = struct.unpack_from("<I", block, self.block_size - 4)[0]
        seed = crc32c_raw(
            struct.pack("<II", inode.number, inode.generation),
            self._checksum_seed(),
        )
        if crc32c_raw(block[:tail], seed) != expected:
            raise Ext3ReadError(f"directory block {logical} checksum mismatch")
        return tail

    def _group_descriptor(self, group: int) -> _GroupDescriptor:
        """Decode 64-bit descriptors with exact bounded metadata ranges."""
        if not self.feature_incompat & _INCOMPAT_64BIT:
            result = super()._group_descriptor(group)
            if self.feature_ro_compat & _RO_COMPAT_METADATA_CSUM:
                raw = self._read_at(
                    self._gdt_offset + group * self.group_descriptor_size,
                    self.group_descriptor_size,
                )
                self._validate_group_checksum(group, raw)
            return result
        cached = self._group_cache.get(group)
        if cached is not None:
            return cached
        if not 0 <= group < self.group_count:
            raise Ext3ReadError("inode or block references an invalid group")
        raw = self._read_at(
            self._gdt_offset + group * self.group_descriptor_size,
            self.group_descriptor_size,
        )
        if self.feature_ro_compat & _RO_COMPAT_METADATA_CSUM:
            self._validate_group_checksum(group, raw)
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
        if self.feature_incompat & _INCOMPAT_FLEX_BG:
            lower = self.first_data_block
            upper = self.blocks_count
            if any(start < lower or stop > upper for start, stop in ranges):
                raise Ext3ReadError(
                    "flex_bg group descriptor metadata points outside the filesystem"
                )
        elif any(start < first_block or stop > end_block for start, stop in ranges):
            raise Ext3ReadError("64-bit group descriptor points outside its group")
        if any(stop <= start for start, stop in ranges):
            raise Ext3ReadError("64-bit group descriptor metadata range overflows")
        for index, (left_start, left_stop) in enumerate(ranges):
            for right_start, right_stop in ranges[index + 1 :]:
                if left_start < right_stop and right_start < left_stop:
                    raise Ext3ReadError(
                        "64-bit group descriptor metadata ranges overlap"
                    )
        if self.feature_incompat & _INCOMPAT_FLEX_BG:
            # With flex_bg the group metadata is deliberately relocated, so the
            # filesystem-wide bounds check above is the applicable guard.
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

    def _validate_group_checksum(self, group: int, raw: bytes) -> None:
        from mountfile.ext_checksum import crc32c_raw

        candidate = bytearray(raw)
        struct.pack_into("<H", candidate, 30, 0)
        actual = crc32c_raw(
            struct.pack("<I", group) + bytes(candidate), self._checksum_seed()
        )
        if (actual & 0xFFFF) != struct.unpack_from("<H", raw, 30)[0]:
            raise Ext3ReadError(f"group descriptor {group} checksum mismatch")

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
        # Every extent-index block reached during this walk. A data extent must
        # never alias one of them: doing so would hand extent-tree metadata to
        # the caller as file content.
        tree_blocks: set[int] = set()
        expected_depth = None
        expected_lower = None
        expected_upper = None
        for _current_depth in range(_MAX_EXTENT_TREE_DEPTH + 1):
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
                    if tree_blocks and not tree_blocks.isdisjoint(
                        range(physical_start, physical_start + length)
                    ):
                        raise Ext3ReadError(
                            "extent data range aliases an extent-tree block"
                        )
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
                tree_blocks.add(child)
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
                self._validate_data_block_run(physical, run_blocks)
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
                self._validate_data_block_run(physical, run_blocks)
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
