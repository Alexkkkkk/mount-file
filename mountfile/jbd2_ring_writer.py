# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Bounded application of a planned classic JBD2 ring transaction.

``mountfile.jbd2_writer`` encodes transaction records and plans their ring
placement, but deliberately does not write them.  That gap made "process kill
at every JBD2 transaction phase" impossible to drive: a caller could place a
kill only at the coarse ``mutation:*`` boundaries of the staged writer, never
between the descriptor, data, revoke and commit records of one transaction
(tracker #510, item B8).

This module applies an *already validated* :class:`JBD2RingPlan` and emits one
stable boundary per record phase through the shared native-write fault seam
(``JBD2_RING_WRITE_FAULT_POINTS``), so an acceptance harness can terminate the
process between two durable states of a single transaction.

It still does not publish a filesystem: the caller owns block IO (``write_block``)
and durability (``flush``), and the returned ``next_head`` must only be
persisted after ``jbd2:commit:done``.  A kill at any earlier boundary therefore
leaves a plan whose commit record is absent from the journal -- a transaction
the recovery path already ignores -- and never a half-advanced ring head.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .jbd2_writer import JBD2RingPlan, JBD2WriteProfileError
from .native_write import _fault

#: Durable phase boundaries of one ring transaction, in emission order.
#: Names are stable so acceptance harnesses never guess internal functions.
JBD2_RING_WRITE_FAULT_POINTS = (
    "jbd2:descriptor:start",
    "jbd2:descriptor:done",
    "jbd2:data:start",
    "jbd2:data:done",
    "jbd2:revoke:start",
    "jbd2:revoke:done",
    "jbd2:commit:start",
    "jbd2:commit:done",
    "jbd2:ring:head",
    "jbd2:flush",
)

_JBD2_MAGIC = 0xC03B3998
_JBD2_DESCRIPTOR = 1
_JBD2_COMMIT = 2
_JBD2_REVOKE = 5
_SUPPORTED_BLOCK_SIZES = (1024, 2048, 4096)

#: Phase name -> (start boundary, done boundary).
_PHASE_BOUNDARIES: Mapping[str, tuple[str, str]] = {
    "descriptor": ("jbd2:descriptor:start", "jbd2:descriptor:done"),
    "data": ("jbd2:data:start", "jbd2:data:done"),
    "revoke": ("jbd2:revoke:start", "jbd2:revoke:done"),
    "commit": ("jbd2:commit:start", "jbd2:commit:done"),
}

#: Canonical order of the four record families inside one transaction.
_PHASE_ORDER = ("descriptor", "data", "revoke", "commit")


@dataclass(frozen=True)
class Jbd2RingWriteResult:
    """Outcome of applying one ring plan.

    ``committed`` is true only when the commit record reached the caller's
    durable ``flush``; a caller that persists ``next_head`` while this is false
    would skip the live records of an uncommitted transaction.
    """

    phases: tuple[str, ...]
    written_blocks: tuple[int, ...]
    committed: bool
    flushed: bool
    next_head: int
    used_blocks: int
    free_blocks: int


def classify_classic_jbd2_record(record: object, block_size: int) -> str:
    """Return the record family of one journal block.

    Control records (descriptor, commit, revoke) are recognised by their
    magic and type word.  Everything else is *data*: a data block in a JBD2
    transaction is a verbatim copy of a filesystem block and therefore carries
    no journal header of its own, so an absent magic is the normal case, not a
    malformed record.
    """

    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    if not isinstance(record, (bytes, bytearray)):
        raise JBD2WriteProfileError("JBD2 record must be bytes")
    payload = bytes(record)
    if len(payload) != block_size:
        raise JBD2WriteProfileError("JBD2 record must fill exactly one block")
    magic = struct.unpack_from(">I", payload, 0)[0]
    if magic != _JBD2_MAGIC:
        return "data"
    block_type = struct.unpack_from(">I", payload, 4)[0]
    if block_type == _JBD2_DESCRIPTOR:
        return "descriptor"
    if block_type == _JBD2_COMMIT:
        return "commit"
    if block_type == _JBD2_REVOKE:
        return "revoke"
    return "data"


def group_classic_jbd2_ring_phases(
    plan: JBD2RingPlan, *, block_size: int
) -> tuple[tuple[str, tuple[tuple[int, bytes], ...]], ...]:
    """Group planned writes into record phases, refusing a reordered plan.

    A journal block is only meaningful once the descriptor that tags it is on
    disk, and a transaction is only complete once its commit block is.  A plan
    whose records arrive out of that order is refused instead of being written
    in a shape no JBD2 recovery path can honour.
    """

    if not isinstance(plan, JBD2RingPlan):
        raise JBD2WriteProfileError("JBD2 ring plan has an invalid type")
    writes: Sequence[tuple[int, bytes]] = plan.writes
    if not writes:
        raise JBD2WriteProfileError("JBD2 ring plan carries no records")

    grouped: dict[str, list[tuple[int, bytes]]] = {name: [] for name in _PHASE_ORDER}
    previous = -1
    for block, record in writes:
        family = classify_classic_jbd2_record(record, block_size)
        index = _PHASE_ORDER.index(family)
        if index < previous:
            raise JBD2WriteProfileError(
                f"JBD2 ring plan places a {family} record after "
                f"{_PHASE_ORDER[previous]}"
            )
        previous = index
        grouped[family].append((block, record))

    commits = grouped["commit"]
    if len(commits) != 1:
        raise JBD2WriteProfileError("JBD2 transaction must carry exactly one commit")
    if commits[0][0] != writes[-1][0]:
        raise JBD2WriteProfileError("JBD2 commit record must be the last write")
    if not grouped["descriptor"]:
        raise JBD2WriteProfileError("JBD2 transaction has no descriptor record")

    return tuple((name, tuple(grouped[name])) for name in _PHASE_ORDER if grouped[name])


def apply_classic_jbd2_ring_transaction(
    plan: JBD2RingPlan,
    *,
    block_size: int,
    write_block: Callable[[int, bytes], None],
    flush: Callable[[], None] | None = None,
) -> Jbd2RingWriteResult:
    """Write a validated ring plan, emitting one boundary per record phase.

    ``write_block`` receives the absolute journal block number and the full
    block payload; it must make the write durable on its own or rely on
    ``flush``.  The ring head is reported back, never persisted here: a caller
    that stores it before ``jbd2:commit:done`` would advertise an uncommitted
    transaction.
    """

    if block_size not in _SUPPORTED_BLOCK_SIZES:
        raise JBD2WriteProfileError("unsupported JBD2 block size")
    if not callable(write_block):
        raise JBD2WriteProfileError("JBD2 write_block must be callable")
    if flush is not None and not callable(flush):
        raise JBD2WriteProfileError("JBD2 flush must be callable or None")

    phases = group_classic_jbd2_ring_phases(plan, block_size=block_size)
    emitted: list[str] = []
    written: list[int] = []
    committed = False

    for name, records in phases:
        start, done = _PHASE_BOUNDARIES[name]
        _fault(start)
        emitted.append(start)
        for block, payload in records:
            write_block(block, payload)
            written.append(block)
        _fault(done)
        emitted.append(done)
        if name == "commit":
            committed = True

    _fault("jbd2:ring:head")
    emitted.append("jbd2:ring:head")
    flushed = False
    if flush is not None:
        _fault("jbd2:flush")
        emitted.append("jbd2:flush")
        flush()
        flushed = True
    else:
        _fault("jbd2:flush")
        emitted.append("jbd2:flush")

    return Jbd2RingWriteResult(
        phases=tuple(name for name, _ in phases),
        written_blocks=tuple(written),
        committed=committed,
        flushed=flushed,
        next_head=plan.next_head,
        used_blocks=plan.used_blocks,
        free_blocks=plan.free_blocks,
    )


__all__ = [
    "JBD2_RING_WRITE_FAULT_POINTS",
    "Jbd2RingWriteResult",
    "apply_classic_jbd2_ring_transaction",
    "classify_classic_jbd2_record",
    "group_classic_jbd2_ring_phases",
]
