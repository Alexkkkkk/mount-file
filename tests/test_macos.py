"""Focused tests for the optional macOS filesystem adapters."""

from pathlib import Path

import pytest

import mountfile.macos as macos
from mountfile import vfs


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


@pytest.mark.parametrize(
    ("marker", "filesystem"),
    [
        (macos.HFS_PLUS_SIGNATURE, "HFS+"),
        (macos.APFS_CONTAINER_SIGNATURE, "APFS"),
    ],
)
def test_vfs_does_not_silently_fallback_for_macos_filesystems(
    tmp_path, marker, filesystem
):
    image = _volume(tmp_path, marker)

    with pytest.raises(vfs.RecognizedFilesystemError, match=filesystem):
        vfs.open_image_provider(image, volume_size=image.stat().st_size)


def test_hfs_requires_optional_pytsk3(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.HFS_PLUS_SIGNATURE)
    monkeypatch.setattr(macos, "pytsk3", None)

    with pytest.raises(macos.AdapterUnavailableError, match="pytsk3"):
        macos.HFSPlusAdapter(image)


def test_apfs_missing_tool_is_explicit_and_does_not_mount(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.APFS_CONTAINER_SIGNATURE)
    calls = []
    monkeypatch.setattr(macos.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        macos.subprocess, "Popen", lambda *args, **kwargs: calls.append(args)
    )

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


def test_apfs_status_is_read_only_and_unmounted_without_side_effects(
    monkeypatch, tmp_path
):
    image = _volume(tmp_path, macos.APFS_CONTAINER_SIGNATURE)
    calls = []
    monkeypatch.setattr(
        macos.subprocess, "Popen", lambda *args, **kwargs: calls.append(args)
    )
    adapter = macos.APFSFuseAdapter(image, executable="apfs-fuse")

    status = adapter.status

    assert status["filesystem"] == "APFS"
    assert status["read_only"] is True
    assert status["writable"] is False
    assert status["write_capabilities"] == []
    assert status["mount_required"] is True
    assert status["mounted"] is False
    assert status["mountpoint"] is None
    assert status["process_owned"] is False
    assert status["process_running"] is False
    assert calls == []


def test_apfs_status_tracks_owned_mount_process_and_close(monkeypatch, tmp_path):
    image = _volume(tmp_path, macos.APFS_CONTAINER_SIGNATURE)
    mountpoint = tmp_path / "mount"
    mountpoint.mkdir()

    class FakeProcess:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(macos.subprocess, "Popen", lambda *args, **kwargs: process)
    adapter = macos.APFSFuseAdapter(image, executable="apfs-fuse")
    adapter.mount(mountpoint, startup_timeout=0.01)

    mounted = adapter.status
    assert mounted["mounted"] is True
    assert mounted["mountpoint"] == str(mountpoint.resolve())
    assert mounted["process_owned"] is True
    assert mounted["process_running"] is True
    assert mounted["writable"] is False
    assert mounted["write_capabilities"] == []

    adapter.close()
    closed = adapter.status
    assert closed["mounted"] is False
    assert closed["mountpoint"] is None
    assert closed["process_owned"] is False
    assert closed["process_running"] is False
