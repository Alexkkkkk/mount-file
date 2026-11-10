import builtins
import os
import sys
from types import SimpleNamespace

import pytest

from mountfile.universal_fs_rw import LibguestfsRW


class _FakeGuestFS:
    def __init__(self, **_kwargs):
        self.closed = False

    def add_drive_opts(self, *_args, **_kwargs):
        return None

    def launch(self):
        return None

    def list_devices(self):
        return ["/dev/sda"]

    def mount_options(self, *_args):
        return None

    def umount_all(self):
        return None

    def sync(self):
        return None

    def shutdown(self):
        return None

    def close(self):
        self.closed = True


def _install_fake_guestfs(monkeypatch):
    monkeypatch.setitem(sys.modules, "guestfs", SimpleNamespace(GuestFS=_FakeGuestFS))


@pytest.mark.skipif(os.name != "posix", reason="libguestfs image lock uses POSIX flock")
def test_libguestfs_writer_serializes_image_access(tmp_path, monkeypatch):
    _install_fake_guestfs(monkeypatch)
    image = tmp_path / "ext4.img"
    image.write_bytes(b"image")

    first = LibguestfsRW(image, fs_type="ext4")
    try:
        with pytest.raises(RuntimeError, match="already locked"):
            LibguestfsRW(image, fs_type="ntfs")
    finally:
        first.close()

    second = LibguestfsRW(image, fs_type="ntfs")
    second.close()


@pytest.mark.skipif(os.name != "posix", reason="libguestfs image lock uses POSIX flock")
def test_libguestfs_import_failure_does_not_leave_lock(tmp_path, monkeypatch):
    image = tmp_path / "ext4.img"
    image.write_bytes(b"image")
    monkeypatch.delitem(sys.modules, "guestfs", raising=False)

    real_import = builtins.__import__

    def missing_guestfs(name, *args, **kwargs):
        if name == "guestfs":
            raise ImportError("missing guestfs")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_guestfs)
    with pytest.raises(RuntimeError, match="python3-libguestfs"):
        LibguestfsRW(image, fs_type="ext4")

    monkeypatch.setattr(builtins, "__import__", real_import)
    _install_fake_guestfs(monkeypatch)
    backend = LibguestfsRW(image, fs_type="ext4")
    backend.close()


@pytest.mark.skipif(os.name == "posix", reason="POSIX hosts support libguestfs flock")
def test_libguestfs_writer_rejects_non_posix_host(tmp_path):
    image = tmp_path / "ext4.img"
    image.write_bytes(b"image")

    with pytest.raises(RuntimeError, match="requires a POSIX host"):
        LibguestfsRW(image, fs_type="ext4")

    assert not image.with_name(image.name + ".mountfile.lock").exists()
