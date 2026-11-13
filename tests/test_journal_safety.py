from __future__ import annotations

import struct
import subprocess

import pytest

from mountfile.journal_safety import (
    Ext4JournalManager,
    JournalSafetyError,
    JournalState,
    NTFSLogFileManager,
    NTFSLogState,
)


def _ext4_image(path, incompat=0):
    image = bytearray(2048)
    struct.pack_into("<H", image, 1024 + 0x38, 0xEF53)
    struct.pack_into("<I", image, 1024 + 0x60, incompat)
    path.write_bytes(image)


def test_ext4_recover_flag_is_not_treated_as_clean(tmp_path):
    image = tmp_path / "ext4.img"
    _ext4_image(image, incompat=0x0004)

    result = Ext4JournalManager(str(image)).check_journal()

    assert result.state is JournalState.NEEDS_REPLAY
    with pytest.raises(JournalSafetyError, match="replay"):
        Ext4JournalManager(str(image)).require_clean()


def test_ext4_clean_state_is_verified_without_mutation(tmp_path):
    image = tmp_path / "ext4.img"
    _ext4_image(image)
    before = image.read_bytes()

    result = Ext4JournalManager(str(image)).check_journal()

    assert result.state is JournalState.CLEAN
    assert image.read_bytes() == before


def test_ntfs_log_gate_is_fail_closed_on_runner_error():
    def runner(*args, **kwargs):
        raise RuntimeError("dirty logfile")

    result = NTFSLogFileManager().check_logfile("image.img", runner)

    assert result.state is NTFSLogState.NEEDS_REPLAY
    with pytest.raises(JournalSafetyError, match="dirty logfile"):
        NTFSLogFileManager().require_clean("image.img", runner)


def test_ntfs_log_gate_does_not_treat_ntfsfix_as_authoritative():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "", "")

    result = NTFSLogFileManager().check_logfile("image.img", runner)

    assert result.state is NTFSLogState.ERROR
    with pytest.raises(JournalSafetyError, match="authoritative"):
        NTFSLogFileManager().require_clean("image.img", runner)
