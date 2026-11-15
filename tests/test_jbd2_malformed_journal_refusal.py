# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Malformed-journal refusal and hash preservation for the ext4 gate.

Issue #510, block B and block D:

* a journal whose metadata cannot be parsed must be refused — never reported as
  ``CLEAN``, because ``CLEAN`` is the only state that authorises a write;
* the gate is read-only, so ``sha256(image)`` must be identical before and after
  every check, including the failing ones (``#387`` hash preservation).

The three cases below are the malformed shapes a corrupted image can produce:
an unreadable file, a damaged ext4 superblock and a damaged JBD2 superblock.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess

import pytest

from mountfile.ext4_journal_gate import Ext4JournalGate, JournalState

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None
    or shutil.which("debugfs") is None
    or shutil.which("e2fsck") is None,
    reason="e2fsprogs tools are unavailable",
)

BLOCK_SIZE = 4096
JBD2_MAGIC = 0xC03B3998
EXT4_MAGIC_OFFSET = 1024 + 0x38


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_image(tmp_path):
    image = tmp_path / "fs.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    assert _run(
        [
            "mkfs.ext4",
            "-q",
            "-F",
            "-b",
            str(BLOCK_SIZE),
            "-O",
            "^metadata_csum_seed",
            str(image),
        ]
    ).returncode == 0
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload" + b"p" * (BLOCK_SIZE - 7))
    assert _run(
        ["debugfs", "-w", "-R", f"write {payload} /target", str(image)]
    ).returncode == 0
    return image


def _journal_superblock_offset(data: bytes) -> int:
    """Locate the JBD2 superblock by its magic (block type 4)."""
    needle = struct.pack(">I", JBD2_MAGIC)
    position = data.find(needle, BLOCK_SIZE)
    assert position >= 0, "no JBD2 superblock in the fixture"
    return position


def _assert_refused(image):
    """A malformed journal is never CLEAN, and checking never writes."""
    before = _sha256(image)
    result = Ext4JournalGate(image).check()
    after = _sha256(image)
    assert after == before, "the journal gate modified the image"
    assert result.state is not JournalState.CLEAN, (
        f"malformed journal reported as clean: {result.state} {result.message!r}"
    )
    return result


def test_unreadable_image_is_refused(tmp_path):
    image = tmp_path / "garbage.img"
    image.write_bytes(b"\x00" * (1 << 20))
    _assert_refused(image)


def test_truncated_image_is_refused(tmp_path):
    image = tmp_path / "truncated.img"
    image.write_bytes(b"\x00" * 512)
    _assert_refused(image)


@needs_ext
def test_damaged_ext4_superblock_is_refused(tmp_path):
    image = _make_image(tmp_path)
    data = bytearray(image.read_bytes())
    struct.pack_into("<H", data, EXT4_MAGIC_OFFSET, 0x0000)
    image.write_bytes(bytes(data))
    _assert_refused(image)


@needs_ext
def test_damaged_jbd2_superblock_magic_is_refused(tmp_path):
    image = _make_image(tmp_path)
    data = bytearray(image.read_bytes())
    offset = _journal_superblock_offset(bytes(data))
    struct.pack_into(">I", data, offset, 0xDEADBEEF)
    image.write_bytes(bytes(data))
    _assert_refused(image)


@needs_ext
def test_damaged_jbd2_block_type_is_refused(tmp_path):
    image = _make_image(tmp_path)
    data = bytearray(image.read_bytes())
    offset = _journal_superblock_offset(bytes(data))
    # h_blocktype sits right after h_magic; JBD2_SUPERBLOCK_V2 is 4.
    struct.pack_into(">I", data, offset + 4, 0x00000009)
    image.write_bytes(bytes(data))
    _assert_refused(image)


@needs_ext
def test_clean_image_stays_clean_and_read_only(tmp_path):
    """Control case: the same gate must accept an untouched fixture."""
    image = _make_image(tmp_path)
    before = _sha256(image)
    result = Ext4JournalGate(image).check()
    assert _sha256(image) == before
    assert result.state is JournalState.CLEAN, result.message
