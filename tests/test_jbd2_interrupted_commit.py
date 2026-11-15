"""Interrupted-commit refusal for the checksummed JBD2 replay path.

Issue #510 / acceptance #385: a transaction whose commit block never reached
the disk (torn write, power loss between data and commit) must be refused
before a single byte of the caller's image changes.
"""

from __future__ import annotations

import pytest
from test_jbd2_csum_replay import (
    BLOCK_SIZE,
    JBD2_FEATURE_INCOMPAT_CSUM_V3,
    NEW,
    _make_image,
    _set_recover,
    _sha256,
    _write_journal_superblock,
    needs_ext,
)

from mountfile.ext4_native_writer import (
    native_jbd2_recover,
    probe_journal,
    recover_journal_on_copy,
    resolve_journal_physical_blocks,
)
from mountfile.jbd2_writer import encode_checksummed_jbd2_transaction
from mountfile.journal_safety import JournalState


def _interrupted_v3_image(tmp_path):
    """A dirty checksummed journal whose last transaction has no commit block."""
    image, target_block, filesystem_blocks, uuid = _make_image(tmp_path)
    journal_blocks = resolve_journal_physical_blocks(str(image))
    records = encode_checksummed_jbd2_transaction(
        block_size=BLOCK_SIZE,
        sequence=2,
        filesystem_blocks=filesystem_blocks,
        journal_uuid=uuid,
        writes={target_block: NEW},
        journal_blocks=journal_blocks,
        checksum_v3=True,
    )
    with open(image, "r+b") as stream:
        # Descriptor + data only: the commit block never reaches the disk.
        for index, record in enumerate(records[:-1]):
            stream.seek(journal_blocks[1 + index] * BLOCK_SIZE)
            stream.write(record)
        stream.seek(journal_blocks[0] * BLOCK_SIZE)
        superblock = stream.read(BLOCK_SIZE)
        stream.seek(journal_blocks[0] * BLOCK_SIZE)
        stream.write(
            _write_journal_superblock(
                superblock,
                start=1,
                sequence=2,
                incompat=JBD2_FEATURE_INCOMPAT_CSUM_V3,
            )
        )
    _set_recover(image)
    return image, target_block


@needs_ext
def test_interrupted_commit_is_refused_and_source_preserved(tmp_path):
    image, _target = _interrupted_v3_image(tmp_path)
    before = _sha256(image)
    probe = probe_journal(str(image))
    assert probe.state is JournalState.NEEDS_REPLAY, probe.message
    with pytest.raises(Exception, match="commit|sequence|incomplete|descriptor"):
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
    assert _sha256(image) == before
