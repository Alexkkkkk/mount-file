# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only per-filesystem eligibility and metadata validation (preflight).

Issue #182 requires each filesystem family to be validated *before* browsing or
mutation: which bounded provider claims the selected extent, which profile
metadata gates pass, why a read is refused, and whether a native write profile
is published at all. This module answers those questions without modifying the
image:

* detection mirrors :func:`mountfile.vfs.open_image_provider` signature order,
  so a preflight result never disagrees with the real router about the selected
  family;
* metadata gates are produced by the same bounded probes the providers use, so
  preflight is not a second, weaker parser that could accept more than the
  reader;
* read eligibility is confirmed by the production routing call with the legacy
  ``pytsk3`` fallback disabled, then the provider is closed immediately;
* write eligibility is reported from :mod:`mountfile.capabilities` only. This
  module never enables a writer, never stages bytes and never writes to the
  image. A published writer profile is reported, not authorized.

Gate states are three-valued on purpose: ``pass`` means the gate was evaluated
and satisfied, ``fail`` means a deliberate refusal was produced, and
``unknown`` means the bounded probe could not evaluate it. Reporting is
fail-closed: an ``unknown`` gate can never make ``eligible_for_read`` true.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from mountfile.capabilities import (
    get_native_capability_profile,
    get_native_capability_status,
)
from mountfile.safety import RefusalCode, RefusalError
from mountfile.vfs import (
    LegacyFallbackAvailableError,
    RecognizedFilesystemError,
    TskError,
    _has_exfat_boot_magic,
    _has_ext_superblock_magic,
    _has_fat_boot_magic,
    _has_ntfs_boot_magic,
    legacy_tsk_enabled,
    open_image_provider,
    probe_exfat_profile,
    probe_ext_profile,
    probe_fat_profile,
    probe_ntfs_metadata,
    probe_ntfs_profile,
    volume_size_for_offset,
)

#: Version of the serialisable preflight payload. Bump on incompatible changes.
PREFLIGHT_SCHEMA_VERSION = 1

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_UNKNOWN = "unknown"

#: exFAT boot-region ``VolumeFlags`` bits that make a volume unusable natively.
_EXFAT_VOLUME_DIRTY = 0x0002
_EXFAT_MEDIA_FAILURE = 0x0004
_EXFAT_CLEAR_TO_ZERO = 0x0008

#: Native writer profiles published for a filesystem, if any. A mapping here is
#: documentation of what the capability matrix publishes; it is not permission.
_NATIVE_WRITE_PROFILE_BY_FILESYSTEM = {
    "FAT12": "fat12-16-native-rw",
    "FAT16": "fat12-16-native-rw",
    "FAT32": "fat32-native-rw",
    "ext2": "ext2-native-rw",
    "exFAT": "exfat-native-rw",
}

_DEFAULT_WRITE_REFUSAL = (
    "preflight never authorizes mutation; select the bounded writer explicitly "
    "(CLI put/mkdir/mv/rm or UniversalFSRW) and re-validate the image state"
)


