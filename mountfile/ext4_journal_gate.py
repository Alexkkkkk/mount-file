# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Public fail-closed journal gate for native ext4 writes.

The canonical on-disk implementation lives in :mod:`ext4_native_writer`: it
reads the ext4 recovery flag and the real JBD2 superblock through the journal
inode.  This module exposes the small API requested by integrations without
copying the fragile filesystem-layout parser into a second implementation.
Recovery is never performed here; dirty images must be replayed on a disposable
copy by the transactional writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class JournalState(str, Enum):
    CLEAN = "clean"
    NEEDS_REPLAY = "needs_replay"
    ERROR = "error"


@dataclass(frozen=True)
class JournalCheckResult:
    state: JournalState
    message: str = ""


class JournalGateError(RuntimeError):
    """Raised by strict callers when journal metadata cannot be trusted."""


class Ext4JournalGate:
    """Read-only, fail-closed ext4/JBD2 gate.

    ``check`` never opens the image for writing.  It returns ``ERROR`` for
    malformed or unsupported metadata and ``NEEDS_REPLAY`` when the ext4
    ``RECOVER`` flag or an uncheckpointed JBD2 head requires recovery.
    """

    def __init__(self, image_path: str | Path, offset: int = 0):
        self.image_path = Path(image_path)
        self.offset = int(offset)

    def check(self) -> JournalCheckResult:
        try:
            # Local import avoids a module cycle: ext4_native_writer provides
            # the authoritative libext2fs/JBD2 implementation.
            from mountfile.ext4_native_writer import probe_journal

            result = probe_journal(str(self.image_path), self.offset)
            return JournalCheckResult(JournalState(result.state.value), result.message)
        except Exception as exc:
            # A journal parser must never turn an unknown state into CLEAN.
            return JournalCheckResult(
                JournalState.ERROR, f"unexpected: {type(exc).__name__}: {exc}"
            )

    def require_clean(self) -> None:
        result = self.check()
        if result.state is not JournalState.CLEAN:
            raise JournalGateError(result.message or result.state.value)


__all__ = [
    "Ext4JournalGate",
    "JournalCheckResult",
    "JournalGateError",
    "JournalState",
]
