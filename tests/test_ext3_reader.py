# SPDX-License-Identifier: MIT
"""Regression tests for conservative ext3/ext4 read-only routing."""

import shutil
import subprocess

import pytest

from mountfile import vfs
from mountfile.ext3_reader import Ext3ReadError, NativeExt3Image, NativeExt4Image
from tests.test_ext2_reader import BLOCKS_COUNT, _make_image


def test_journaled_profile_rejects_without_a_valid_jbd2_journal(tmp_path):
    image_path = tmp_path / "journaled.img"
    _make_image(image_path, compat=0x000C)
    with pytest.raises(Ext3ReadError, match="journal inode|JBD2"):
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
