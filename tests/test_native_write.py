from __future__ import annotations

import pytest

from mountfile.native_write import (
    ENABLE_ENV,
    Ext4TransactionalWriter,
    NativeWriteUnsafe,
    NtfsTransactionalWriter,
    validate_internal_path,
)
from mountfile.vfs_drivers.ext4_adapter import Ext4VFS
from mountfile.vfs_drivers.ntfs_adapter import NtfsVFS


def test_native_write_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    image = tmp_path / "image.img"
    image.write_bytes(b"original")

    result = Ext4VFS(str(image)).write("etc/config", b"new")

    assert result.success is False
    assert result.error_code == "WRITE_DISABLED_JOURNAL_UNSAFE"
    assert image.read_bytes() == b"original"
    assert "write" not in {
        operation.value for operation in Ext4VFS(str(image)).supported_operations()
    }


def test_ntfs_write_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    image = tmp_path / "image.img"
    image.write_bytes(b"original")

    result = NtfsVFS(str(image)).write("etc/config", b"new")

    assert result.success is False
    assert result.error_code == "WRITE_DISABLED_LOGFILE_UNSAFE"
    assert image.read_bytes() == b"original"


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "", "a//b", "a/./b", "a/../b"]
)
def test_internal_path_validation_rejects_ambiguous_paths(path):
    with pytest.raises(NativeWriteUnsafe):
        validate_internal_path(path)


def test_ext4_writer_checks_copy_before_atomic_replace(monkeypatch, tmp_path):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = tmp_path / "image.img"
    image.write_bytes(b"original")
    commands = []

    monkeypatch.setattr(
        "mountfile.native_write.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        "mountfile.native_write._run",
        lambda command, **kwargs: commands.append(list(command)),
    )
    Ext4TransactionalWriter().write_file(str(image), "etc/config", b"new")

    assert [command[0] for command in commands] == ["e2fsck", "debugfs", "e2fsck"]
    assert commands[0][1:] == ["-fy", commands[0][-1]]
    assert commands[2][1] == "-fn"
    assert image.read_bytes() == b"original"


def test_ntfs_writer_rejects_partition_offsets_before_touching_image(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = tmp_path / "image.img"
    image.write_bytes(b"original")
    monkeypatch.setattr(
        "mountfile.native_write.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    with pytest.raises(NativeWriteUnsafe, match="offset"):
        NtfsTransactionalWriter().write_file(str(image), "file.txt", b"new", offset=512)

    assert image.read_bytes() == b"original"
