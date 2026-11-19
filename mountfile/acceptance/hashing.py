# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Source identity and atomic publication used by the acceptance probes.

Item D4 of tracker #510 asks that a concurrent modification of the source image
be detected instead of silently overwritten; item D3 asks that a refusal or an
injected failure leave the source byte-identical.  Both are properties of the
publication primitive, so the primitive lives here as production code and the
probes exercise it directly instead of asserting that some other module exists.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mountfile.safety import RefusalCode, RefusalError

#: Fault-seam names this primitive emits.  They are a subset of
#: ``mountfile.native_write.FAULT_POINTS`` on purpose: the acceptance harness
#: must speak the same vocabulary as the runtime writer, or a probe could pass
#: on names no production code ever emits.
PUBLISH_POINTS = (
    "mutation:start",
    "mutation:flush",
    "mutation:done",
    "publish:start",
    "publish:done",
)


class AcceptanceError(RuntimeError):
    """Base error for acceptance probes."""


class SourceChanged(RefusalError):
    """The source image changed between capture and publication."""

    def __init__(self, message: str) -> None:
        super().__init__(message, RefusalCode.CONCURRENT_CHANGE)


class PublishRefused(RefusalError):
    """Publication was refused before the target was touched."""

    def __init__(self, message: str, code: RefusalCode = RefusalCode.UNSAFE) -> None:
        super().__init__(message, code)


def sha256_bytes(payload: bytes) -> str:
    """Return the hex SHA-256 of ``payload``."""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Return the hex SHA-256 of the file at ``path``."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    """A snapshot of the source image taken before any mutation."""

    path: Path
    sha256: str
    size: int

    @classmethod
    def capture(cls, path: str | Path) -> SourceIdentity:
        location = Path(path)
        stat = location.stat()
        return cls(path=location, sha256=sha256_file(location), size=stat.st_size)

    def assert_unchanged(self) -> None:
        """Raise :class:`SourceChanged` when the source moved under us."""

        current = SourceIdentity.capture(self.path)
        if current.sha256 != self.sha256:
            raise SourceChanged(
                f"source changed during the operation: {self.sha256} -> {current.sha256}"
            )
        if current.size != self.size:
            raise SourceChanged(
                f"source size changed during the operation: {self.size} -> {current.size}"
            )

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "sha256": self.sha256, "size": self.size}


def _emit(hook: Callable[[str], None] | None, name: str) -> None:
    if hook is not None:
        hook(name)


def _fsync_file(path: Path) -> None:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def publish_atomically(
    staged: str | Path,
    target: str | Path,
    identity: SourceIdentity,
    *,
    expected_sha256: str | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> str:
    """Replace ``target`` with ``staged`` only after every check passes.

    Order matters and is the whole point of D3/D4: the staged digest is verified
    first, then the source identity, then the rename happens.  Any refusal
    raises before ``os.replace``, so the target keeps its original bytes.
    """

    staged_path = Path(staged)
    target_path = Path(target)

    if not staged_path.is_file():
        raise PublishRefused(f"staged file is missing: {staged_path}")

    _emit(fault_hook, "mutation:start")
    actual = sha256_file(staged_path)
    if expected_sha256 is not None and actual != expected_sha256:
        raise PublishRefused(
            f"staged digest mismatch: {actual} != {expected_sha256}",
            RefusalCode.INTEGRITY_CHECK_FAILED,
        )

    identity.assert_unchanged()

    if staged_path.parent.resolve() != target_path.parent.resolve():
        raise PublishRefused(
            "staged file must live beside the target for an atomic rename"
        )

    _emit(fault_hook, "mutation:flush")
    _fsync_file(staged_path)
    _emit(fault_hook, "mutation:done")

    _emit(fault_hook, "publish:start")
    os.replace(staged_path, target_path)
    _fsync_directory(target_path.parent)
    _emit(fault_hook, "publish:done")

    final = sha256_file(target_path)
    if final != actual:
        raise PublishRefused(
            f"published image does not match the staged digest: {final} != {actual}",
            RefusalCode.INTEGRITY_CHECK_FAILED,
        )
    return final


def is_within(path: str | Path, root: str | Path) -> bool:
    """True when ``path`` resolves inside ``root`` (no traversal escape)."""

    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True
