# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Transactional, fail-closed orchestration for native image writers.

The engine deliberately knows nothing about a filesystem.  A backend receives
only an isolated copy and may prepare/recover it and apply one declarative plan.
The source image is never opened for writing; publication happens only after an
independent verifier accepts the copy.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Protocol

log = logging.getLogger(__name__)
CHUNK_SIZE = 1 << 20


class WriteOutcome(str, Enum):
    COMMITTED = "committed"
    REJECTED_PRECONDITION = "rejected_precondition"
    FAILED_WRITE = "failed_write"
    FAILED_VERIFY = "failed_verify"


@dataclass(frozen=True)
class WritePlan:
    """Declarative operation passed to an :class:`Engine`."""

    image: Path
    operation: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WriteReceipt:
    """Immutable JSON-serialisable record of one attempt."""

    outcome: WriteOutcome
    operation: str
    image: str
    original_sha256: str
    final_sha256: str | None
    duration_ms: int
    detail: str = ""


class Engine(Protocol):
    """Backend that can mutate only the supplied disposable copy."""

    def prepare(self, copy: Path) -> None:
        """Validate or recover the copy before mutation."""

    def apply(self, copy: Path, plan: WritePlan) -> None:
        """Apply the plan to the copy."""


class JournalWriter:
    """Append-only JSONL audit journal with durable individual records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, receipt: WriteReceipt) -> None:
        line = json.dumps(asdict(receipt), sort_keys=True, default=str) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = line.encode("utf-8")
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise OSError("audit journal short write")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)


class TransactionalWriter:
    """Run a native write on a copy and atomically publish verified output."""

    def __init__(
        self,
        engine: Engine,
        *,
        verify: Callable[[Path], bool],
        journal: JournalWriter | None = None,
        workdir: Path | None = None,
    ) -> None:
        self.engine = engine
        self.verify = verify
        self.journal = journal
        self.workdir = Path(workdir) if workdir is not None else None

    def execute(self, plan: WritePlan) -> WriteReceipt:
        started = time.monotonic()
        image = Path(plan.image)
        try:
            original_hash = _sha256(image)
        except OSError as exc:
            return self._reject(
                plan,
                "",
                started,
                WriteOutcome.REJECTED_PRECONDITION,
                f"source image: {exc}",
            )
        temp: Path | None = None
        try:
            try:
                lock = _exclusive_lock(image)
                lock.__enter__()
            except (OSError, RuntimeError) as exc:
                return self._reject(
                    plan,
                    original_hash,
                    started,
                    WriteOutcome.REJECTED_PRECONDITION,
                    f"lock: {exc}",
                )
            try:
                locked_hash = _sha256(image)
                if locked_hash != original_hash:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.REJECTED_PRECONDITION,
                        "source image changed while acquiring the lock",
                    )
                try:
                    temp = _snapshot(image, self.workdir)
                except OSError as exc:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.REJECTED_PRECONDITION,
                        f"snapshot: {exc}",
                    )
                if _sha256(image) != locked_hash:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.REJECTED_PRECONDITION,
                        "source image changed during snapshot",
                    )
                try:
                    self.engine.prepare(temp)
                    self.engine.apply(temp, plan)
                except Exception as exc:  # backend errors are receipt data
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.FAILED_WRITE,
                        repr(exc),
                    )
                try:
                    verified = bool(self.verify(temp))
                except Exception as exc:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.FAILED_VERIFY,
                        f"verification raised: {exc!r}",
                    )
                if not verified:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.FAILED_VERIFY,
                        "verification on copy failed",
                    )
                if _sha256(image) != locked_hash:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.REJECTED_PRECONDITION,
                        "source image changed during write",
                    )
                final_hash = _sha256(temp)
                try:
                    _atomic_replace(temp, image)
                except OSError as exc:
                    return self._reject(
                        plan,
                        original_hash,
                        started,
                        WriteOutcome.FAILED_WRITE,
                        f"atomic replace: {exc}",
                    )
                temp = None
                return self._commit(plan, original_hash, final_hash, started)
            finally:
                lock.__exit__(None, None, None)
        finally:
            if temp is not None:
                with contextlib.suppress(OSError):
                    temp.unlink()

    def _commit(
        self,
        plan: WritePlan,
        before: str,
        after: str,
        started: float,
    ) -> WriteReceipt:
        receipt = WriteReceipt(
            outcome=WriteOutcome.COMMITTED,
            operation=plan.operation,
            image=str(plan.image),
            original_sha256=before,
            final_sha256=after,
            duration_ms=_duration_ms(started),
        )
        self._emit(receipt)
        return receipt

    def _reject(
        self,
        plan: WritePlan,
        before: str,
        started: float,
        outcome: WriteOutcome,
        detail: str,
    ) -> WriteReceipt:
        receipt = WriteReceipt(
            outcome=outcome,
            operation=plan.operation,
            image=str(plan.image),
            original_sha256=before,
            final_sha256=None,
            duration_ms=_duration_ms(started),
            detail=detail,
        )
        self._emit(receipt)
        return receipt

    def _emit(self, receipt: WriteReceipt) -> None:
        if self.journal is not None:
            try:
                self.journal.append(receipt)
            except OSError:
                # Do not report a published filesystem mutation as failed after
                # os.replace has already completed.  The returned receipt remains
                # authoritative; audit storage can be repaired independently.
                log.exception("could not append native-write audit receipt")


@contextlib.contextmanager
def _exclusive_lock(image: Path) -> Iterator[None]:
    """Take a non-blocking advisory lock beside the image."""
    lock_path = image.with_name(image.name + ".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    f"image {image} is locked by another writer"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"image {image} is locked by another writer"
                ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _snapshot(image: Path, workdir: Path | None) -> Path:
    if not image.is_file():
        raise OSError(f"image is not a regular file: {image}")
    directory = workdir or image.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{image.name}.", suffix=".staged", dir=directory
    )
    os.close(fd)
    staged = Path(name)
    try:
        shutil.copy2(image, staged)
    except Exception:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise
    return staged


def _atomic_replace(staged: Path, image: Path) -> None:
    """Flush staged bytes and atomically replace the source in its directory."""

    # fsync() on a read-only fd fails with EBADF on Windows (works on POSIX),
    # so the flush opens the staged copy read-write.
    fd = os.open(staged, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(staged, image)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            directory_fd = os.open(image.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "Engine",
    "JournalWriter",
    "TransactionalWriter",
    "WriteOutcome",
    "WritePlan",
    "WriteReceipt",
]
