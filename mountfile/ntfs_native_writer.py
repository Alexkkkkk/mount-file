# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""NTFS write path gated by an authoritative ``$LogFile`` / ``$Volume`` check.

PR #504 writes NTFS images with ``ntfscp`` and "checks" them with
``ntfsfix -n``.  ``ntfsfix`` is a diagnostic helper: it never proves that the log
is clean, so :class:`mountfile.journal_safety.NTFSLogFileManager` (correctly)
treats it as non-authoritative and refuses.  This module replaces that dead end
with the parsers the project already owns:

* ``$Volume`` ``VOLUME_INFORMATION`` flags -- dirty bit must be clear;
* the ``$LogFile`` restart area -- ``RSTR`` page, USA fixups, client record and
  the ``clean dismount`` flag must all validate.

The verdict comes from :mod:`mountfile.ntfs_reader`, i.e. from validated on-disk
structures rather than from a command's exit code.  A volume that fails any check
is refused; there is no "repair the log" shortcut here.

Replay limitation (issue #386)
------------------------------
Redo/undo replay of ``$LogFile`` LFS records is **not** implemented.  Unlike the
ext4 path there is no utility in the repository's tool belt that replays NTFS
logs on a disposable copy (``ntfsfix`` only resets the dirty bit).  Therefore a
volume whose log needs replay is never modified.  ``recover="refuse"`` reports a
typed refusal; ``recover="copy"`` preserves the evidence in a content-addressed
quarantine copy plus a JSON marker and then refuses with
:class:`NtfsReplayQuarantined`.  Neither mode claims replay support.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from mountfile.ext4_native_writer import normalize_internal_path
from mountfile.native_write import (
    NativeWriteUnsafe,
    _atomic_replace,
    _fsync_path,
    _image_copy,
    _require_enabled,
    _require_source_unchanged,
    _require_tools,
    _run,
    _sha256_file,
    _unlink,
    native_write_enabled,
)
from mountfile.ntfs_mft import NtfsRecordError
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsMetadataError,
    parse_ntfs_volume_state,
)


class NtfsProfileUnsupported(NativeWriteUnsafe):
    """The image layout or state is outside the validated write profile."""


@dataclass(frozen=True)
class NtfsQuarantine:
    """Evidence-preserving copy of an image whose replay is not implemented."""

    source: str
    copy: str
    marker: str
    sha256: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "copy": self.copy,
            "marker": self.marker,
            "sha256": self.sha256,
            "reason": self.reason,
            "replay": "not_implemented",
        }


class NtfsReplayQuarantined(NtfsProfileUnsupported):
    """The dirty image was quarantined instead of being replayed or written."""

    def __init__(self, quarantine: NtfsQuarantine) -> None:
        super().__init__(
            "NTFS log replay is not implemented; dirty image was quarantined at "
            f"{quarantine.copy} (marker {quarantine.marker})"
        )
        self.quarantine = quarantine


