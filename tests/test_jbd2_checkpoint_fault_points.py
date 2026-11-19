# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 D1/B8: durable-boundary coverage for the JBD2 checkpoint executor.

The checkpoint executor used to be a black box between the declared writer
boundaries: a crash *inside* ``execute_classic_jbd2_checkpoint`` was invisible
to the fault seam, so "process-kill at every JBD2 phase" could not be driven.
The executor now emits its durable boundaries through the shared native-write
seam (``JBD2_CHECKPOINT_FAULT_POINTS``), and this module is the per-phase
coverage:

* a successful checkpoint emits every declared boundary, in order;
* a termination at every boundary before ``checkpoint:marker:done`` leaves the
  journal superblock untouched -- the volume still requires recovery, exactly
  as before the checkpoint started;
* from ``checkpoint:marker:done`` on, the clean marker is on disk: stale ring
  records are unreachable and recovery is no longer required.

The kill is simulated in-process with ``KeyboardInterrupt`` -- a
``BaseException``, so the executor's ``except Exception`` barrier cannot
swallow it.  That mirrors the phase-kill contract of the NTFS replay tests.
"""

from __future__ import annotations

import io
import struct

import pytest

from mountfile.jbd2_checkpoint import (
    JBD2_CHECKPOINT_FAULT_POINTS,
    execute_classic_jbd2_checkpoint,
    plan_classic_jbd2_checkpoint,
)
from mountfile.native_write import fault_hook_installed, fault_point_trace

BLOCK_SIZE = 1024
FS_BLOCKS = 100
JOURNAL_BLOCK = 40
DATA_BLOCK = 8
UUID = b"0123456789abcdef"


def journal_superblock(*, sequence=17, start=5, block_size=BLOCK_SIZE):
    raw = bytearray(block_size)
    struct.pack_into(">III", raw, 0, 0xC03B3998, 3, sequence)
    struct.pack_into(">IIIII", raw, 12, block_size, 32, 1, sequence, start)
    raw[48:64] = UUID
    return bytes(raw)


def make_plan(**kwargs):
    options = dict(
        superblock=journal_superblock(),
        filesystem_block_size=BLOCK_SIZE,
        filesystem_uuid=UUID,
        journal_blocks=32,
        filesystem_blocks=FS_BLOCKS,
        journal_physical_blocks=[JOURNAL_BLOCK],
        data_writes={DATA_BLOCK: b"X" * BLOCK_SIZE},
        journal_superblock_physical_block=JOURNAL_BLOCK,
    )
    options.update(kwargs)
    return plan_classic_jbd2_checkpoint(**options)


def _fresh_stream() -> io.BytesIO:
    stream = io.BytesIO(b"\x00" * (FS_BLOCKS * BLOCK_SIZE))
    stream.seek(JOURNAL_BLOCK * BLOCK_SIZE)
    stream.write(journal_superblock())
    stream.seek(0)
    return stream


def _marker(stream: io.BytesIO) -> bytes:
    stream.seek(JOURNAL_BLOCK * BLOCK_SIZE)
    return stream.read(BLOCK_SIZE)


def test_declared_checkpoint_boundaries_are_unique_and_namespaced():
    assert len(JBD2_CHECKPOINT_FAULT_POINTS) == len(set(JBD2_CHECKPOINT_FAULT_POINTS))
    assert all(
        point.startswith("checkpoint:") for point in JBD2_CHECKPOINT_FAULT_POINTS
    )
    assert JBD2_CHECKPOINT_FAULT_POINTS[-1] == "checkpoint:marker:sync"


def test_successful_checkpoint_emits_every_boundary_in_order():
    plan = make_plan()
    stream = _fresh_stream()
    trace = fault_point_trace(
        execute_classic_jbd2_checkpoint,
        stream,
        plan,
        block_size=BLOCK_SIZE,
        sync=lambda: None,
    )
    # One data block -> exactly one data:start/data:done pair.
    assert tuple(trace) == JBD2_CHECKPOINT_FAULT_POINTS


@pytest.mark.parametrize("point", JBD2_CHECKPOINT_FAULT_POINTS)
def test_termination_at_every_checkpoint_boundary(point):
    """Kill the executor at each durable boundary and inspect the volume."""
    plan = make_plan()
    old_marker = journal_superblock()
    stream = _fresh_stream()

    def hook(name):
        if name == point:
            raise KeyboardInterrupt

    fault_hook_installed(hook)
    try:
        with pytest.raises(KeyboardInterrupt):
            execute_classic_jbd2_checkpoint(
                stream, plan, block_size=BLOCK_SIZE, sync=lambda: None
            )
    finally:
        fault_hook_installed(None)

    stream.seek(0)
    marker = _marker(stream)
    if point in ("checkpoint:marker:done", "checkpoint:marker:sync"):
        # The clean marker is on disk: stale ring records are unreachable and
        # recovery is no longer required.
        assert marker == plan.writes[-1].data
    else:
        # The old dirty marker is untouched: recovery remains required, the
        # checkpoint made no durable claim.
        assert marker == old_marker, f"crash at {point} published the clean marker"

    data_block = stream.getvalue()[
        DATA_BLOCK * BLOCK_SIZE : (DATA_BLOCK + 1) * BLOCK_SIZE
    ]
    if point in ("checkpoint:data:start",):
        # Killed before the data block was written.
        assert data_block == b"\x00" * BLOCK_SIZE
    else:
        assert data_block == plan.writes[0].data
