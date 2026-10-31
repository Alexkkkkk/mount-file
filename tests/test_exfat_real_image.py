"""Integration checks against images formatted by the independent exfatprogs."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys

import pytest

from mountfile.exfat_reader import NativeExfatImage
from mountfile.exfat_writer import NativeExfatWriter


def _assert_read_only_fsck(fsck, image, checker_copy):
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


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the real-image consistency test uses Linux exfatprogs tools",
)
def test_real_exfat_unicode_write_and_rename_pass_read_only_fsck(tmp_path):
    mkfs = shutil.which("mkfs.exfat")
    fsck = shutil.which("fsck.exfat")
    if not mkfs or not fsck:
        pytest.skip("install exfatprogs to run the real-image consistency test")

    image_size = 16 * 1024 * 1024
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
    replacement = b"replacement-after-truncate"
    nested_payload = bytes(reversed(range(256))) * 23 + b"nested-boundary"
    writer = NativeExfatWriter(image, volume_size=image_size)
    writer.write_file("/Report-é.txt", payload)
    writer.write_file("/report-É.TXT", replacement)
    writer.mkdir("/Nested")
    writer.write_file("/Nested/Quarterly-über.txt", nested_payload)
    writer.rename("/Nested/quarterly-ÜBER.txt", "/résumé-final-é.txt")
    writer.delete("/Nested")
    writer.write_file("/empty.bin", b"")

    reader = NativeExfatImage(image, volume_size=image_size)
    try:
        assert reader.file_size("/REPORT-é.TXT") == len(replacement)
        assert reader.read_file("/report-É.txt") == replacement
        assert reader.file_size("/RÉSUMÉ-FINAL-É.TXT") == len(nested_payload)
        assert reader.read_file("/résumé-final-é.txt") == nested_payload
        assert (
            reader.read_file_range("/RÉSUMÉ-FINAL-É.TXT", 4090, 37)
            == nested_payload[4090:4127]
        )
        assert reader.file_size("/empty.bin") == 0
        assert reader.read_file("/empty.bin") == b""
        entries = {entry["name"]: entry for entry in reader.list_dir("/")}
        assert set(entries) == {"Report-é.txt", "résumé-final-é.txt", "empty.bin"}
        assert entries["Report-é.txt"]["size"] == len(replacement)
        assert entries["résumé-final-é.txt"]["size"] == len(nested_payload)
        assert entries["empty.bin"]["size"] == 0
    finally:
        reader.close()

    _assert_read_only_fsck(fsck, image, tmp_path / "checker-copy.img")


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the real-image consistency test uses Linux exfatprogs tools",
)
def test_real_exfat_root_directory_growth_passes_read_only_fsck(tmp_path):
    mkfs = shutil.which("mkfs.exfat")
    fsck = shutil.which("fsck.exfat")
    if not mkfs or not fsck:
        pytest.skip("install exfatprogs to run the real-image consistency test")

    image_size = 2 * 1024 * 1024
    image = tmp_path / "issue-182-root-growth.img"
    with image.open("wb") as stream:
        stream.truncate(image_size)
    subprocess.run(
        [mkfs, "-b", "512", "-c", "512", "-L", "ROOTGROWTH", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    reader = NativeExfatImage(image, volume_size=image_size)
    try:
        assert reader.profile.cluster_size == 512
        assert reader.list_dir("/") == []
    finally:
        reader.close()

    writer = NativeExfatWriter(image, volume_size=image_size)
    for index in range(6):
        writer.write_file(f"/EMPTY-{index:02}.TXT", b"")

    reader = NativeExfatImage(image, volume_size=image_size)
    try:
        assert len(reader._chain(reader.profile.root_cluster, no_fat_chain=False)) > 1
        entries = reader.list_dir("/")
        assert {entry["name"] for entry in entries} == {
            f"EMPTY-{index:02}.TXT" for index in range(6)
        }
        assert all(entry["size"] == 0 for entry in entries)
    finally:
        reader.close()

    _assert_read_only_fsck(fsck, image, tmp_path / "root-growth-checker-copy.img")
