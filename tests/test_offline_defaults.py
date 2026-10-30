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

    with pytest.raises(vfs.LegacyFallbackAvailableError, match="offline providers"):
        vfs.open_image_provider(image)


def test_unknown_filesystem_can_explicitly_opt_into_legacy_tsk(tmp_path, monkeypatch):
    image = tmp_path / "unknown.img"
    image.write_bytes(b"not a filesystem")
    sentinel = object()
    monkeypatch.setattr(vfs, "TskImage", lambda *args, **kwargs: sentinel)

    assert vfs.open_image_provider(image, allow_legacy_fallback=True) is sentinel


def test_recognized_unsafe_profile_is_not_eligible_for_gui_fallback(
    tmp_path, monkeypatch
):
    """The GUI fallback offer applies only to unknown formats, not rejected profiles."""
    from types import SimpleNamespace

    image = tmp_path / "dirty.ext2"
    image.write_bytes(b"synthetic")
    monkeypatch.setattr(vfs, "_has_ext_superblock_magic", lambda *_args: True)
    monkeypatch.setattr(
        vfs,
        "probe_ext_superblock",
        lambda *_args, **_kwargs: SimpleNamespace(
            fs_type="ext2", clean_unmounted=False, journal_recovery_needed=False
        ),
    )
    monkeypatch.setattr(
        vfs, "TskImage", lambda *args, **kwargs: pytest.fail("must not fallback")
    )

    with pytest.raises(vfs.RecognizedFilesystemError) as error:
        vfs.open_image_provider(image, volume_size=9)
    assert not isinstance(error.value, vfs.LegacyFallbackAvailableError)


def test_legacy_tsk_environment_switch_is_explicit(tmp_path, monkeypatch):
    image = tmp_path / "unknown.img"
    image.write_bytes(b"not a filesystem")
    sentinel = object()
    monkeypatch.setattr(vfs, "TskImage", lambda *args, **kwargs: sentinel)
    monkeypatch.setenv("MOUNTFILE_ALLOW_LEGACY_TSK", "1")

    assert vfs.open_image_provider(image) is sentinel
