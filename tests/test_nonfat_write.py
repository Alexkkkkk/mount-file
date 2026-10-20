# SPDX-License-Identifier: MIT
"""Regression coverage for non-FAT image writes exposed by the package facade."""

import shutil
import subprocess

import pytest

from mountfile.universal_fs_rw import UniversalFSRW


HAVE_EXT_TOOLS = all(shutil.which(name) for name in ("mkfs.ext4", "debugfs"))


@pytest.mark.skipif(not HAVE_EXT_TOOLS, reason="mkfs.ext4/debugfs not available")
def test_ext4_facade_supports_gui_style_file_lifecycle(tmp_path):
    image = tmp_path / "ext4.img"
    with image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True)

    fs = UniversalFSRW(image, auto_backup=False)
    try:
        assert fs.info().writable is True
        fs.mkdir("/EVIDENCE")
        fs.write_file("/EVIDENCE/NOTE.TXT", b"first evidence")
        assert fs.read("/EVIDENCE/NOTE.TXT") == b"first evidence"
        fs.rename("/EVIDENCE/NOTE.TXT", "/EVIDENCE/RENAMED.TXT")
        assert fs.read("/EVIDENCE/RENAMED.TXT") == b"first evidence"
        fs.delete("/EVIDENCE/RENAMED.TXT")
        assert fs.backend.exists("/EVIDENCE/RENAMED.TXT") is False
    finally:
        fs.close()
