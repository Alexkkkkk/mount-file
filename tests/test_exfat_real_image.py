"""Integration checks against images formatted by the independent exfatprogs."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys

import pytest

from mountfile.exfat_reader import NativeExfatImage
from mountfile.exfat_writer import NativeExfatWriter


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the real-image consistency test uses Linux exfatprogs tools",
)
def test_real_exfat_unicode_write_and_rename_pass_read_only_fsck(tmp_path):
    mkfs = shutil.which("mkfs.exfat")
    fsck = shutil.which("fsck.exfat")
    if not mkfs or not fsck:
        pytest.skip("install exfatprogs to run the real-image consistency test")

    image_size = 64 * 1024 * 1024
    image = tmp_path / "issue-182-real-exfat.img"
    with image.open("wb") as stream:
        stream.truncate(image_size)
    subprocess.run(
        [mkfs, "-L", "ISSUE182", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    reader = NativeExfatImage(image, volume_size=image_size)
    try:
        assert reader.list_dir("/") == []
    finally:
        reader.close()

    payload = bytes(range(256)) * 23 + b"cluster-boundary"
    writer = NativeExfatWriter(image, volume_size=image_size)
    writer.write_file("/Report-é.txt", payload)
    writer.rename("/rEPORT-É.txt", "/résumé-é.txt")

    reader = NativeExfatImage(image, volume_size=image_size)
    try:
        assert reader.file_size("/RÉSUMÉ-é.txt") == len(payload)
        assert reader.read_file("/résumé-é.txt") == payload
        assert reader.read_file_range("/RÉSUMÉ-é.txt", 4090, 37) == payload[4090:4127]
        assert reader.list_dir("/") == [
            {
                "name": "résumé-é.txt",
                "is_dir": False,
                "size": len(payload),
                "date": "",
            }
        ]
    finally:
        reader.close()

    # The independent checker is strictly read-only and sees only a copy.
    checker_copy = tmp_path / "checker-copy.img"
    shutil.copyfile(image, checker_copy)
    before = hashlib.sha256(checker_copy.read_bytes()).digest()
    result = subprocess.run(
        [fsck, "-n", str(checker_copy)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    after = hashlib.sha256(checker_copy.read_bytes()).digest()

    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after, "fsck.exfat -n unexpectedly modified its disposable copy"
