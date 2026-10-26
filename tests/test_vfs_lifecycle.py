# SPDX-License-Identifier: MIT
"""Lifecycle regressions for optional pytsk3-backed VFS handles."""

from types import SimpleNamespace

import pytest

import mountfile.vfs as vfs


def test_tsk_image_closes_image_handle_when_filesystem_open_fails(monkeypatch):
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
        vfs.TskImage("dummy.img")

    assert image_handle.close_calls == 1
