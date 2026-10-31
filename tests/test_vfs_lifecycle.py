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
