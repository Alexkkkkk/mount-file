# SPDX-License-Identifier: MIT
"""Regression tests for the optional transactional ext debugfs writer."""
import shutil
import subprocess

import pytest

from mountfile.ext_debugfs_writer import (
    Ext234ImageWriter,
    Ext234Provider,
    ExtWriteError,
)
from mountfile.safety import RefusalCode
from mountfile.universal_fs_rw import Ext234RW

DEBUGFS = shutil.which("debugfs")
MKE2FS = shutil.which("mke2fs")
pytestmark = pytest.mark.skipif(
    not (DEBUGFS and MKE2FS), reason="e2fsprogs debugfs and mke2fs are required"
)


def _make_ext4(path, size=16 * 1024 * 1024):
    with open(path, "wb") as stream:
        stream.truncate(size)
    subprocess.run([MKE2FS, "-q", "-F", "-t", "ext4", str(path)], check=True)


def _make_ext2(path, size=16 * 1024 * 1024):
    with open(path, "wb") as stream:
        stream.truncate(size)
    subprocess.run([MKE2FS, "-q", "-F", "-t", "ext2", str(path)], check=True)


def _force_legacy_backend(monkeypatch):
    monkeypatch.setattr(Ext234ImageWriter, "_try_native_ext2", lambda _self: False)


def test_ext2_legacy_create_overwrite_and_verify_at_nonzero_offset(
    tmp_path, monkeypatch
):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition)
    _force_legacy_backend(monkeypatch)
    prefix = b"IMAGE-HEADER" * 341
    offset = len(prefix)
    suffix = b"NEXT-PARTITION-SENTINEL"
    image = tmp_path / "disk.img"
    with image.open("wb") as target, partition.open("rb") as source:
        target.write(prefix)
        shutil.copyfileobj(source, target)
        target.write(suffix)

    writer = Ext234ImageWriter(
        str(image),
        offset=offset,
        volume_size=partition.stat().st_size,
        allow_legacy_debugfs=True,
    )
    writer.mkdir("/config")
    writer.write_file("/config/factorySrv.ini", b"UARTOnOff=0\r\n")
    assert writer.read_file("/config/factorySrv.ini") == b"UARTOnOff=0\r\n"
    writer.write_file("/config/factorySrv.ini", b"UARTOnOff=1\r\n")
    assert writer.read_file("/config/factorySrv.ini") == b"UARTOnOff=1\r\n"
    provider = Ext234Provider(
        str(image),
        offset=offset,
        volume_size=partition.stat().st_size,
        allow_legacy_debugfs=True,
    )
    listing = provider.list_dir("/config")
    assert listing == [
        {"name": "factorySrv.ini", "is_dir": False, "size": 13, "date": ""}
    ]
    assert provider.read_file("/config/factorySrv.ini") == b"UARTOnOff=1\r\n"

    with image.open("rb") as stream:
        assert stream.read(offset) == prefix
        stream.seek(offset + writer.filesystem_size)
        assert stream.read() == suffix


def test_invalid_image_paths_never_change_image(tmp_path, monkeypatch):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    _force_legacy_backend(monkeypatch)
    writer = Ext234ImageWriter(str(image), allow_legacy_debugfs=True)
    before = image.read_bytes()

    with pytest.raises(ExtWriteError, match="simple ASCII path"):
        writer.write_file("/config/../escape", b"nope")

    assert image.read_bytes() == before


def test_superblock_size_must_fit_partition_bound(tmp_path):
    image = tmp_path / "filesystem.ext4"
    _make_ext4(image)
    with pytest.raises(ExtWriteError, match="selected partition bounds"):
        Ext234ImageWriter(str(image), volume_size=1024, allow_legacy_debugfs=True)


def test_unsupported_ext_profile_does_not_auto_enable_debugfs(tmp_path, monkeypatch):
    image = tmp_path / "filesystem.ext4"
    _make_ext4(image)
    before = image.read_bytes()
    monkeypatch.setattr(
        "mountfile.ext_debugfs_writer._find_debugfs",
        lambda *_args, **_kwargs: pytest.fail("debugfs discovery must be explicit"),
    )
    writer = Ext234ImageWriter(str(image))
    assert not writer.writable
    assert writer.write_strategy == "read-only-unsupported"
    with pytest.raises(ExtWriteError, match="legacy debugfs compatibility is disabled"):
        writer.write_file("/blocked.txt", b"no mutation")
    assert image.read_bytes() == before


def test_failure_before_commit_keeps_original_image_unchanged(tmp_path, monkeypatch):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    _force_legacy_backend(monkeypatch)
    writer = Ext234ImageWriter(str(image), allow_legacy_debugfs=True)
    before = image.read_bytes()

    def fail_commit(_working):
        raise OSError("simulated image commit failure")

    monkeypatch.setattr(writer, "_commit_extent", fail_commit)
    with pytest.raises(OSError, match="simulated image commit failure"):
        writer.write_file("/new.ini", b"value=1\n")

    assert image.read_bytes() == before


def test_free_space_and_delete_round_trip(tmp_path, monkeypatch):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    _force_legacy_backend(monkeypatch)
    writer = Ext234ImageWriter(str(image), allow_legacy_debugfs=True)
    assert writer.free_space() is None or writer.free_space() > 0

    writer.mkdir("/keep")
    writer.write_file("/keep/one.ini", b"a=1\n")
    writer.write_file("/keep/two.ini", b"b=2\n")

    with pytest.raises(ExtWriteError, match="not empty"):
        writer.delete("/keep")
    with pytest.raises(ExtWriteError, match="root"):
        writer.delete("/")

    writer.delete("/keep", recursive=True)
    # mke2fs always creates /lost+found, so only the created entries disappear.
    assert [entry["name"] for entry in writer.list_dir("/") if entry["is_dir"]] == [
        "lost+found"
    ]

    writer.write_file("/solo.ini", b"c=3\n")
    writer.delete("/solo.ini")
    names = [entry["name"] for entry in writer.list_dir("/") if not entry["is_dir"]]
    assert names == []


@pytest.mark.parametrize("filesystem", ["ext3", "ext4"])
@pytest.mark.parametrize("entrypoint", ["image_writer", "provider", "rw_facade"])
def test_journaled_ext_legacy_writer_refuses_before_debugfs_discovery(
    tmp_path, monkeypatch, filesystem, entrypoint
):
    image = tmp_path / f"filesystem.{filesystem}"
    with image.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run([MKE2FS, "-q", "-F", "-t", filesystem, str(image)], check=True)
    before = image.read_bytes()
    monkeypatch.setattr(
        "mountfile.ext_debugfs_writer._find_debugfs",
        lambda *_args, **_kwargs: pytest.fail("debugfs must not be discovered"),
    )
    with pytest.raises(RuntimeError, match="refused for journaled ext3/ext4") as error:
        if entrypoint == "image_writer":
            Ext234ImageWriter(str(image), allow_legacy_debugfs=True)
        elif entrypoint == "provider":
            Ext234Provider(str(image), allow_legacy_debugfs=True)
        else:
            Ext234RW(image, allow_legacy_debugfs=True)

    assert error.value.code is RefusalCode.UNSUPPORTED_PROFILE
    assert image.read_bytes() == before
