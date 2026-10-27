# SPDX-License-Identifier: MIT
"""Regression tests for the dependency-free/offline runtime defaults."""

import pytest

from mountfile import vfs


def test_unknown_filesystem_is_offline_default_deny(tmp_path, monkeypatch):
    image = tmp_path / "unknown.img"
    image.write_bytes(b"not a filesystem")
    monkeypatch.delenv("MOUNTFILE_ALLOW_LEGACY_TSK", raising=False)
    monkeypatch.setattr(
        vfs, "TskImage", lambda *args, **kwargs: pytest.fail("legacy fallback")
    )

    with pytest.raises(vfs.RecognizedFilesystemError, match="offline providers"):
        vfs.open_image_provider(image)


def test_unknown_filesystem_can_explicitly_opt_into_legacy_tsk(tmp_path, monkeypatch):
    image = tmp_path / "unknown.img"
    image.write_bytes(b"not a filesystem")
    sentinel = object()
    monkeypatch.setattr(vfs, "TskImage", lambda *args, **kwargs: sentinel)

    assert vfs.open_image_provider(image, allow_legacy_fallback=True) is sentinel


def test_legacy_tsk_environment_switch_is_explicit(tmp_path, monkeypatch):
    image = tmp_path / "unknown.img"
    image.write_bytes(b"not a filesystem")
    sentinel = object()
    monkeypatch.setattr(vfs, "TskImage", lambda *args, **kwargs: sentinel)
    monkeypatch.setenv("MOUNTFILE_ALLOW_LEGACY_TSK", "1")

    assert vfs.open_image_provider(image) is sentinel
