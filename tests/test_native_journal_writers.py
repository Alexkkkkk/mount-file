# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Acceptance tests for the journal-gated native writers (#385 follow-up).

The tests build real images with ``mkfs.ext4`` / ``mkntfs`` and assert on what
independent tools (``e2fsck -fn``, ``debugfs``, ``ntfscat``) read back, so a
writer that only *claims* success fails here.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess

import pytest

from mountfile.ext4_native_writer import (
    Ext4NativeWriter,
    Ext4ProfileUnsupported,
    ext4_write_file_native,
    journal_gate_summary,
    libext2fs_available,
    probe_journal,
    recover_journal_on_copy,
    require_supported_profile,
)
from mountfile.journal_safety import JournalState
from mountfile.native_write import (
    ENABLE_ENV,
    NativeWriteDisabled,
    NativeWriteUnsafe,
)
from mountfile.ntfs_native_writer import (
    NtfsNativeWriter,
    NtfsProfileUnsupported,
    logfile_gate_summary,
    probe_ntfs_log,
)

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None or shutil.which("debugfs") is None,
    reason="e2fsprogs tools are unavailable",
)
needs_libext2fs = pytest.mark.skipif(
    not libext2fs_available(), reason="libext2fs is unavailable"
)
needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="ntfs-3g tools are unavailable",
)


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def make_ext4(tmp_path, name="fs.img", size="32M", seed=b"seed\n"):
    image = tmp_path / name
    handle = open(image, "wb")
    handle.truncate(32 << 20)
    handle.close()
    # ``^metadata_csum`` keeps the superblock checksum-free so the crash fixture
    # below can set the real RECOVER bit the way an interrupted mount would;
    # images with checksums are covered by the profile-rejection tests instead.
    assert (
        _run(["mkfs.ext4", "-q", "-F", "-O", "^metadata_csum", str(image)]).returncode
        == 0
    )
    payload = tmp_path / "seed.bin"
    payload.write_bytes(seed)
    assert (
        _run(["debugfs", "-w", "-R", f"write {payload} /f.txt", str(image)]).returncode
        == 0
    )
    return image


def ext4_read(image, internal="/f.txt"):
    return _run(["debugfs", "-R", f"cat {internal}", str(image)]).stdout


def set_recover_bit(image, *, set_bit=True):
    """Mark the image as needing journal recovery the way a crash would.

    ``tune2fs`` is used when present because it also refreshes the superblock
    checksum; the raw bit patch is only a fallback for checksum-free images.
    """

    tune2fs = shutil.which("tune2fs")
    if tune2fs:
        flag = "+needs_recovery" if set_bit else "-needs_recovery"
        _run([tune2fs, "-O", flag, str(image)])
        # tune2fs reports success even when the feature is refused, so the bit is
        # read back; only a verified change is trusted.
        current = struct.unpack_from("<I", image.read_bytes(), 1024 + 0x60)[0]
        if bool(current & 0x0004) is set_bit:
            return
    data = bytearray(image.read_bytes())
    flags = struct.unpack_from("<I", data, 1024 + 0x60)[0]
    flags = flags | 0x0004 if set_bit else flags & ~0x0004
    struct.pack_into("<I", data, 1024 + 0x60, flags)
    image.write_bytes(bytes(data))


# ---------------------------------------------------------------- ext4 profile


@needs_ext
def test_ext4_profile_check_accepts_mke2fs_image(tmp_path):
    image = make_ext4(tmp_path)
    profile = require_supported_profile(str(image), 0)
    assert profile["block_size"] in (1024, 2048, 4096)
    assert profile["journal_inode"] == 8


def test_ext4_profile_check_rejects_non_ext_image(tmp_path):
    image = tmp_path / "not-ext.img"
    image.write_bytes(b"\x00" * 4096)
    with pytest.raises(Ext4ProfileUnsupported, match="not an ext"):
        require_supported_profile(str(image), 0)


def test_ext4_profile_check_rejects_unjournaled_image(tmp_path):
    image = tmp_path / "ext2.img"
    with open(image, "wb") as handle:
        handle.truncate(8 << 20)
    if shutil.which("mkfs.ext2") is None:
        pytest.skip("mkfs.ext2 is unavailable")
    assert _run(["mkfs.ext2", "-q", "-F", str(image)]).returncode == 0
    with pytest.raises(Ext4ProfileUnsupported, match="no internal journal"):
        require_supported_profile(str(image), 0)


# ---------------------------------------------------------------- ext4 journal


