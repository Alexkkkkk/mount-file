# SPDX-License-Identifier: MIT
"""Regression coverage for non-FAT image writes exposed by the package facade."""

import shutil
import subprocess

import pytest

from mountfile.universal_fs_rw import UniversalFSRW

HAVE_EXT_TOOLS = all(shutil.which(name) for name in ("mkfs.ext4", "debugfs"))


@pytest.mark.skipif(not HAVE_EXT_TOOLS, reason="mkfs.ext4/debugfs not available")
def test_ext4_facade_does_not_auto_enable_legacy_debugfs(tmp_path):
    image = tmp_path / "ext4.img"
    with image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True)

    with pytest.raises(RuntimeError, match="explicit legacy compatibility backend"):
        UniversalFSRW(image, auto_backup=False)
