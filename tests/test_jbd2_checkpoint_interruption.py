# SPDX-License-Identifier: MIT
"""Issue #530, P3: interruption of the checksummed JBD2 checkpoint.

The checkpoint executor writes data blocks first, takes a durable barrier, and
only then writes the journal superblock that carries the clean marker
(``s_start == 0``).  These tests inject a failure at every phase boundary of
``coordinate_jbd2_replay_from_ring`` on a real dirty v3 image and prove the
crash-consistency contract required by #530 / #387:

* a crash **before the clean marker** leaves the journal ring unchanged
  (``s_start != 0``), so the volume still requires replay — a later recovery
  pass re-applies the committed transaction and ``e2fsck -fn`` accepts it;
* a full run checkpoints the ring and leaves the volume clean.

The image is always a disposable copy inside ``tmp_path``; the source
preservation invariant is asserted by the hash checks of the caller paths in
``test_ext4_writer_fault_coverage.py``.
"""

from __future__ import annotations

import shutil
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys

import pytest
from test_jbd2_csum_replay import _dirty_v3_image, _target_bytes

from mountfile.ext4_native_writer import (
    _clear_recover_flag,
    _recover_flag_set,
    native_jbd2_recover,
    probe_journal,
    recover_journal_on_copy,
    resolve_journal_physical_blocks,
)
from mountfile.jbd2_checkpoint import (
    JBD2CheckpointExecutionError,
    JBD2CheckpointWrite,
    coordinate_jbd2_replay_from_ring,
)
from mountfile.jbd2_writer import parse_jbd2_superblock
from mountfile.journal_safety import JournalState

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None
    or shutil.which("debugfs") is None
    or shutil.which("e2fsck") is None,
    reason="e2fsprogs tools are unavailable",
)

BLOCK_SIZE = 4096

_SIGKILL_CHILD = r'''
import os
import signal
import struct
import sys

from mountfile.ext4_native_writer import resolve_journal_physical_blocks
from mountfile.jbd2_checkpoint import coordinate_jbd2_replay_from_ring
from mountfile.jbd2_writer import parse_jbd2_superblock

image_path, uuid_hex, target_phase = sys.argv[1:]
image_uuid = bytes.fromhex(uuid_hex)
journal_physical = resolve_journal_physical_blocks(image_path)
with open(image_path, "r+b") as stream:
    stream.seek(1024)
    superblock = stream.read(1024)
    filesystem_blocks = struct.unpack_from("<I", superblock, 0x04)[0]
    ring = []
    for physical in journal_physical:
        stream.seek(physical * 4096)
        ring.append(stream.read(4096))
    state = parse_jbd2_superblock(
        ring[0],
        filesystem_block_size=4096,
        filesystem_uuid=image_uuid,
        journal_blocks=len(journal_physical),
        allow_checksums=True,
    )
    def kill_at(write):
        if write.phase == target_phase:
            os.kill(os.getpid(), signal.SIGKILL)
    coordinate_jbd2_replay_from_ring(
        stream,
        state=state,
        physical_blocks=journal_physical,
        filesystem_uuid=image_uuid,
        filesystem_blocks=filesystem_blocks,
        journal_physical_blocks=journal_physical,
        journal_superblock_physical_block=journal_physical[0],
        sync=lambda: None,
        fault_injector=kill_at,
    )
'''


def _fault_at(phase: str):
    def inject(write: JBD2CheckpointWrite) -> None:
        if write.phase == phase:
            raise JBD2CheckpointExecutionError(
                f"injected checkpoint failure at phase {phase}"
            )

    return inject


def _coordinate_with_injection(image, uuid: bytes, *, fault_at_phase: str):
    """Run the ring coordinator with a fault injected at the given phase."""
    superblock = image.read_bytes()[1024:2048]
    blocks_lo = struct.unpack_from("<I", superblock, 0x04)[0]
    journal_physical = resolve_journal_physical_blocks(str(image))
    with open(image, "r+b") as stream:
        ring = []
        for physical in journal_physical:
            stream.seek(physical * BLOCK_SIZE)
            block = stream.read(BLOCK_SIZE)
            assert len(block) == BLOCK_SIZE
            ring.append(block)
        state = parse_jbd2_superblock(
            ring[0],
            filesystem_block_size=BLOCK_SIZE,
            filesystem_uuid=uuid,
            journal_blocks=len(journal_physical),
            allow_checksums=True,
        )
        coordinate_jbd2_replay_from_ring(
            stream,
            state=state,
            physical_blocks=journal_physical,
            filesystem_uuid=uuid,
            filesystem_blocks=blocks_lo,
            journal_physical_blocks=journal_physical,
            journal_superblock_physical_block=journal_physical[0],
            sync=lambda: None,
            fault_injector=_fault_at(fault_at_phase),
        )


