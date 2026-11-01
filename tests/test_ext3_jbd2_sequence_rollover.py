# SPDX-License-Identifier: MIT
"""Focused JBD2 replay sequence rollover regressions."""

import hashlib
import struct

import pytest

from mountfile.ext3_reader import Ext3ReadError, NativeExt3Image
from tests.test_ext2_reader import BLOCK_SIZE, BLOCKS_COUNT
from tests.test_ext3_reader import _make_clean_journal_image

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JOURNAL_INODE = 8
_JOURNAL_FIRST_BLOCK = 40
_JOURNAL_BLOCK_COUNT = 8
_REPLAY_TARGET = 21


def _write_transaction(journal, logical, sequence, payload):
    descriptor_offset = logical * BLOCK_SIZE
    struct.pack_into(
        ">III", journal, descriptor_offset, _JBD2_MAGIC, _JBD2_DESCRIPTOR, sequence
    )
    struct.pack_into(">II", journal, descriptor_offset + 12, _REPLAY_TARGET, 0x0000000A)

    data_offset = (logical + 1) * BLOCK_SIZE
    journal[data_offset : data_offset + BLOCK_SIZE] = payload.ljust(BLOCK_SIZE, b"\x00")

    commit_offset = (logical + 2) * BLOCK_SIZE
    struct.pack_into(
        ">III", journal, commit_offset, _JBD2_MAGIC, _JBD2_COMMIT, sequence
    )


def _make_rollover_journal_image(path, second_sequence=0):
    _make_clean_journal_image(path)
    image = bytearray(path.read_bytes())

    journal_inode_offset = 5 * BLOCK_SIZE + (_JOURNAL_INODE - 1) * 128
    struct.pack_into(
        "<I", image, journal_inode_offset + 4, _JOURNAL_BLOCK_COUNT * BLOCK_SIZE
    )
    struct.pack_into(
        "<8I",
        image,
        journal_inode_offset + 40,
        *range(_JOURNAL_FIRST_BLOCK, _JOURNAL_FIRST_BLOCK + _JOURNAL_BLOCK_COUNT),
    )

    journal_start = _JOURNAL_FIRST_BLOCK * BLOCK_SIZE
    journal = memoryview(image)[
        journal_start : journal_start + _JOURNAL_BLOCK_COUNT * BLOCK_SIZE
    ]
    journal[:] = bytes(len(journal))
    filesystem_uuid = bytes(image[1024 + 0x68 : 1024 + 0x78])
    struct.pack_into(">III", journal, 0, _JBD2_MAGIC, 3, 0xFFFFFFFF)
    struct.pack_into(">II", journal, 0x0C, BLOCK_SIZE, _JOURNAL_BLOCK_COUNT)
    struct.pack_into(">II", journal, 0x14, 1, 1)
    journal[0x30:0x40] = filesystem_uuid

    _write_transaction(journal, 1, 0xFFFFFFFF, b"before-rollover")
    _write_transaction(journal, 4, second_sequence, b"after-rollover")
    path.write_bytes(image)


def test_jbd2_replay_accepts_sequence_rollover_and_publishes_final_data(tmp_path):
    image = tmp_path / "jbd2-sequence-rollover.img"
    _make_rollover_journal_image(image)
    original_hash = hashlib.sha256(image.read_bytes()).digest()

    provider = NativeExt3Image(
        image,
        volume_size=BLOCKS_COUNT * BLOCK_SIZE,
        replay_journal=True,
    )
    try:
        assert provider.journal_replayed is True
        assert provider._read_at(
            _REPLAY_TARGET * BLOCK_SIZE, len(b"after-rollover")
        ) == (b"after-rollover")
    finally:
        provider.close()

    assert hashlib.sha256(image.read_bytes()).digest() == original_hash


def test_jbd2_replay_rejects_wrong_sequence_after_rollover(tmp_path):
    image = tmp_path / "jbd2-sequence-rollover-invalid.img"
    _make_rollover_journal_image(image, second_sequence=1)
    original_hash = hashlib.sha256(image.read_bytes()).digest()

    with pytest.raises(Ext3ReadError, match="descriptor sequence"):
        NativeExt3Image(
            image,
            volume_size=BLOCKS_COUNT * BLOCK_SIZE,
            replay_journal=True,
        )

    assert hashlib.sha256(image.read_bytes()).digest() == original_hash
