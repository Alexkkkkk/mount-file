# SPDX-License-Identifier: MIT
"""Regression tests for conservative ext3/ext4 read-only routing."""

import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

from mountfile import vfs
from mountfile.ext2_reader import _EXT2_COMPR_FL, _EXT2_INDEX_FL, _Inode
from mountfile.ext3_reader import Ext3ReadError, NativeExt3Image, NativeExt4Image
from tests.test_ext2_reader import BLOCKS_COUNT, _make_image


def _make_clean_journal_image(
    path, *, descriptor_sequence=7, commit_sequence=7, truncate_commit=False
):
    _make_image(path, compat=0x000C)
    image = bytearray(path.read_bytes())
    sb = memoryview(image)[1024:2048]
    filesystem_uuid = b"0123456789abcdef"
    sb[0x68:0x78] = filesystem_uuid
    # inode 8: a five-block internal journal at blocks 40..44. The final
    # zero block is the terminator for the non-wrapping replay range.
    inode = 5 * 1024 + 7 * 128
    struct.pack_into("<H", image, inode, 0x8000 | 0o600)
    struct.pack_into("<I", image, inode + 4, 5 * 1024)
    struct.pack_into("<5I", image, inode + 40, 40, 41, 42, 43, 44)

    journal = memoryview(image)[40 * 1024 : 45 * 1024]
    struct.pack_into(">III", journal, 0, 0xC03B3998, 3, 7)
    struct.pack_into(">II", journal, 12, 1024, 5)
    struct.pack_into(">II", journal, 20, 1, 0)
    journal[48:64] = filesystem_uuid
    # One classic descriptor tag, one data block, and its matching commit.
    struct.pack_into(">III", journal, 1024, 0xC03B3998, 1, descriptor_sequence)
    struct.pack_into(">II", journal, 1036, 21, 0x0000000A)  # LAST|SAME_UUID
    struct.pack_into(">III", journal, 3072, 0xC03B3998, 2, commit_sequence)
    if truncate_commit:
        struct.pack_into(">I", image, 43 * 1024 + 8, 0)
    path.write_bytes(image)


def test_journaled_profile_rejects_without_a_valid_jbd2_journal(tmp_path):
    image_path = tmp_path / "journaled.img"
    _make_image(image_path, compat=0x000C)
    with pytest.raises(Ext3ReadError, match="journal inode|JBD2"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_clean_journal_with_bounded_transaction_is_readable(tmp_path):
    image_path = tmp_path / "clean-journal.img"
    _make_clean_journal_image(image_path)
    provider = NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)
    try:
        assert provider.backend_name == "native-ext3-ro"
    finally:
        provider.close()