def quarantine_ntfs_image(
    image_path: str, *, reason: str, offset: int = 0
) -> NtfsQuarantine:
    """Copy an unproven NTFS image next to itself and write a durable marker.

    The source is opened read-only and is never renamed or replaced.  The copy
    name is derived from the source digest, so repeated calls for the same
    content reuse the same evidence.  A same-named file whose digest differs is
    treated as tampering and refused.
    """

    source = os.path.abspath(os.fspath(image_path))
    if not os.path.isfile(source):
        raise NtfsProfileUnsupported(f"cannot quarantine unreadable image: {source}")
    try:
        digest = _sha256_file(source).hex()
    except OSError as exc:
        raise NtfsProfileUnsupported(
            f"cannot read image for quarantine: {exc}"
        ) from exc
    copy = f"{source}.mountfile-ntfs-quarantine-{digest[:12]}"
    marker = f"{copy}.json"
    if os.path.exists(copy):
        if _sha256_file(copy).hex() != digest:
            raise NtfsProfileUnsupported(
                f"quarantine copy {copy} does not match the source digest"
            )
    else:
        temporary = f"{copy}.partial-{os.getpid()}"
        try:
            with open(source, "rb") as reader, open(temporary, "wb") as writer:
                while chunk := reader.read(1 << 20):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, copy)
        finally:
            if os.path.exists(temporary):
                _unlink(temporary)
    record = {
        "schema": "mountfile-ntfs-quarantine-v1",
        "source": source,
        "copy": copy,
        "sha256": digest,
        "reason": reason,
        "offset": offset,
        "replay": "not_implemented",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary_marker = f"{marker}.partial-{os.getpid()}"
    try:
        with open(temporary_marker, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_marker, marker)
    finally:
        if os.path.exists(temporary_marker):
            _unlink(temporary_marker)
    _fsync_path(copy)
    return NtfsQuarantine(source, copy, marker, digest, reason)


@dataclass(frozen=True)
class NtfsLogGate:
    """Authoritative, read-only verdict on whether the volume may be written."""

    clean: bool
    reason: str
    volume_flags: int = 0
    log_clean_dismount: bool | None = None
    log_used: bool = True
    mirror_matches: bool = True

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "reason": self.reason,
            "volume_flags": self.volume_flags,
            "log_clean_dismount": self.log_clean_dismount,
            "log_used": self.log_used,
            "mirror_matches": self.mirror_matches,
        }


def probe_ntfs_log(image_path: str, offset: int = 0) -> NtfsLogGate:
    """Validate ``$Volume`` and ``$LogFile`` without mutating the image.

    Any parse failure, unknown flag or unsupported geometry yields
    ``clean=False`` -- the gate is fail-closed by construction.
    """

    try:
        size = os.path.getsize(image_path)
    except OSError as exc:
        return NtfsLogGate(False, f"image is not readable: {exc}")
    if offset < 0 or offset >= size:
        return NtfsLogGate(False, "offset is outside the image")
    volume_size = size - offset
    try:
        image = NativeNtfsMetadataImage(
            image_path, offset=offset, volume_size=volume_size
        )
    except (NtfsMetadataError, NtfsRecordError, ValueError, OSError) as exc:
        return NtfsLogGate(False, f"NTFS metadata is unusable: {exc}")
    try:
        profile = image.profile
        try:
            attributes = (
                image._attributes_for_record(  # noqa: SLF001 - validated helper
                    3, image.read_mft_record(3)
                )
            )
            volume_state = parse_ntfs_volume_state(attributes)
        except (NtfsMetadataError, NtfsRecordError, ValueError) as exc:
            return NtfsLogGate(False, f"$Volume state is unreadable: {exc}")
        restart = profile.logfile_restart
        if not volume_state.clean:
            return NtfsLogGate(
                False,
                f"NTFS volume is dirty (VOLUME_INFORMATION flags=0x{volume_state.flags:04x})",
                volume_state.flags,
                None if restart is None else restart.clean_dismount,
                restart is not None,
                profile.mirror_matches_mft,
            )
        if restart is None:
            # The stream has never carried a restart page: mke2fs-created and
            # never-mounted volumes look like this and are safe to write.
            return NtfsLogGate(
                True,
                "$LogFile has no restart page (log never used)",
                volume_state.flags,
                None,
                False,
                profile.mirror_matches_mft,
            )
        if not restart.clean_dismount:
            return NtfsLogGate(
                False,
                (
                    "$LogFile restart area is not marked as a clean dismount "
                    f"(flags=0x{restart.flags:04x}, current_lsn=0x{restart.current_lsn:x})"
                ),
                volume_state.flags,
                False,
                True,
                profile.mirror_matches_mft,
            )
        if not profile.mirror_matches_mft:
            return NtfsLogGate(
                False,
                "$MFT and $MFTMirr record 0 differ; the volume needs repair",
                volume_state.flags,
                True,
                True,
                False,
            )
        return NtfsLogGate(
            True,
            "$Volume is clean and $LogFile shows a clean dismount",
            volume_state.flags,
            True,
            True,
            True,
        )
    finally:
        image.close()


