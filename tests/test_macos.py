"""Focused tests for the optional macOS filesystem adapters."""

from pathlib import Path

import pytest

import mountfile.macos as macos


def _volume(tmp_path: Path, marker: bytes, offset: int = 0) -> Path:
    image = tmp_path / "volume.img"
    data = bytearray(offset + 2048)
    if marker in (macos.APFS_CONTAINER_SIGNATURE, macos.APFS_VOLUME_SIGNATURE):
        data[offset + 32 : offset + 36] = marker
    else:
        data[offset + 1024 : offset + 1026] = marker
    image.write_bytes(data)
    return image


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (macos.HFS_PLUS_SIGNATURE, "HFS+"),
        (macos.HFSX_SIGNATURE, "HFS+"),
        (macos.APFS_CONTAINER_SIGNATURE, "APFS"),
        (macos.APFS_VOLUME_SIGNATURE, "APFS"),
    ],
)
def test_detect_macos_filesystem_signatures(tmp_path, marker, expected):
    assert macos.detect_macos_filesystem(_volume(tmp_path, marker)) == expected


def test_detection_honors_filesystem_offset(tmp_path):
    image = _volume(tmp_path, macos.HFS_PLUS_SIGNATURE, offset=4096)

    assert macos.detect_macos_filesystem(image, offset=4096) == "HFS+"
    assert macos.detect_macos_filesystem(image) is None


def test_hfs_requires_optional_pytsk3(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.HFS_PLUS_SIGNATURE)
    monkeypatch.setattr(macos, "pytsk3", None)

    with pytest.raises(macos.AdapterUnavailableError, match="pytsk3"):
        macos.HFSPlusAdapter(image)


def test_apfs_missing_tool_is_explicit_and_does_not_mount(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.APFS_CONTAINER_SIGNATURE)
    calls = []
    monkeypatch.setattr(macos.shutil, "which", lambda name: None)
    monkeypatch.setattr(macos.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(macos.AdapterUnavailableError, match="apfs-fuse"):
        macos.APFSFuseAdapter(image)

    assert calls == []


def test_apfs_constructor_never_mounts(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.APFS_CONTAINER_SIGNATURE)
    calls = []
    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/apfs-fuse")
    monkeypatch.setattr(
        macos.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    adapter = macos.APFSFuseAdapter(image)

    assert adapter.mounted is False
    assert calls == []
