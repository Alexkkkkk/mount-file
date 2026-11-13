# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Fail-closed journal state checks used by transactional native writers.

The module deliberately does not mutate filesystem metadata.  Recovery is
performed by the filesystem's checked utility on a temporary image copy, then
this module verifies that the copy is clean before publication.
"""
from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from subprocess import CompletedProcess


class JournalState(str, Enum):
    CLEAN = "clean"
    NEEDS_REPLAY = "needs_replay"
    ERROR = "error"


@dataclass(frozen=True)
class JournalCheckResult:
    state: JournalState
    message: str = ""


class JournalSafetyError(RuntimeError):
    """A journal could not be proven safe for the next write step."""


CommandRunner = Callable[..., CompletedProcess[str]]


class Ext4JournalManager:
    """Read ext4 recovery metadata and verify externally replayed copies."""

    SUPERBLOCK_OFFSET = 1024
    EXT4_MAGIC = 0xEF53
    EXT4_FEATURE_INCOMPAT_RECOVER = 0x0004

    def __init__(self, image_path: str, offset: int = 0):
        self.image_path = Path(image_path)
        self.offset = int(offset)

    def check_journal(self, image_path: str | None = None) -> JournalCheckResult:
        """Return a conservative state without modifying the image.

        The ext4 RECOVER incompatibility bit is authoritative for whether the
        volume needs journal recovery. Unknown or truncated metadata is never
        treated as clean.
        """

        path = Path(image_path) if image_path is not None else self.image_path
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset + self.SUPERBLOCK_OFFSET)
                superblock = handle.read(1024)
        except (OSError, ValueError) as exc:
            return JournalCheckResult(JournalState.ERROR, str(exc))
        if len(superblock) < 1024:
            return JournalCheckResult(JournalState.ERROR, "short ext4 superblock")
        magic = struct.unpack_from("<H", superblock, 0x38)[0]
        if magic != self.EXT4_MAGIC:
            return JournalCheckResult(JournalState.ERROR, "not an ext4 filesystem")
        incompat = struct.unpack_from("<I", superblock, 0x60)[0]
        if incompat & self.EXT4_FEATURE_INCOMPAT_RECOVER:
            return JournalCheckResult(
                JournalState.NEEDS_REPLAY,
                "ext4 RECOVER flag is set; journal replay is required",
            )
        return JournalCheckResult(JournalState.CLEAN, "ext4 recovery flag is clear")

    def require_clean(self, image_path: str | None = None) -> None:
        result = self.check_journal(image_path)
        if result.state != JournalState.CLEAN:
            raise JournalSafetyError(result.message or result.state.value)

    def replay_on_copy(self, image_path: str, runner: CommandRunner) -> None:
        """Replay JBD2 through e2fsck on a temporary copy only.

        ``runner`` is supplied by the transactional writer so the command is
        executed without a shell and with the writer's timeout/error policy.
        """

        before = self.check_journal(image_path)
        if before.state == JournalState.ERROR:
            raise JournalSafetyError(before.message)
        runner(["e2fsck", "-fy", image_path], allowed={0, 1})
        self.require_clean(image_path)


class NTFSLogState(str, Enum):
    CLEAN = "clean"
    NEEDS_REPLAY = "needs_replay"
    ERROR = "error"


@dataclass(frozen=True)
class NTFSLogCheckResult:
    state: NTFSLogState
    message: str = ""


class NTFSLogFileManager:
    """Conservative $LogFile gate using ntfsfix's read-only check."""

    def check_logfile(
        self, image_path: str, runner: CommandRunner
    ) -> NTFSLogCheckResult:
        try:
            runner(["ntfsfix", "-n", image_path])
        except Exception as exc:  # runner normalizes command/exit failures
            return NTFSLogCheckResult(NTFSLogState.NEEDS_REPLAY, str(exc))
        return NTFSLogCheckResult(NTFSLogState.CLEAN, "$LogFile check passed")

    def require_clean(self, image_path: str, runner: CommandRunner) -> None:
        result = self.check_logfile(image_path, runner)
        if result.state != NTFSLogState.CLEAN:
            raise JournalSafetyError(result.message or result.state.value)


__all__ = [
    "Ext4JournalManager",
    "JournalCheckResult",
    "JournalSafetyError",
    "JournalState",
    "NTFSLogCheckResult",
    "NTFSLogFileManager",
    "NTFSLogState",
]