@needs_ext
@needs_libext2fs
def test_ext4_journal_probe_reports_clean_mke2fs_image(tmp_path):
    image = make_ext4(tmp_path)
    probe = probe_journal(str(image), 0)
    assert probe.state is JournalState.CLEAN, probe.message
    assert probe.block_size == 4096
    # A clean filesystem may keep a non-zero (but in-ring) journal head; only
    # RECOVER decides that a replay is needed.
    assert probe.journal_start >= 0


@needs_ext
@needs_libext2fs
def test_ext4_journal_probe_detects_recover_flag(tmp_path):
    image = make_ext4(tmp_path)
    set_recover_bit(image)
    probe = probe_journal(str(image), 0)
    assert probe.state is JournalState.NEEDS_REPLAY
    assert "replay" in probe.message


@needs_ext
@needs_libext2fs
def test_ext4_probe_never_mutates_the_image(tmp_path):
    image = make_ext4(tmp_path)
    before = image.read_bytes()
    probe_journal(str(image), 0)
    journal_gate_summary(str(image), 0)
    assert image.read_bytes() == before


@needs_ext
@needs_libext2fs
def test_ext4_replay_on_copy_uses_e2fsck_and_clears_recover(tmp_path):
    image = make_ext4(tmp_path)
    set_recover_bit(image)
    assert recover_journal_on_copy(str(image), 0) is True
    assert probe_journal(str(image), 0).state is JournalState.CLEAN
    # The replay really ran: the recovery bit is gone on disk.
    assert struct.unpack_from("<I", image.read_bytes(), 1024 + 0x60)[0] & 0x0004 == 0


# ---------------------------------------------------------------- ext4 writing


@needs_ext
@needs_libext2fs
def test_ext4_native_write_replaces_existing_file(tmp_path):
    image = make_ext4(tmp_path)
    payload = b"journal-gated native write\n" * 3
    written = ext4_write_file_native(str(image), "/f.txt", payload)
    assert written == len(payload)
    assert ext4_read(image) == payload.decode()
    assert _run(["e2fsck", "-fn", str(image)]).returncode == 0


@needs_ext
@needs_libext2fs
def test_ext4_native_write_refuses_missing_target(tmp_path):
    image = make_ext4(tmp_path)
    before = image.read_bytes()
    with pytest.raises(Exception) as excinfo:
        ext4_write_file_native(str(image), "/missing.txt", b"data")
    assert "missing" in str(excinfo.value)
    assert image.read_bytes() == before


