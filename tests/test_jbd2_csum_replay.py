"""End-to-end JBD2 checksum v2/v3 replay tests on real ext4 images.

The fixture starts from a normal ``mkfs.ext4`` filesystem (metadata_csum is on
by default, and ``metadata_csum_seed`` is explicitly disabled so the image
stays inside the writer's validated profile on e2fsprogs 1.47+, which turns
metadata_csum_seed on by default), then writes a synthetic but JBD2-shaped
dirty journal: a v3 superblock with a valid CRC32C, one committed
descriptor/data/commit transaction that overwrites a file block, and the
ext4 RECOVER bit set.  The native coordinator must replay the committed
block, refresh both checksums, checkpoint the ring, clear RECOVER, and leave
``e2fsck -fn`` clean.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess

import pytest

from mountfile.ext4_jbd2_csum import (
    JBD2_CRC32C_CHKSUM,
    JBD2_FEATURE_INCOMPAT_CSUM_V2,
    JBD2_FEATURE_INCOMPAT_CSUM_V3,
    journal_superblock_checksum,
    verify_journal_superblock_checksum,
)
from mountfile.ext4_native_writer import (
    native_jbd2_recover,
    probe_journal,
    recover_journal_on_copy,
    resolve_journal_physical_blocks,
)
from mountfile.ext_checksum import ext4_superblock_checksum
from mountfile.jbd2_checkpoint import scan_jbd2_transactions
from mountfile.jbd2_writer import (
    JBD2JournalState,
    JBD2WriteProfileError,
    decode_checksummed_jbd2_transaction,
    encode_checksummed_jbd2_transaction,
    parse_jbd2_superblock,
)
from mountfile.journal_safety import JournalState

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None
    or shutil.which("debugfs") is None
    or shutil.which("e2fsck") is None,
    reason="e2fsprogs tools are unavailable",
)
BLOCK_SIZE = 4096
OLD = b"OLD!" + b"o" * (BLOCK_SIZE - 4)
NEW = b"NEW!" + b"n" * (BLOCK_SIZE - 4)


def _run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _make_image(tmp_path):
    image = tmp_path / "fs.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    assert (
        _run(
            [
                "mkfs.ext4",
                "-q",
                "-F",
                "-b",
                str(BLOCK_SIZE),
                # e2fsprogs >= 1.47 enables metadata_csum_seed by default;
                # the native writer's validated profile rejects it
                # (EXT4_FEATURE_INCOMPAT_CSUM_SEED), so pin it off while
                # keeping metadata_csum itself enabled.
                "-O",
                "^metadata_csum_seed",
                str(image),
            ]
        ).returncode
        == 0
    )
    incompat = struct.unpack_from("<I", image.read_bytes(), 1024 + 0x60)[0]
    assert not incompat & 0x2000, "metadata_csum_seed must be off for this fixture"
    payload = tmp_path / "payload.bin"
    payload.write_bytes(OLD + b"p" * BLOCK_SIZE)
    assert (
        _run(["debugfs", "-w", "-R", f"write {payload} /target", str(image)]).returncode
        == 0
    )
    blocks = _run(["debugfs", "-R", "blocks /target", str(image)]).stdout.split()
    assert blocks, "debugfs did not report the target block map"
    target_block = int(blocks[0])
    superblock = image.read_bytes()[1024:2048]
    filesystem_blocks = struct.unpack_from("<I", superblock, 0x04)[0]
    uuid = superblock[0x68:0x78]
    return image, target_block, filesystem_blocks, uuid


def _write_journal_superblock(raw, *, start, sequence, incompat):
    block = bytearray(raw)
    struct.pack_into(">I", block, 0x18, sequence)
    struct.pack_into(">I", block, 0x1C, start)
    struct.pack_into(">I", block, 0x28, incompat)
    block[0x50] = JBD2_CRC32C_CHKSUM
    struct.pack_into(">I", block, 0xFC, journal_superblock_checksum(bytes(block)))
    return bytes(block)


def _set_recover(image):
    """Set the ext4 RECOVER bit and refresh metadata_csum's s_checksum."""
    data = bytearray(image.read_bytes())
    flags = struct.unpack_from("<I", data, 1024 + 0x60)[0]
    struct.pack_into("<I", data, 1024 + 0x60, flags | 0x0004)
    superblock = data[1024:2048]
    ro_compat = struct.unpack_from("<I", superblock, 0x64)[0]
    if ro_compat & 0x0400:
        struct.pack_into(
            "<I", data, 1024 + 0x3FC, ext4_superblock_checksum(bytes(superblock))
        )
    image.write_bytes(bytes(data))