def ntfs_write_file_native(
    image_path: str, internal_path: str, data: bytes, *, offset: int = 0
) -> int:
    """Overwrite/create one file with ``ntfscp`` on the *copy* handed in.

    ``ntfscp`` has no offset option, so a nonzero partition offset is rejected
    instead of being silently applied to the wrong bytes.  Journal gating,
    copying and publication belong to :class:`NtfsNativeWriter`.
    """

    if offset:
        raise NtfsProfileUnsupported(
            "ntfscp cannot address a partition offset; only whole-volume images "
            "are supported by the bounded NTFS profile"
        )
    internal = normalize_internal_path(internal_path)
    _require_tools("ntfscp")
    payload = bytes(data)
    import tempfile

    handle, payload_path = tempfile.mkstemp(prefix="mountfile_ntfs_", suffix=".payload")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _run(["ntfscp", "-f", image_path, payload_path, "/" + internal.lstrip("/")])
    finally:
        _unlink(payload_path)
    return len(payload)


class NtfsNativeWriter:
    """Copy -> $LogFile/$Volume gate -> ntfscp -> re-gate -> atomic publish."""

    def __init__(self, *, require_flag: bool = True, recover: str = "refuse") -> None:
        if recover not in ("copy", "refuse"):
            raise ValueError("recover must be 'copy' or 'refuse'")
        self.require_flag = require_flag
        self.recover = recover

    def write_file(
        self, image_path: str, path: str, data: bytes, offset: int = 0
    ) -> int:
        if self.require_flag:
            _require_enabled()
        if offset:
            raise NtfsProfileUnsupported(
                "nonzero offsets are not supported by the bounded NTFS profile"
            )
        _require_tools("ntfscp")
        internal = normalize_internal_path(path)
        source = os.path.abspath(os.fspath(image_path))
        if not os.path.isfile(source):
            raise NtfsProfileUnsupported(f"image is not readable: {source}")
        try:
            source_identity = _sha256_file(source)
        except OSError as exc:
            raise NtfsProfileUnsupported(f"image is not readable: {exc}") from exc
        if self.recover == "copy":
            # Dirty volumes are never replayed or written: preserve evidence in
            # a quarantine copy and report a typed refusal.
            gate = probe_ntfs_log(source, 0)
            if not gate.clean:
                raise NtfsReplayQuarantined(
                    quarantine_ntfs_image(source, reason=gate.reason)
                )
        source_path, temporary = _image_copy(source)
        try:
            gate = probe_ntfs_log(temporary, 0)
            if not gate.clean:
                raise NtfsProfileUnsupported(
                    f"refusing to write a volume that needs log recovery: {gate.reason}"
                )
            written = ntfs_write_file_native(temporary, internal, data)
            after = probe_ntfs_log(temporary, 0)
            if not after.clean:
                raise NtfsProfileUnsupported(
                    f"volume is not clean after the write: {after.reason}"
                )
            _require_source_unchanged(source_path, source_identity)
            _atomic_replace(source_path, temporary)
            temporary = ""
            return written
        finally:
            if temporary:
                _unlink(temporary)


def logfile_gate_summary(image_path: str, offset: int = 0) -> dict:
    """Machine-readable gate result for the VFS adapter and the CLI."""

    gate = probe_ntfs_log(image_path, offset)
    enabled = native_write_enabled()
    summary = gate.as_dict()
    summary["native_write_enabled"] = enabled
    summary["writable"] = bool(enabled and gate.clean)
    summary["replay_implemented"] = False
    summary["quarantine_supported"] = True
    return summary


__all__ = [
    "NtfsLogGate",
    "NtfsNativeWriter",
    "NtfsProfileUnsupported",
    "NtfsQuarantine",
    "NtfsReplayQuarantined",
    "logfile_gate_summary",
    "ntfs_write_file_native",
    "probe_ntfs_log",
    "quarantine_ntfs_image",
]