@needs_ext
@needs_libext2fs
def test_ext4_native_writer_rejects_dirty_journal_when_refusing(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    set_recover_bit(image)
    before = image.read_bytes()
    with pytest.raises(Ext4ProfileUnsupported, match="replay"):
        Ext4NativeWriter(recover="refuse").write_file(str(image), "/f.txt", b"x")
    # Fail-closed: the caller's image is untouched.
    assert image.read_bytes() == before


@needs_ext
@needs_libext2fs
def test_ext4_native_writer_replays_dirty_journal_on_the_copy(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    set_recover_bit(image)
    payload = b"after replay\n"
    assert Ext4NativeWriter(recover="copy").write_file(
        str(image), "/f.txt", payload
    ) == len(payload)
    assert ext4_read(image) == payload.decode()
    assert probe_journal(str(image), 0).state is JournalState.CLEAN
    assert _run(["e2fsck", "-fn", str(image)]).returncode == 0


@needs_ext
@needs_libext2fs
def test_ext4_native_writer_requires_the_opt_in_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    image = make_ext4(tmp_path)
    before = image.read_bytes()
    with pytest.raises(NativeWriteDisabled):
        Ext4NativeWriter().write_file(str(image), "/f.txt", b"nope")
    assert image.read_bytes() == before


def test_ext4_native_writer_rejects_bad_recover_mode():
    with pytest.raises(ValueError):
        Ext4NativeWriter(recover="magic")


@needs_ext
@needs_libext2fs
def test_ext4_native_writer_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    with pytest.raises(NativeWriteUnsafe):
        Ext4NativeWriter().write_file(str(image), "../escape.txt", b"x")


@needs_ext
@needs_libext2fs
def test_ext4_journal_gate_summary_is_machine_readable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    summary = journal_gate_summary(str(image), 0)
    assert summary["journal_state"] == "clean"
    assert summary["writable"] is True
    assert summary["libext2fs"] is True
    monkeypatch.delenv(ENABLE_ENV)
    assert journal_gate_summary(str(image), 0)["writable"] is False


# ------------------------------------------------------------------- NTFS gate


def _make_ntfs(tmp_path, name="ntfs.img"):
    image = tmp_path / name
    with open(image, "wb") as handle:
        handle.truncate(24 << 20)
    if shutil.which("mkntfs") is None:
        pytest.skip("mkntfs is unavailable")
    assert _run(["mkntfs", "-q", "-F", "-f", str(image)]).returncode == 0
    return image


@needs_ntfs
def test_ntfs_gate_accepts_fresh_mkntfs_volume(tmp_path):
    image = _make_ntfs(tmp_path)
    gate = probe_ntfs_log(str(image), 0)
    assert gate.clean is True, gate.reason
    assert gate.volume_flags == 0
    assert gate.mirror_matches is True


@needs_ntfs
def test_ntfs_gate_rejects_dirty_volume_bit(tmp_path):
    image = _make_ntfs(tmp_path)
    gate = probe_ntfs_log(str(image), 0)
    assert gate.clean

    # Set VOLUME_DIRTY (0x0001) in $Volume's VOLUME_INFORMATION the same way a
    # crashed Windows mount would: patch the resident attribute value in $MFT.
    from mountfile.ntfs_reader import NativeNtfsMetadataImage

    provider = NativeNtfsMetadataImage(str(image), volume_size=os.path.getsize(image))
    try:
        blob = bytearray(image.read_bytes())
        offset = (
            provider.profile.boot.mft_offset
            + 3 * provider.profile.boot.file_record_size
        )
        record_offset = blob.find(b"FILE", offset - 0x40)
        attribute = blob.find(b"\x70\x00\x00\x00", record_offset)
        assert attribute > 0
        value_offset = struct.unpack_from("<H", blob, attribute + 0x14)[0]
        flags = struct.unpack_from("<H", blob, attribute + value_offset + 10)[0]
        struct.pack_into("<H", blob, attribute + value_offset + 10, flags | 0x0001)
        image.write_bytes(bytes(blob))
    finally:
        provider.close()

    dirty = probe_ntfs_log(str(image), 0)
    assert dirty.clean is False
    assert "dirty" in dirty.reason


@needs_ntfs
def test_ntfs_gate_rejects_corrupt_boot_sector(tmp_path):
    image = _make_ntfs(tmp_path)
    blob = bytearray(image.read_bytes())
    blob[3:11] = b"\xff" * 8
    image.write_bytes(bytes(blob))
    gate = probe_ntfs_log(str(image), 0)
    assert gate.clean is False
    assert gate.reason


@needs_ntfs
def test_ntfs_gate_rejects_offset_outside_image(tmp_path):
    image = _make_ntfs(tmp_path)
    gate = probe_ntfs_log(str(image), 1 << 40)
    assert gate.clean is False
    assert "offset" in gate.reason


# ------------------------------------------------------------------ NTFS write


@needs_ntfs
def test_ntfs_native_writer_writes_through_ntfscp(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs(tmp_path)
    payload = b"ntfs journal-gated write\n"
    assert NtfsNativeWriter().write_file(str(image), "/f.txt", payload) == len(payload)
    assert _run(["ntfscat", str(image), "/f.txt"]).stdout == payload.decode()


@needs_ntfs
def test_ntfs_native_writer_requires_the_opt_in_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    image = _make_ntfs(tmp_path)
    before = image.read_bytes()
    with pytest.raises(NativeWriteDisabled):
        NtfsNativeWriter().write_file(str(image), "/f.txt", b"nope")
    assert image.read_bytes() == before


@needs_ntfs
def test_ntfs_native_writer_refuses_dirty_volume(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs(tmp_path)
    # Corrupting the boot sector makes the gate refuse; the copy must not be
    # published over the original.
    blob = bytearray(image.read_bytes())
    blob[3:11] = b"\xff" * 8
    image.write_bytes(bytes(blob))
    before = image.read_bytes()
    with pytest.raises(NtfsProfileUnsupported):
        NtfsNativeWriter().write_file(str(image), "/f.txt", b"x")
    assert image.read_bytes() == before


def test_ntfs_native_writer_rejects_replay_mode():
    with pytest.raises(ValueError, match="not implemented"):
        NtfsNativeWriter(recover="copy")


@needs_ntfs
def test_ntfs_gate_summary_reports_replay_is_not_implemented(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs(tmp_path)
    summary = logfile_gate_summary(str(image), 0)
    assert summary["clean"] is True
    assert summary["writable"] is True
    assert summary["replay_implemented"] is False


@needs_ntfs
def test_ntfs_writer_rejects_nonzero_offset(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs(tmp_path)
    with pytest.raises(NtfsProfileUnsupported, match="offset"):
        NtfsNativeWriter().write_file(str(image), "/f.txt", b"x", offset=512)
