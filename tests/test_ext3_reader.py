# SPDX-License-Identifier: MIT
"""Regression tests for conservative ext3/ext4 read-only routing."""

import pytest

from mountfile import vfs
from mountfile.ext3_reader import Ext3ReadError, NativeExt3Image
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
        vfs.open_image_provider(
            image_path, offset=0, volume_size=BLOCKS_COUNT * 1024
        )


def test_dirty_ext_profile_fails_before_any_provider_open(tmp_path, monkeypatch):
    image_path = tmp_path / "dirty-ext.img"
    _make_image(image_path, state=2, compat=0x000C)
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *args, **kwargs: pytest.fail("dirty ext must not use fallback"),
    )
    with pytest.raises(vfs.TskError, match="dirty or requires journal recovery"):
        vfs.open_image_provider(
            image_path, offset=0, volume_size=BLOCKS_COUNT * 1024
        )