def _dirty_v3_image(tmp_path, *, sequence=2, bad_commit=False):
    image, target_block, filesystem_blocks, uuid = _make_image(tmp_path)
    journal_blocks = resolve_journal_physical_blocks(str(image))
    records = encode_checksummed_jbd2_transaction(
        block_size=BLOCK_SIZE,
        sequence=sequence,
        filesystem_blocks=filesystem_blocks,
        journal_uuid=uuid,
        writes={target_block: NEW},
        journal_blocks=journal_blocks,
        checksum_v3=True,
    )
    if bad_commit:
        commit = bytearray(records[-1])
        commit[16] ^= 0xFF
        records = records[:-1] + (bytes(commit),)
    with open(image, "r+b") as stream:
        for index, record in enumerate(records):
            stream.seek(journal_blocks[1 + index] * BLOCK_SIZE)
            stream.write(record)
        stream.seek(journal_blocks[0] * BLOCK_SIZE)
        superblock = stream.read(BLOCK_SIZE)
        stream.seek(journal_blocks[0] * BLOCK_SIZE)
        stream.write(
            _write_journal_superblock(
                superblock,
                start=1,
                sequence=sequence,
                incompat=JBD2_FEATURE_INCOMPAT_CSUM_V3,
            )
        )
    _set_recover(image)
    return image, target_block, uuid, journal_blocks, records


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target_bytes(image, tmp_path):
    destination = tmp_path / "target.out"
    result = _run(["debugfs", "-R", f"dump /target {destination}", str(image)])
    assert result.returncode == 0, result.stderr
    return destination.read_bytes()


def test_checksummed_superblock_roundtrip_and_tag_layout():
    block = bytearray(BLOCK_SIZE)
    struct.pack_into(">III", block, 0, 0xC03B3998, 4, 0)
    struct.pack_into(">IIIII", block, 12, BLOCK_SIZE, 1024, 1, 1, 0)
    block[0x28] = JBD2_FEATURE_INCOMPAT_CSUM_V3
    block[0x50] = JBD2_CRC32C_CHKSUM
    struct.pack_into(">I", block, 0xFC, journal_superblock_checksum(bytes(block)))
    assert verify_journal_superblock_checksum(bytes(block))
    block[0x30] ^= 1
    assert not verify_journal_superblock_checksum(bytes(block))


def test_encoder_decoder_roundtrip_v2_and_v3():
    uuid = bytes(range(16))
    for v3, feature in (
        (True, JBD2_FEATURE_INCOMPAT_CSUM_V3),
        (False, JBD2_FEATURE_INCOMPAT_CSUM_V2),
    ):
        records = encode_checksummed_jbd2_transaction(
            block_size=1024,
            sequence=7,
            filesystem_blocks=512,
            journal_uuid=uuid,
            writes={9: b"A" * 1024},
            revokes=[10],
            checksum_v3=v3,
        )
        decoded = decode_checksummed_jbd2_transaction(
            records,
            block_size=1024,
            filesystem_blocks=512,
            journal_features=(0, feature, 0),
            filesystem_uuid=uuid,
        )
        assert decoded.sequence == 7
        assert decoded.writes == ((9, b"A" * 1024),)
        assert decoded.revokes == (10,)


def test_scanner_selects_checksummed_profile():
    uuid = bytes(range(16))
    records = encode_checksummed_jbd2_transaction(
        block_size=1024,
        sequence=3,
        filesystem_blocks=256,
        journal_uuid=uuid,
        writes={8: b"B" * 1024},
    )
    ring = [bytes(1024)] * 8
    ring[1 : 1 + len(records)] = list(records)
    state = JBD2JournalState(
        block_size=1024,
        max_length=8,
        first=1,
        sequence=3,
        start=1,
        uuid=uuid,
        empty=False,
        feature_incompat=JBD2_FEATURE_INCOMPAT_CSUM_V3,
        checksum_type=1,
    )
    transactions = scan_jbd2_transactions(ring, state)
    assert len(transactions) == 1
    assert transactions[0] == records