def _checked_int(value: Any, name: str) -> int:
    """Return ``value`` as a real integer; reject booleans and floats."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RefusalError(
            f"{name} must be an integer", RefusalCode.RANGE_OUT_OF_BOUNDS
        )
    return value


def _refusal(code: RefusalCode, message: str) -> dict[str, str]:
    return RefusalError(message, code).to_dict()


def _gate(
    gate_id: str,
    kind: str,
    state: str,
    detail: str = "",
    refusal: dict[str, str] | None = None,
) -> PreflightGate:
    return PreflightGate(gate_id, kind, state, detail, refusal)


@dataclass(frozen=True)
class PreflightGate:
    """One evaluated image-state, feature or operation gate."""

    gate_id: str
    kind: str
    state: str
    detail: str = ""
    refusal: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.gate_id,
            "kind": self.kind,
            "state": self.state,
            "detail": self.detail,
            "refusal": None if self.refusal is None else dict(self.refusal),
        }


@dataclass
class PreflightReport:
    """Bounded, read-only eligibility report for one selected filesystem."""

    image: str
    offset: int
    volume_size: int | None
    schema_version: int = PREFLIGHT_SCHEMA_VERSION
    filesystem: str = "unknown"
    detection: str = "signature-scan"
    backend: str | None = None
    profile_id: str | None = None
    profile_interface: str = "vfs"
    read_capabilities: list[str] = field(default_factory=list)
    write_capabilities: list[str] = field(default_factory=list)
    native_write_profile: str | None = None
    write_enabled: bool = False
    write_refusal: dict[str, str] | None = None
    eligible_for_read: bool = False
    eligible_for_write: bool = False
    refusal: dict[str, str] | None = None
    gates: list[PreflightGate] = field(default_factory=list)
    operations: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, JSON-serialisable report."""
        return {
            "schema_version": self.schema_version,
            "image": self.image,
            "offset": self.offset,
            "volume_size": self.volume_size,
            "filesystem": self.filesystem,
            "detection": self.detection,
            "backend": self.backend,
            "profile_id": self.profile_id,
            "profile_interface": self.profile_interface,
            "read_capabilities": list(self.read_capabilities),
            "write_capabilities": list(self.write_capabilities),
            "native_write_profile": self.native_write_profile,
            "write_enabled": self.write_enabled,
            "write_refusal": (
                None if self.write_refusal is None else dict(self.write_refusal)
            ),
            "eligible_for_read": self.eligible_for_read,
            "eligible_for_write": self.eligible_for_write,
            "refusal": None if self.refusal is None else dict(self.refusal),
            "gates": [gate.to_dict() for gate in self.gates],
            "operations": self.operations,
            "notes": list(self.notes),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialise the report deterministically (sorted keys)."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True
        )


def _detect_family(report: PreflightReport) -> str:
    """Mirror the production signature order and return a display family name.

    The helpers are the same ones :func:`mountfile.vfs.open_image_provider`
    uses, so preflight cannot route an image to a different family than the
    reader would. Detection only inspects bounded signature fields.
    """
    image, offset, volume_size = report.image, report.offset, report.volume_size
    if _has_ext_superblock_magic(image, offset, volume_size):
        report.detection = "ext-superblock-magic"
        return "ext"
    if _has_exfat_boot_magic(image, offset, volume_size):
        report.detection = "exfat-boot-magic"
        return "exFAT"
    if _has_ntfs_boot_magic(image, offset, volume_size):
        report.detection = "ntfs-boot-magic"
        return "NTFS"
    if _has_fat_boot_magic(image, offset, volume_size):
        report.detection = "fat-bpb-magic"
        return "FAT"
    report.detection = "none"
    return "unknown"