@needs_ext
@pytest.mark.parametrize("phase", ["data", "journal-superblock"])
def test_checkpoint_interruption_before_clean_marker_requires_replay(tmp_path, phase):
    """A crash before the durable clean marker never fakes a clean volume."""
    image, target_block, uuid, _journal_blocks, _records = _dirty_v3_image(tmp_path)

    with pytest.raises(JBD2CheckpointExecutionError):
        _coordinate_with_injection(image, uuid, fault_at_phase=phase)

    # The journal head was never checkpointed: replay is still required and
    # the volume must not be reported clean.
    probe = probe_journal(str(image))
    assert probe.state is JournalState.NEEDS_REPLAY, probe.message
    assert probe.journal_start != 0
    # The data-before-marker barrier is observable per phase:
    # * a crash in the data phase durably wrote nothing -- the committed
    #   transaction is only visible after a replay;
    # * a crash after the data phase but before the clean marker has already
    #   applied the block (that is the barrier's point); a later replay
    #   re-applies it idempotently and only then clears the head.
    if phase == "data":
        assert _target_bytes(image, tmp_path)[:4] == b"OLD!"
    else:
        assert _target_bytes(image, tmp_path)[:4] == b"NEW!"
    # ...and a later recovery pass restores a checker-clean volume.
    assert (
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
        is True
    )
    assert probe_journal(str(image)).state is JournalState.CLEAN
    assert (
        subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode
        == 0
    )


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@needs_ext
@pytest.mark.parametrize("phase", ["data", "journal-superblock"])
def test_sigkill_at_each_checkpoint_phase_requires_recovery(tmp_path, phase):
    """A real process kill cannot make an incomplete checkpoint look clean."""
    image, _target_block, uuid, _journal_blocks, _records = _dirty_v3_image(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", _SIGKILL_CHILD, str(image), uuid.hex(), phase],
        cwd=str(repo_root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == -signal.SIGKILL, (result.stdout, result.stderr)
    probe = probe_journal(str(image))
    assert probe.state is JournalState.NEEDS_REPLAY, probe.message
    assert probe.journal_start != 0
    if phase == "data":
        assert _target_bytes(image, tmp_path)[:4] == b"OLD!"
    else:
        assert _target_bytes(image, tmp_path)[:4] == b"NEW!"
    assert (
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
        is True
    )
    assert probe_journal(str(image)).state is JournalState.CLEAN
    assert (
        subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode
        == 0
    )


@needs_ext
def test_recovery_after_checkpoint_interruption_succeeds(tmp_path):
    """A later recovery pass repairs an interrupted checkpoint volume."""
    image, _target_block, uuid, _journal_blocks, _records = _dirty_v3_image(tmp_path)
    with pytest.raises(JBD2CheckpointExecutionError):
        _coordinate_with_injection(image, uuid, fault_at_phase="journal-superblock")

    assert (
        recover_journal_on_copy(str(image), 0, native_coordinator=native_jbd2_recover)
        is True
    )
    probe = probe_journal(str(image))
    assert probe.state is JournalState.CLEAN, probe.message
    assert probe.journal_start == 0
    assert (
        subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode
        == 0
    )
    assert _target_bytes(image, tmp_path)[:4] == b"NEW!"


@needs_ext
def test_full_checkpoint_run_leaves_volume_clean(tmp_path):
    """The uninterrupted coordinator checkpoints the ring in one pass."""
    image, _target_block, uuid, _journal_blocks, _records = _dirty_v3_image(tmp_path)

    _coordinate_with_injection(image, uuid, fault_at_phase="__never__")

    # The coordinator checkpoints the ring but deliberately does not own the
    # ext4 RECOVER marker (see ``recover_journal_on_copy``); clear it the same
    # way the writer path does and require a checker-clean volume.
    assert _recover_flag_set(str(image), 0)
    _clear_recover_flag(str(image), 0)

    probe = probe_journal(str(image))
    assert probe.state is JournalState.CLEAN, probe.message
    assert probe.journal_start == 0
    assert (
        subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode
        == 0
    )
    assert _target_bytes(image, tmp_path)[:4] == b"NEW!"
