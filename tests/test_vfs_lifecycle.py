# SPDX-License-Identifier: MIT
"""Lifecycle regressions for optional pytsk3-backed VFS handles."""

from types import SimpleNamespace

import pytest

import mountfile.vfs as vfs


def test_tsk_image_closes_image_handle_when_filesystem_open_fails(
    tmp_path, monkeypatch
):
    image_path = tmp_path / "dummy.img"
    image_path.write_bytes(b"whole image")

    class ImageHandle:
        close_calls = 0

        def close(self):
            self.close_calls += 1

    image_handle = ImageHandle()

    def fail_open_filesystem(_image, *, offset):
        assert offset == 0
        raise RuntimeError("invalid filesystem offset")

    fake_tsk = SimpleNamespace(
        Img_Info=lambda _path: image_handle,
        FS_Info=fail_open_filesystem,
    )
    monkeypatch.setattr(vfs, "pytsk3", fake_tsk)

    with pytest.raises(vfs.TskError, match="invalid filesystem offset"):
        vfs.TskImage(image_path)

    assert image_handle.close_calls == 1


def test_failed_reopen_closes_new_image_and_provider_can_retry(tmp_path, monkeypatch):
    image_path = tmp_path / "dummy.img"
    image_path.write_bytes(b"whole image")
    handles = []
    fs_calls = 0

    class ImageHandle:
        def __init__(self):
            self.close_calls = 0
            handles.append(self)

        def close(self):
            self.close_calls += 1

    def open_fs(_image, *, offset):
        nonlocal fs_calls
        assert offset == 0
        fs_calls += 1
        if fs_calls == 2:
            raise RuntimeError("reopen filesystem failed")
        return object()

    monkeypatch.setattr(
        vfs,
        "pytsk3",
        SimpleNamespace(Img_Info=lambda _path: ImageHandle(), FS_Info=open_fs),
    )
    image = vfs.TskImage(image_path)
    image.close()
    with pytest.raises(vfs.TskError, match="reopen filesystem failed"):
        image.reopen()
    assert handles[1].close_calls == 1
    assert image.img is None and image.fs is None
    image.reopen()
    assert image.img is handles[2] and image.fs is not None
    image.close()
    assert all(handle.close_calls == 1 for handle in handles)


@pytest.mark.parametrize("replacement", [b"short", b"longer than the original"])
def test_reopen_revalidates_original_extent_before_opening(
    tmp_path, monkeypatch, replacement
):
    image_path = tmp_path / "dummy.img"
    image_path.write_bytes(b"whole image")
    handles = []
    monkeypatch.setattr(
        vfs,
        "pytsk3",
        SimpleNamespace(
            Img_Info=lambda _path: handles.append(object()) or handles[-1],
            FS_Info=lambda _image, *, offset: object(),
        ),
    )
    image = vfs.TskImage(image_path)
    image.close()
    image_path.write_bytes(replacement)
    with pytest.raises(vfs.TskError, match="extent|constrain"):
        image.reopen()
    assert len(handles) == 1
    assert image.img is None and image.fs is None


def test_tsk_image_refuses_selected_partition_before_opening_pytsk(
    tmp_path, monkeypatch
):
    image_path = tmp_path / "disk.img"
    image_path.write_bytes(b"P" * 4096 + b"selected".ljust(4096, b"\0") + b"S" * 4096)
    open_calls = []
    fake_tsk = SimpleNamespace(
        Img_Info=lambda _path: open_calls.append("image"),
        FS_Info=lambda *_args, **_kwargs: open_calls.append("filesystem"),
    )
    monkeypatch.setattr(vfs, "pytsk3", fake_tsk)

    with pytest.raises(
        vfs.TskError, match="cannot safely constrain a selected partition"
    ):
        vfs.TskImage(image_path, offset=4096, volume_size=4096)

    assert open_calls == []


def test_tsk_image_read_range_has_typed_bounds_and_keeps_eof_clipping():
    class File:
        class Info:
            class Meta:
                size = 4

            meta = Meta()

        info = Info()

        def read_random(self, offset, length):
            return b"abcd"[offset : offset + length]

    image = object.__new__(vfs.TskImage)
    image.fs = type("FS", (), {"open": lambda self, path: File()})()
    assert image.read_file_range("/file", 1, 99) == b"bcd"
    with pytest.raises(vfs.TskError) as caught:
        image.read_file_range("/file", -1, 1)
    assert caught.value.code is vfs.RefusalCode.RANGE_OUT_OF_BOUNDS