def _probe_ext(report: PreflightReport) -> None:
    """Evaluate the ext2/ext3/ext4 profile gates from the bounded superblock."""
    profile = probe_ext_profile(
        report.image, offset=report.offset, volume_size=report.volume_size
    )
    report.filesystem = profile.fs_type
    report.notes.append(
        "ext geometry: block_size=%d blocks=%d inodes=%d inode_size=%d"
        % (
            profile.block_size,
            profile.blocks_count,
            profile.inodes_count,
            profile.inode_size,
        )
    )
    report.notes.append(
        f"ext features: compat=0x{profile.feature_compat:X} incompat=0x{profile.feature_incompat:X} ro_compat=0x{profile.feature_ro_compat:X}"
    )

    if profile.clean_unmounted:
        report.gates.append(
            _gate(
                "clean-volume",
                "image-state",
                GATE_PASS,
                "superblock reports a clean, unmounted volume",
            )
        )
    else:
        reason = "ext volume is dirty or requires journal recovery"
        report.gates.append(
            _gate(
                "clean-volume",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.DIRTY_OR_RECOVERY_REQUIRED, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.DIRTY_OR_RECOVERY_REQUIRED, reason)

    unknown_features = (
        profile.unknown_incompat_features or profile.unknown_ro_compat_features
    )
    if not unknown_features:
        report.gates.append(
            _gate(
                "known-features",
                "feature",
                GATE_PASS,
                "no unknown incompat/ro_compat feature bits are set",
            )
        )
    else:
        reason = f"unknown ext feature bits are set (incompat=0x{profile.unknown_incompat_features:X} ro_compat=0x{profile.unknown_ro_compat_features:X})"
        report.gates.append(
            _gate(
                "known-features",
                "feature",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason)

    if profile.fs_type == "ext2":
        if profile.phase1_ext2_profile_candidate and not profile.write_gate_reasons:
            report.gates.append(
                _gate(
                    "ext2-allowlist-profile",
                    "feature",
                    GATE_PASS,
                    "superblock matches the bounded ext2 allowlist profile",
                )
            )
        else:
            detail = "; ".join(profile.write_gate_reasons) or (
                "superblock is outside the bounded ext2 allowlist profile"
            )
            report.gates.append(
                _gate(
                    "ext2-allowlist-profile",
                    "feature",
                    GATE_UNKNOWN,
                    detail,
                )
            )
        report.notes.append(
            "an ext2 allowlist match is a profile hint only; the writer still "
            "validates every inode, block and directory before mutation"
        )
        return

    journal_state = (
        "journal recovery is required"
        if profile.journal_recovery_needed
        else "no journal replay is pending"
    )
    report.gates.append(
        _gate(
            "jbd2-no-replay",
            "image-state",
            GATE_PASS if not profile.journal_recovery_needed else GATE_FAIL,
            journal_state,
            (
                None
                if not profile.journal_recovery_needed
                else _refusal(
                    RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
                    "ext journal requires replay; replay is not write permission",
                )
            ),
        )
    )
    report.notes.append(
        f"{profile.fs_type} is browsed read-only; native ext3/ext4 writer profiles are not "
        "published and journal replay is never followed by a write"
    )


def _probe_exfat(report: PreflightReport) -> None:
    """Evaluate the bounded one-FAT exFAT profile gates."""
    profile = probe_exfat_profile(
        report.image, offset=report.offset, volume_size=report.volume_size
    )
    report.filesystem = "exFAT"
    report.notes.append(
        "exFAT geometry: sector=%d cluster=%d cluster_count=%d root_cluster=%d"
        % (
            profile.bytes_per_sector,
            profile.cluster_size,
            profile.cluster_count,
            profile.root_cluster,
        )
    )

    if profile.number_of_fats == 1:
        report.gates.append(
            _gate(
                "one-fat-profile",
                "feature",
                GATE_PASS,
                "volume declares a single FAT (one-FAT profile)",
            )
        )
    else:
        reason = (
            "exFAT volume declares %d FATs; only the one-FAT profile is supported"
            % profile.number_of_fats
        )
        report.gates.append(
            _gate(
                "one-fat-profile",
                "feature",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason)

    blocked = profile.volume_flags & (
        _EXFAT_VOLUME_DIRTY | _EXFAT_MEDIA_FAILURE | _EXFAT_CLEAR_TO_ZERO
    )
    if not blocked:
        report.gates.append(
            _gate(
                "volume-flags-clean",
                "image-state",
                GATE_PASS,
                f"VolumeFlags=0x{profile.volume_flags:X}: no dirty, media-failure or clear-to-zero bit",
            )
        )
    else:
        reason = (
            f"exFAT VolumeFlags=0x{profile.volume_flags:X} reports dirty, media failure or clear-to-zero "
            "state"
        )
        report.gates.append(
            _gate(
                "volume-flags-clean",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.DIRTY_OR_RECOVERY_REQUIRED, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.DIRTY_OR_RECOVERY_REQUIRED, reason)

    report.notes.append(
        "boot-region geometry is a bounded profile check only; the reader still "
        "validates directory entry sets, allocation bitmap and $UpCase"
    )


def _probe_ntfs(report: PreflightReport) -> None:
    """Evaluate bounded NTFS boot/MFT gates; never enable log replay."""
    profile = probe_ntfs_profile(
        report.image, offset=report.offset, volume_size=report.volume_size
    )
    report.filesystem = "NTFS"
    report.notes.append(
        "NTFS geometry: sector=%d cluster=%d total_sectors=%d record_size=%d"
        % (
            profile.bytes_per_sector,
            profile.cluster_size,
            profile.total_sectors,
            profile.file_record_size,
        )
    )

    metadata = probe_ntfs_metadata(
        report.image, offset=report.offset, volume_size=report.volume_size
    )
    if metadata.mirror_matches_mft:
        report.gates.append(
            _gate(
                "mft-mirror-match",
                "image-state",
                GATE_PASS,
                "bounded MFT record 0 matches its mirror copy",
            )
        )
    else:
        reason = "MFT record 0 does not match the mirror record"
        report.gates.append(
            _gate(
                "mft-mirror-match",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.MALFORMED_METADATA, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.MALFORMED_METADATA, reason)

    if metadata.logfile_restart is None:
        report.gates.append(
            _gate(
                "logfile-restart-area",
                "feature",
                GATE_UNKNOWN,
                "$LogFile restart area is unavailable; browsing eligibility is "
                "decided by the read-only provider gate",
            )
        )
    else:
        restart = metadata.logfile_restart
        report.gates.append(
            _gate(
                "logfile-restart-area",
                "feature",
                GATE_PASS,
                "$LogFile restart area parsed: current_lsn=%d flags=0x%X"
                % (restart.current_lsn, restart.flags),
            )
        )
    report.notes.append(
        "NTFS $LogFile replay and native writes remain disabled; a clean volume "
        "grants read-only browsing only"
    )


def _probe_fat(report: PreflightReport) -> None:
    """Evaluate bounded FAT12/16/32 geometry gates."""
    profile = probe_fat_profile(
        report.image, offset=report.offset, volume_size=report.volume_size
    )
    report.filesystem = "FAT%d" % profile.fat_type
    report.notes.append(
        "FAT geometry: type=FAT%d sector=%d cluster=%d fat_count=%d fat_sectors=%d"
        % (
            profile.fat_type,
            profile.bytes_per_sector,
            profile.cluster_size,
            profile.fat_count,
            profile.fat_sectors,
        )
    )
    report.gates.append(
        _gate(
            "fat-geometry",
            "feature",
            GATE_PASS,
            "BPB geometry and cluster-chain bounds are valid for FAT%d"
            % profile.fat_type,
        )
    )
    if profile.fat_count > 1 and not profile.fat_mirroring_enabled:
        report.notes.append(
            "multiple FATs are present without mirroring; only the selected "
            "active FAT index %d is consulted" % profile.active_fat_index
        )
    report.notes.append(
        "FAT has no dirty flag in the BPB; only the bounded chain/BPB checks "
        "above decide read eligibility"
    )


_PROBE_BY_FAMILY = {
    "ext": _probe_ext,
    "exFAT": _probe_exfat,
    "NTFS": _probe_ntfs,
    "FAT": _probe_fat,
}


def _ensure_write_refusal(report: PreflightReport) -> None:
    """Guarantee that every report explains why it does not authorize a write."""
    if report.write_refusal is None:
        report.write_refusal = _refusal(
            RefusalCode.OPERATION_NOT_SUPPORTED, _DEFAULT_WRITE_REFUSAL
        )
    report.write_enabled = False
    report.eligible_for_write = False


def _apply_capability_status(report: PreflightReport, backend: str) -> None:
    """Publish the opened provider's profile and operation matrix."""
    profile = get_native_capability_profile(backend, "vfs")
    if profile is None:
        report.notes.append(
            f"backend {backend!r} has no entry in the capability matrix; operations are "
            "reported from the provider contract only"
        )
        return
    status = get_native_capability_status(
        profile,
        backend=backend,
        write_enabled=False,
        write_rejection_reason=_DEFAULT_WRITE_REFUSAL,
        write_rejection_code=RefusalCode.OPERATION_NOT_SUPPORTED,
    )
    report.profile_id = str(status["profile_id"])
    report.read_capabilities = [str(item) for item in status["read_capabilities"]]
    report.write_capabilities = [str(item) for item in status["write_capabilities"]]
    report.operations = dict(status["operations"])
    report.write_refusal = (
        None if status["write_rejection"] is None else dict(status["write_rejection"])
    )


def preflight_filesystem(
    image_path: str | os.PathLike[str],
    offset: int = 0,
    volume_size: int | None = None,
) -> PreflightReport:
    """Validate one bounded filesystem extent and report eligibility.

    The image is only read. Read eligibility requires a passing signature
    detection, a passing family probe, and a successful production routing call
    with the legacy fallback disabled. Write eligibility is always ``False``:
    this report documents which writer profile exists, it does not select one.
    """
    image = os.fspath(image_path)
    offset = _checked_int(offset, "offset")
    if isinstance(volume_size, bool) or (
        volume_size is not None and not isinstance(volume_size, int)
    ):
        raise RefusalError(
            "volume_size must be an integer or None", RefusalCode.RANGE_OUT_OF_BOUNDS
        )
    if volume_size is not None and volume_size <= 0:
        raise RefusalError(
            "volume_size must be positive", RefusalCode.RANGE_OUT_OF_BOUNDS
        )

    report = PreflightReport(image=image, offset=offset, volume_size=volume_size)

    try:
        image_size = os.path.getsize(image)
    except OSError as exc:
        reason = f"cannot stat image: {exc}"
        report.gates.append(
            _gate(
                "image-readable",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSAFE, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.UNSAFE, reason)
        _ensure_write_refusal(report)
        return report

    report.gates.append(
        _gate(
            "image-readable",
            "image-state",
            GATE_PASS,
            "image opens read-only; %d bytes available" % image_size,
        )
    )
    if offset >= image_size:
        reason = "filesystem offset 0x%X is outside the %d-byte image" % (
            offset,
            image_size,
        )
        report.gates.append(
            _gate(
                "bounded-extent",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.RANGE_OUT_OF_BOUNDS, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.RANGE_OUT_OF_BOUNDS, reason)
        _ensure_write_refusal(report)
        return report

    if report.volume_size is None:
        report.volume_size = volume_size_for_offset(image, offset)
        if report.volume_size is None and offset == 0:
            report.volume_size = image_size
    if report.volume_size is None:
        reason = (
            f"a recognized filesystem at offset 0x{offset:X} requires an explicit bounded "
            "extent; pass volume_size from the partition table"
        )
        report.gates.append(
            _gate(
                "bounded-extent",
                "image-state",
                GATE_UNKNOWN,
                reason,
            )
        )
        report.notes.append(
            "detection is skipped without a bounded extent so no probe can read "
            "outside the intended partition"
        )
        _ensure_write_refusal(report)
        return report

    if offset + report.volume_size > image_size:
        reason = "filesystem extent [0x%X, 0x%X) exceeds the %d-byte image" % (
            offset,
            offset + report.volume_size,
            image_size,
        )
        report.gates.append(
            _gate(
                "bounded-extent",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.RANGE_OUT_OF_BOUNDS, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.RANGE_OUT_OF_BOUNDS, reason)
        _ensure_write_refusal(report)
        return report

    report.gates.append(
        _gate(
            "bounded-extent",
            "image-state",
            GATE_PASS,
            f"filesystem extent [0x{offset:X}, 0x{offset + report.volume_size:X}) is inside the image",
        )
    )

    try:
        family = _detect_family(report)
    except OSError as exc:
        reason = f"cannot read filesystem signatures: {exc}"
        report.gates.append(
            _gate(
                "signature-detection",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSAFE, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.UNSAFE, reason)
        _ensure_write_refusal(report)
        return report

    if family == "unknown":
        reason = (
            "no supported native filesystem signature was found in the selected "
            "extent"
        )
        report.gates.append(
            _gate(
                "native-signature",
                "feature",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason),
            )
        )
        report.refusal = _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason)
        if legacy_tsk_enabled():
            report.notes.append(
                "the optional pytsk3 compatibility fallback is enabled by "
                "environment; preflight never counts it as native support"
            )
        else:
            report.notes.append(
                "the optional pytsk3 compatibility fallback is disabled; an "
                "unrecognized format is refused rather than silently re-routed"
            )
        _ensure_write_refusal(report)
        return report

    probe = _PROBE_BY_FAMILY[family]
    try:
        probe(report)
    except TskError as exc:
        _record_probe_failure(report, exc)
        _ensure_write_refusal(report)
        return report
    except (ValueError, RuntimeError, OSError) as exc:
        _record_probe_failure(report, exc)
        _ensure_write_refusal(report)
        return report

    try:
        provider = open_image_provider(
            image,
            offset=offset,
            volume_size=report.volume_size,
            allow_legacy_fallback=False,
        )
    except LegacyFallbackAvailableError as exc:
        reason = f"native provider did not claim this image: {exc}"
        report.gates.append(
            _gate(
                "native-provider-open",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.UNSUPPORTED_PROFILE, reason)
        _ensure_write_refusal(report)
        return report
    except RecognizedFilesystemError as exc:
        code = getattr(exc, "code", None)
        code = code if isinstance(code, RefusalCode) else RefusalCode.MALFORMED_METADATA
        reason = f"native provider refused the validated profile: {exc}"
        report.gates.append(
            _gate(
                "native-provider-open",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(code, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(code, reason)
        report.notes.append(
            "the legacy fallback stays disabled for a recognized profile, so "
            "this refusal cannot be re-routed to another parser"
        )
        _ensure_write_refusal(report)
        return report
    except TskError as exc:
        reason = f"image routing failed: {exc}"
        report.gates.append(
            _gate(
                "native-provider-open",
                "image-state",
                GATE_FAIL,
                reason,
                _refusal(RefusalCode.UNSAFE, reason),
            )
        )
        if report.refusal is None:
            report.refusal = _refusal(RefusalCode.UNSAFE, reason)
        _ensure_write_refusal(report)
        return report

    try:
        backend = str(getattr(provider, "backend_name", type(provider).__name__))
        report.backend = backend
    finally:
        closer = getattr(provider, "close", None)
        if callable(closer):
            closer()

    report.gates.append(
        _gate(
            "native-provider-open",
            "image-state",
            GATE_PASS,
            f"bounded native provider {backend!r} opened and closed read-only",
        )
    )
    _apply_capability_status(report, backend)

    native_write_profile = _NATIVE_WRITE_PROFILE_BY_FILESYSTEM.get(report.filesystem)
    report.native_write_profile = native_write_profile
    if native_write_profile is None:
        write_reason = (
            f"no native write profile is published for {report.filesystem}; the filesystem stays "
            "read-only"
        )
    else:
        write_reason = (
            f"{report.filesystem} publishes the bounded profile {native_write_profile!r}, but preflight does not select "
            "it: writes require an explicit writer call and a fresh state check"
        )
    report.write_refusal = _refusal(RefusalCode.OPERATION_NOT_SUPPORTED, write_reason)
    _ensure_write_refusal(report)

    report.eligible_for_read = all(
        gate.state != GATE_FAIL for gate in report.gates if gate.kind != "operation"
    ) and any(gate.gate_id == "native-provider-open" for gate in report.gates)
    if report.eligible_for_read:
        report.refusal = None
    return report


def _record_probe_failure(report: PreflightReport, exc: Exception) -> None:
    """Record a bounded probe refusal without turning it into permission."""
    code = getattr(exc, "code", None)
    code = code if isinstance(code, RefusalCode) else RefusalCode.MALFORMED_METADATA
    reason = f"bounded profile probe refused this image: {exc}"
    report.gates.append(
        _gate("profile-probe", "image-state", GATE_FAIL, reason, _refusal(code, reason))
    )
    if report.refusal is None:
        report.refusal = _refusal(code, reason)
    report.notes.append(
        "the probe result is not a browsing permission; a malformed or "
        "unvalidated profile is refused before any file listing"
    )
    _ensure_write_refusal(report)


def _partition_extents(image_path: str) -> list[tuple[int, int | None]]:
    """Return bounded ``(offset, size)`` pairs, falling back to the raw image."""
    try:
        from mountfile.partition import list_partitions

        partitions = list_partitions(image_path)
    except Exception:  # pragma: no cover - partition parsing is best effort
        return [(0, None)]
    extents = [
        (int(partition.offset), int(partition.size_bytes))
        for partition in partitions
        if int(getattr(partition, "size_bytes", 0) or 0) > 0
    ]
    return extents or [(0, None)]


def preflight_image(
    image_path: str | os.PathLike[str],
    *,
    offset: int | None = None,
    volume_size: int | None = None,
) -> dict[str, Any]:
    """Validate one explicit extent or every detected partition of an image."""
    image = os.fspath(image_path)
    if offset is not None:
        report = preflight_filesystem(image, offset=offset, volume_size=volume_size)
        return {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "image": image,
            "volumes": [report.to_dict()],
        }
    volumes = [
        preflight_filesystem(
            image, offset=extent_offset, volume_size=extent_size
        ).to_dict()
        for extent_offset, extent_size in _partition_extents(image)
    ]
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "image": image,
        "volumes": volumes,
    }
