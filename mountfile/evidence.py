# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only hashing and sidecar manifests for single-file evidence inputs.

This module hashes source-file bytes only. It does not identify container
formats, validate logical media streams, or establish a complete chain of custody.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024


class EvidenceManifestError(ValueError):
    """Raised when a safe evidence manifest cannot be created."""


def _utc_from_ns(timestamp_ns: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
        int(info.st_dev),
        int(info.st_ino),
    )


def create_evidence_manifest(
    image_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Hash one regular file read-only and create a non-overwriting JSON sidecar.

    The manifest is created with exclusive-create semantics. The input is opened
    read-only, read in bounded chunks, and checked with before/after ``fstat``.
    It records an opaque-file byte hash: no format detection or media-stream
    verification is performed.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
        raise EvidenceManifestError("chunk_size must be an integer")
    if not 1 <= chunk_size <= MAX_CHUNK_SIZE:
        raise EvidenceManifestError(
            "chunk_size must be between 1 byte and %d bytes" % MAX_CHUNK_SIZE
        )

    source = os.path.abspath(os.fspath(image_path))
    destination = os.path.abspath(os.fspath(output_path))
    if source == destination:
        raise EvidenceManifestError("manifest output must be separate from the source")
    if os.path.islink(source):
        raise EvidenceManifestError("symbolic-link sources are not accepted")
    if os.path.lexists(destination):
        try:
            if os.path.samefile(source, destination):
                raise EvidenceManifestError(
                    "manifest output must not refer to the source file"
                )
        except OSError:
            pass
        raise EvidenceManifestError(
            f"manifest output already exists; refusing to overwrite: {destination}"
        )
    parent = os.path.dirname(destination) or os.curdir
    if not os.path.isdir(parent):
        raise EvidenceManifestError(
            f"manifest output directory does not exist: {parent}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    noatime_flag = getattr(os, "O_NOATIME", 0)
    try:
        try:
            source_fd = (
                os.open(source, flags | noatime_flag)
                if noatime_flag
                else os.open(source, flags)
            )
            noatime_applied = bool(noatime_flag)
        except OSError as exc:
            unsupported = {errno.EPERM, errno.EINVAL}
            if hasattr(errno, "EOPNOTSUPP"):
                unsupported.add(errno.EOPNOTSUPP)
            if not noatime_flag or exc.errno not in unsupported:
                raise
            source_fd = os.open(source, flags)
            noatime_applied = False
    except OSError as exc:
        raise EvidenceManifestError(f"cannot open source read-only: {exc}") from exc

    started_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceManifestError("source must be a regular file")
        digest = hashlib.sha256()
        bytes_read = 0
        try:
            while True:
                chunk = os.read(source_fd, chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
        except OSError as exc:
            raise EvidenceManifestError(f"cannot read source: {exc}") from exc
        after = os.fstat(source_fd)
    finally:
        os.close(source_fd)

    if bytes_read != before.st_size or _stat_identity(before) != _stat_identity(after):
        raise EvidenceManifestError(
            "source changed while it was being hashed; no manifest written"
        )

    completed_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    result: dict[str, Any] = {
        "schema": "mountfile.evidence-manifest/v1",
        "created_at_utc": completed_at,
        "tool": {"name": "Mount File", "version": __version__},
        "source": {
            "path": source,
            "name": Path(source).name,
            "size_bytes": int(before.st_size),
            "modified_at_utc": _utc_from_ns(
                int(getattr(before, "st_mtime_ns", before.st_mtime * 1_000_000_000))
            ),
            "sha256": digest.hexdigest(),
        },
        "hashing": {
            "algorithm": "SHA-256",
            "scope": "all bytes of this single source file",
            "format_detection": "not performed",
            "logical_media_stream_hash": None,
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "chunk_size_bytes": chunk_size,
            "opened_read_only": True,
            "noatime_requested": bool(noatime_flag),
            "noatime_applied": noatime_applied,
            "source_stat_unchanged_during_read": True,
        },
        "limitations": [
            "This manifest records a source-file byte hash only; it does not identify or validate an image/container format.",
            "No logical media-stream hash, multi-segment ordering, or container integrity verification is performed.",
            "This is not a complete chain-of-custody record and does not replace a hardware write blocker or independent verification.",
        ],
    }
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        output_fd = os.open(destination, output_flags, 0o600)
    except OSError as exc:
        raise EvidenceManifestError(
            f"cannot create manifest without overwriting: {exc}"
        ) from exc

    try:
        with os.fdopen(output_fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(destination)
        raise EvidenceManifestError(f"cannot write manifest: {exc}") from exc
    return result