def test_journal_commit_sequence_mismatch_fails_closed(tmp_path):
    image_path = tmp_path / "mismatched-journal.img"
    _make_clean_journal_image(image_path, commit_sequence=8)
    with pytest.raises(Ext3ReadError, match="sequence/type mismatch"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_truncated_journal_record_fails_closed(tmp_path):
    image_path = tmp_path / "truncated-journal.img"
    _make_clean_journal_image(image_path, truncate_commit=True)
    with pytest.raises(Ext3ReadError, match="journal|JBD2|bounds|short"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_journal_with_nonzero_start_is_recovery_required(tmp_path):
    image_path = tmp_path / "active-journal.img"
    _make_clean_journal_image(image_path)
    image = bytearray(image_path.read_bytes())
    struct.pack_into(">I", image, 40 * 1024 + 24, 1)
    image_path.write_bytes(image)
    with pytest.raises(Ext3ReadError, match="uncheckpointed transaction"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_explicit_journal_replay_uses_read_only_overlay(tmp_path):
    image_path = tmp_path / "replay-journal.img"
    _make_clean_journal_image(image_path)
    image = bytearray(image_path.read_bytes())
    payload = bytes([0xA7]) * 1024
    image[40 * 1024 + 2 * 1024 : 40 * 1024 + 3 * 1024] = payload
    struct.pack_into(">I", image, 40 * 1024 + 24, 1)
    image_path.write_bytes(image)

    provider = NativeExt3Image(
        image_path,
        volume_size=BLOCKS_COUNT * 1024,
        replay_journal=True,
    )
    try:
        assert provider.journal_replayed is True
        assert provider._read_at(21 * 1024, 1024) == payload
        assert image_path.read_bytes()[21 * 1024 : 22 * 1024] != payload
    finally:
        provider.close()


def test_active_journal_replay_rejects_empty_first_descriptor(tmp_path):
    image_path = tmp_path / "empty-replay-descriptor.img"
    _make_clean_journal_image(image_path)
    image = bytearray(image_path.read_bytes())
    struct.pack_into(">I", image, 40 * 1024 + 24, 1)
    image[41 * 1024 : 42 * 1024] = bytes(1024)
    image_path.write_bytes(image)

    with pytest.raises(Ext3ReadError, match="first JBD2 replay descriptor is empty"):
        NativeExt3Image(
            image_path,
            volume_size=BLOCKS_COUNT * 1024,
            replay_journal=True,
        )


def test_journal_replay_empty_first_descriptor_preserves_existing_overlay():
    reader = NativeExt3Image.__new__(NativeExt3Image)
    reader.block_size = 1024
    reader.blocks_count = 128
    reader._journal_overlay = {99: b"previous-state"}
    reader._journal_logical_block = lambda _inode, _logical, _count: bytes(1024)

    with pytest.raises(Ext3ReadError, match="first JBD2 replay descriptor is empty"):
        reader._replay_journal(
            object(), start=1, max_length=7, journal_blocks=8, expected_sequence=7
        )

    assert reader._journal_overlay == {99: b"previous-state"}


def test_journal_replay_requires_terminator_before_nonwrapping_log_end(tmp_path):
    image_path = tmp_path / "unterminated-replay-range.img"
    _make_clean_journal_image(image_path)
    image = bytearray(image_path.read_bytes())
    struct.pack_into(">I", image, 40 * 1024 + 16, 4)
    struct.pack_into(">I", image, 40 * 1024 + 24, 1)
    image_path.write_bytes(image)

    with pytest.raises(Ext3ReadError, match="end of non-wrapping log without terminator"):
        NativeExt3Image(
            image_path,
            volume_size=BLOCKS_COUNT * 1024,
            replay_journal=True,
        )


def test_journal_replay_rejects_sequence_not_advertised_by_superblock(tmp_path):
    image_path = tmp_path / "stale-replay-sequence.img"
    _make_clean_journal_image(image_path, descriptor_sequence=99, commit_sequence=99)
    image = bytearray(image_path.read_bytes())
    struct.pack_into(">I", image, 40 * 1024 + 24, 1)
    image_path.write_bytes(image)

    with pytest.raises(Ext3ReadError, match="sequence disagrees with superblock"):
        NativeExt3Image(
            image_path,
            volume_size=BLOCKS_COUNT * 1024,
            replay_journal=True,
        )


def test_journal_replay_does_not_publish_partial_overlay_on_late_failure():
    reader = NativeExt3Image.__new__(NativeExt3Image)
    reader.block_size = 1024
    reader.blocks_count = 128
    reader.profile = SimpleNamespace(filesystem_uuid=b"0123456789abcdef")
    reader._journal_overlay = {99: b"previous-state"}

    def descriptor(sequence, target):
        block = bytearray(1024)
        struct.pack_into(">III", block, 0, 0xC03B3998, 1, sequence)
        struct.pack_into(">II", block, 12, target, 0x0000000A)
        return bytes(block)

    def commit(sequence):
        block = bytearray(1024)
        struct.pack_into(">III", block, 0, 0xC03B3998, 2, sequence)
        return bytes(block)

    blocks = {
        1: descriptor(7, 21),
        2: b"A" * 1024,
        3: commit(7),
        4: descriptor(8, 22),
        5: b"B" * 1024,
        6: commit(99),  # Corrupt later commit, after one valid transaction.
    }
    reader._journal_logical_block = lambda _inode, logical, _count: blocks[logical]

    with pytest.raises(Ext3ReadError, match="commit does not match descriptor"):
        reader._replay_journal(
            object(), start=1, max_length=7, journal_blocks=8, expected_sequence=7
        )

    assert reader._journal_overlay == {99: b"previous-state"}


def test_journal_with_unknown_feature_bits_fails_closed(tmp_path):
    image_path = tmp_path / "unsupported-journal-feature.img"
    _make_clean_journal_image(image_path)
    image = bytearray(image_path.read_bytes())
    struct.pack_into(">I", image, 40 * 1024 + 0x24, 1)
    image_path.write_bytes(image)
    with pytest.raises(Ext3ReadError, match="unsupported or unchecked JBD2"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_vfs_does_not_downgrade_rejected_ext3_profile(tmp_path, monkeypatch):
    image_path = tmp_path / "journaled-routing.img"
    _make_image(image_path, compat=0x000C)

    def fail_fallback(*args, **kwargs):
        raise AssertionError("pytsk3 fallback must not open for rejected ext")

    monkeypatch.setattr(vfs, "TskImage", fail_fallback)
    with pytest.raises(vfs.TskError, match="native ext3/ext4 reader declined"):
        vfs.open_image_provider(image_path, offset=0, volume_size=BLOCKS_COUNT * 1024)


def test_dirty_ext_profile_fails_before_any_provider_open(tmp_path, monkeypatch):
    image_path = tmp_path / "dirty-ext.img"
    _make_image(image_path, state=2, compat=0x000C)
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *args, **kwargs: pytest.fail("dirty ext must not use fallback"),
    )
    with pytest.raises(vfs.TskError, match="dirty or requires journal recovery"):
        vfs.open_image_provider(image_path, offset=0, volume_size=BLOCKS_COUNT * 1024)


def test_external_journal_device_is_rejected_explicitly(tmp_path):
    image_path = tmp_path / "external-journal.img"
    _make_image(image_path, compat=0x000C, incompat=0x000A)
    with pytest.raises(Ext3ReadError, match="external JBD2"):
        NativeExt3Image(image_path, volume_size=BLOCKS_COUNT * 1024)


@pytest.mark.parametrize(
    ("field", "bit", "reason"),
    [
        ("feature_incompat", 0x0080, "64-bit/group-descriptor"),
        ("feature_incompat", 0x0200, "flex_bg"),
        ("feature_ro_compat", 0x0200, "bigalloc"),
        ("feature_ro_compat", 0x0400, "metadata checksums"),
    ],
)
def test_ext4_unsupported_layout_profiles_fail_closed(field, bit, reason):
    profile = SimpleNamespace(
        fs_type="ext4",
        unknown_incompat_features=0,
        unknown_ro_compat_features=0,
        feature_incompat=0x0040 if field != "feature_incompat" else bit,
        feature_ro_compat=0x0003 if field != "feature_ro_compat" else 0x0003 | bit,
        feature_compat=0,
    )
    with pytest.raises(Ext3ReadError, match=reason):
        NativeExt4Image._validate_profile(profile)


def test_ext4_metadata_csum_rejection_names_unvalidated_structures():
    profile = SimpleNamespace(
        fs_type="ext4",
        unknown_incompat_features=0,
        unknown_ro_compat_features=0,
        feature_incompat=0x0040,
        feature_ro_compat=0x0003 | 0x0400,
        feature_compat=0,
    )
    with pytest.raises(Ext3ReadError, match="UUID-seeded.*group-descriptor.*extent"):
        NativeExt4Image._validate_profile(profile)


def _ext4_inode_reader(raw, *, block_size=1024):
    reader = NativeExt4Image.__new__(NativeExt4Image)
    reader.inodes_count = 16
    reader.inodes_per_group = 16
    reader.inode_size = len(raw)
    reader.block_size = block_size
    reader.feature_ro_compat = 0x0003
    reader.profile = SimpleNamespace(feature_incompat=0)
    reader._inode_cache = {}
    reader._group_descriptor = lambda _group: SimpleNamespace(inode_table=5)
    reader._inode_is_allocated = lambda _number, _group, _index: True
    reader._read_at = lambda _offset, size: bytes(raw[:size])
    return reader


@pytest.mark.parametrize(
    ("mode", "flags", "reason"),
    [
        (0x8000, _EXT2_COMPR_FL, "unsupported ext4 data layout"),
        (0x4000, _EXT2_INDEX_FL, "indexed directories"),
    ],
)
def test_ext4_rejects_compressed_inodes_and_indexed_directories(mode, flags, reason):
    raw = bytearray(128)
    struct.pack_into("<H", raw, 0, mode)
    struct.pack_into("<I", raw, 32, flags)

    with pytest.raises(Ext3ReadError, match=reason):
        _ext4_inode_reader(raw)._read_inode(2)


def test_ext4_rejects_nonextent_inode_larger_than_classic_pointer_capacity():
    block_size = 1024
    entries = block_size // 4
    capacity = 12 + entries + entries**2 + entries**3
    size = capacity * block_size + 1
    raw = bytearray(128)
    struct.pack_into("<H", raw, 0, 0x8000)
    struct.pack_into("<I", raw, 4, size & 0xFFFFFFFF)
    struct.pack_into("<I", raw, 108, size >> 32)

    with pytest.raises(Ext3ReadError, match="classic block-pointer capacity"):
        _ext4_inode_reader(raw, block_size=block_size)._read_inode(2)


def test_ext4_rejects_extent_whose_full_physical_range_exceeds_filesystem():
    root = bytearray(60)
    struct.pack_into("<HHHHI", root, 0, 0xF30A, 1, 4, 0, 0)
    # logical block 2 maps to the final filesystem block, but the extent has
    # length 2 and therefore its second physical block is out of range.
    struct.pack_into("<IHHI", root, 12, 2, 2, 0, BLOCKS_COUNT - 1)
    inode = _Inode(
        number=12,
        mode=0x8000,
        size=4 * 1024,
        flags=0x00080000,
        mtime=0,
        block_pointers=struct.unpack("<15I", root),
    )
    reader = NativeExt4Image.__new__(NativeExt4Image)
    reader.blocks_count = BLOCKS_COUNT

    with pytest.raises(Ext3ReadError, match="extent physical range is out of bounds"):
        reader._extent_map(inode, 2)


def _extent_inode(root, *, logical_size=8 * 1024):
    root = bytes(root).ljust(60, b"\x00")
    return _Inode(
        number=12,
        mode=0x8000,
        size=logical_size,
        flags=0x00080000,
        mtime=0,
        block_pointers=struct.unpack("<15I", root),
    )


def _extent_reader(blocks_count=64, blocks=None):
    reader = NativeExt4Image.__new__(NativeExt4Image)
    reader.blocks_count = blocks_count
    blocks = blocks or {}
    reader._extent_block = lambda block: blocks[block]
    return reader


def test_ext4_synthetic_extent_index_and_leaf_are_read_only_and_bounded():
    root = bytearray(60)
    struct.pack_into("<HHHHI", root, 0, 0xF30A, 1, 4, 1, 0)
    struct.pack_into("<IIH", root, 12, 2, 40, 0)
    leaf = bytearray(1024)
    struct.pack_into("<HHHH", leaf, 0, 0xF30A, 1, 84, 0)
    struct.pack_into("<IHHI", leaf, 12, 2, 2, 0, 50)

    reader = _extent_reader(blocks={40: bytes(leaf)})
    inode = _extent_inode(root)
    assert reader._extent_map(inode, 2) == (50, False)
    assert reader._extent_map(inode, 3) == (51, False)
    assert reader._extent_map(inode, 0) == (0, False)


@pytest.mark.parametrize(
    ("indexes", "reason"),
    [
        ([(0, 40, 0), (0, 41, 0)], "strictly increasing"),
        ([(0, 40, 1)], "reserved field is nonzero"),
    ],
)
def test_ext4_extent_index_requires_unique_keys_and_zero_reserved_field(
    indexes, reason
):
    root = bytearray(60)
    struct.pack_into("<HHHHI", root, 0, 0xF30A, len(indexes), 4, 1, 0)
    for position, (logical, child, reserved) in enumerate(indexes):
        struct.pack_into("<IIHH", root, 12 + position * 12, logical, child, 0, reserved)

    with pytest.raises(Ext3ReadError, match=reason):
        _extent_reader()._extent_map(_extent_inode(root), 0)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: struct.pack("<HHHHI", 0xF30A, 1, 5, 0, 0) + bytes(40),
        lambda: struct.pack("<HHHHI", 0xF30A, 1, 4, 6, 0) + bytes(40),
        lambda: struct.pack("<HHHHI", 0xF30A, 1, 4, 1, 0)
        + struct.pack("<IIH", 2, 64, 0)
        + bytes(28),
    ],
)
def test_ext4_synthetic_extent_tree_malformed_or_out_of_bounds_fails_closed(builder):
    root = bytearray(builder())
    reader = _extent_reader(blocks={})
    inode = _extent_inode(root)
    with pytest.raises(Ext3ReadError, match="extent|depth|header|bounds"):
        reader._extent_map(inode, 2)


def test_ext4_extent_tree_depth_is_bounded():
    root = bytearray(60)
    struct.pack_into("<HHHHI", root, 0, 0xF30A, 0, 4, 6, 0)
    reader = _extent_reader()
    with pytest.raises(Ext3ReadError, match="depth|header"):
        reader._extent_map(_extent_inode(root), 0)


def test_extent_profile_reads_a_real_clean_ext4_image(tmp_path):
    mke2fs = shutil.which("mke2fs")
    if mke2fs is None:
        pytest.skip("mke2fs is not installed")
    image_path = tmp_path / "extent-ext4.img"
    with image_path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    subprocess.run(
        [
            mke2fs,
            "-q",
            "-F",
            "-t",
            "ext4",
            "-b",
            "1024",
            "-O",
            "extent,^has_journal,^64bit,^metadata_csum,^dir_index,^flex_bg,^huge_file",
            str(image_path),
        ],
        check=True,
    )
    provider = NativeExt4Image(image_path, volume_size=image_path.stat().st_size)
    try:
        assert provider.backend_name == "native-ext4-ro"
        assert any(entry["name"] == "lost+found" for entry in provider.list_dir("/"))
    finally:
        provider.close()