def _checksummed_rollover_ring(second_sequence: int):
    uuid = bytes(range(16))
    first = encode_checksummed_jbd2_transaction(
        block_size=1024,
        sequence=0xFFFFFFFF,
        filesystem_blocks=256,
        journal_uuid=uuid,
        writes={8: b"A" * 1024},
    )
    second = encode_checksummed_jbd2_transaction(
        block_size=1024,
        sequence=second_sequence,
        filesystem_blocks=256,
        journal_uuid=uuid,
        writes={9: b"B" * 1024},
    )
    ring = [bytes(1024)] * 12
    ring[1 : 1 + len(first)] = list(first)
    ring[1 + len(first) : 1 + len(first) + len(second)] = list(second)
    state = JBD2JournalState(
        block_size=1024,
        max_length=len(ring),
        first=1,
        sequence=0xFFFFFFFF,
        start=1,
        uuid=uuid,
        empty=False,
        feature_incompat=JBD2_FEATURE_INCOMPAT_CSUM_V3,
        checksum_type=JBD2_CRC32C_CHKSUM,
    )
    return ring, state


def test_checksummed_scanner_accepts_sequence_wrap_at_u32_boundary():
    ring, state = _checksummed_rollover_ring(0)
    transactions = scan_jbd2_transactions(ring, state)
    assert len(transactions) == 2
    assert struct.unpack_from(">I", transactions[0][0], 8)[0] == 0xFFFFFFFF
    assert struct.unpack_from(">I", transactions[1][0], 8)[0] == 0
    decoded = decode_checksummed_jbd2_transaction(
        transactions[1],
        block_size=state.block_size,
        filesystem_blocks=256,
        journal_features=(0, JBD2_FEATURE_INCOMPAT_CSUM_V3, 0),
        filesystem_uuid=state.uuid,
    )
    assert decoded.sequence == 0


def test_checksummed_scanner_rejects_wrong_sequence_after_u32_wrap():
    ring, state = _checksummed_rollover_ring(1)
    with pytest.raises(JBD2WriteProfileError, match="expected sequence"):
        scan_jbd2_transactions(ring, state)


@needs_ext
def test_native_v3_replay_restores_committed_block(tmp_path):
    image, target_block, uuid, journal_blocks, records = _dirty_v3_image(tmp_path)
    before = probe_journal(str(image))
    assert before.state is JournalState.NEEDS_REPLAY
    assert before.checksummed

    changed = recover_journal_on_copy(
        str(image), 0, native_coordinator=native_jbd2_recover
    )
    assert changed is True

    after = probe_journal(str(image))
    assert after.state is JournalState.CLEAN, after.message
    assert after.journal_start == 0
    assert after.checksummed
    assert _target_bytes(image, tmp_path)[:4] == b"NEW!"
    assert _run(["e2fsck", "-fn", str(image)]).returncode == 0

    journal_superblock = image.read_bytes()[
        journal_blocks[0] * BLOCK_SIZE : (journal_blocks[0] + 1) * BLOCK_SIZE
    ]
    assert verify_journal_superblock_checksum(journal_superblock)
    state = parse_jbd2_superblock(
        journal_superblock,
        filesystem_block_size=BLOCK_SIZE,
        filesystem_uuid=uuid,
        journal_blocks=len(journal_blocks),
        allow_checksums=True,
    )
    assert state.checksummed and state.start == 0


@needs_ext
def test_bad_commit_checksum_is_refused_before_any_write(tmp_path):
    image, _target_block, _uuid, _journal_blocks, _records = _dirty_v3_image(
        tmp_path, bad_commit=True
    )
    before = _sha256(image)
    with pytest.raises(Exception, match="checksum|commit"):
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
    assert _sha256(image) == before


@needs_ext
def test_bad_journal_superblock_checksum_is_an_error(tmp_path):
    image, _target_block, _uuid, journal_blocks, _records = _dirty_v3_image(tmp_path)
    # Corrupt a byte of ``s_checksum`` (0xFC) rather than ``0x30``.  Offset
    # 0x30 is the *high* byte of the big-endian ``s_maxlen`` word, which is
    # already 0x00 for every realistic journal, so the write was a no-op on
    # most images: the superblock checksum stayed valid and the probe returned
    # NEEDS_REPLAY (RECOVER is set) instead of ERROR.  That made the test pass
    # or fail depending on the journal size mke2fs happened to pick.
    with open(image, "r+b") as stream:
        stream.seek(journal_blocks[0] * BLOCK_SIZE + 0xFC)
        checksum = stream.read(4)
        assert len(checksum) == 4
        stream.seek(journal_blocks[0] * BLOCK_SIZE + 0xFC)
        stream.write(bytes([checksum[0] ^ 0xFF]) + checksum[1:])
    probe = probe_journal(str(image))
    assert probe.state is JournalState.ERROR, probe.message
    assert probe.state is JournalState.ERROR
    before = _sha256(image)
    with pytest.raises(Exception):
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
    assert _sha256(image) == before
