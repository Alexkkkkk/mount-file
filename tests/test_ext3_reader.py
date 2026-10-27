# SPDX-License-Identifier: MIT
"""Regression tests for conservative ext3/ext4 read-only routing."""

import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

from mountfile import vfs
from mountfile.ext2_reader import _Inode
from mountfile.ext3_reader import Ext3ReadError, NativeExt3Image, NativeExt4Image
from tests.test_ext2_reader import BLOCKS_COUNT, _make_image


def _make_clean_journal_image(path, *, commit_sequence=7, truncate_commit=False):
    _make_image(path, compat=0x000C)
    image = bytearray(path.read_bytes())
    sb = memoryview(image)[1024:2048]
    filesystem_uuid = b"0123456789abcdef"
    sb[0x68:0x78] = filesystem_uuid
    # inode 8: a four-block internal journal at blocks 40..43
    inode = 5 * 1024 + 7 * 128
    struct.pack_into("<H", image, inode, 0x8000 | 0o600)
    struct.pack_into("<I", image, inode + 4, 4 * 1024)
    struct.pack_into("<4I", image, inode + 40, 40, 41, 42, 43)

    journal = memoryview(image)[40 * 1024 : 44 * 1024]
    struct.pack_into(">III", journal, 0, 0xC03B3998, 3, 7)
    struct.pack_into(">II", journal, 12, 1024, 4)
    struct.pack_into(">II", journal, 20, 1, 0)
    journal[48:64] = filesystem_uuid
    # One classic descriptor tag, one data block, and its matching commit.
    struct.pack_into(">III", journal, 1024, 0xC03B3998, 1, 7)
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
