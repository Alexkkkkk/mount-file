# SPDX-License-Identifier: MIT
"""Canonical operation vocabulary and bounded native profile matrix.

The matrix describes operations exposed by the current VFS and RW APIs. It is
not a claim of full filesystem-format parity: a profile is available only after
its parser/writer validates the selected image and all profile-specific gates.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from types import MappingProxyType

CAPABILITY_MATRIX_VERSION = 6

# Read-only providers surfaced through the public VFS API.
VFS_READ_CAPABILITIES = ("list", "stat", "read-file", "read-range", "extract")
VFS_WRITE_CAPABILITIES: tuple[str, ...] = ()

# Operations surfaced by UniversalFSRW; this is a distinct API vocabulary.
RW_READ_CAPABILITIES = ("list", "read", "extract")
RW_WRITE_CAPABILITIES = ("write_file", "mkdir", "delete", "rename")
OPERATION_VOCABULARY = (
    "list",
    "stat",
    "read",
    "read-file",
    "read-range",
    "extract",
    "write_file",
    "mkdir",
    "delete",
    "rename",
)


@dataclass(frozen=True)
class CapabilityProfile:
    """One accepted bounded profile and the operations its API exposes."""

    profile_id: str
    filesystem: str
    backend: str
    interface: str
    mode: str
    read_capabilities: tuple[str, ...]
    write_capabilities: tuple[str, ...] = ()
    write_strategy: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable profile record."""
        value = asdict(self)
        value["read_capabilities"] = list(self.read_capabilities)
        value["write_capabilities"] = list(self.write_capabilities)
        value["constraints"] = list(_PROFILE_CONSTRAINTS[self.profile_id])
        value["known_gates"] = _profile_known_gates(self.profile_id)
        value["operation_decisions"] = _profile_operation_decisions(self)
        return value


_READ_ONLY_PROFILES = (
    CapabilityProfile(
        "fat12-native-ro",
        "FAT12",
        "native-fat12-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "fat16-native-ro",
        "FAT16",
        "native-fat16-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "fat32-native-ro",
        "FAT32",
        "native-fat32-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "ext2-native-ro",
        "ext2",
        "native-ext2",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "ext3-native-ro",
        "ext3",
        "native-ext3-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "ext4-native-ro",
        "ext4",
        "native-ext4-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "ntfs-native-ro",
        "NTFS",
        "native-ntfs-metadata-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
    CapabilityProfile(
        "exfat-native-ro",
        "exFAT",
        "native-exfat-ro",
        "vfs",
        "read-only",
        VFS_READ_CAPABILITIES,
    ),
)

_WRITE_PROFILES = (
    CapabilityProfile(
        "fat32-native-rw",
        "FAT32",
        "native-fat-rw",
        "universal-fs-rw",
        "read-write",
        RW_READ_CAPABILITIES,
        RW_WRITE_CAPABILITIES,
        "native",
    ),
    CapabilityProfile(
        "ext2-native-rw",
        "ext2",
        "native-ext2",
        "universal-fs-rw",
        "read-write",
        RW_READ_CAPABILITIES,
        RW_WRITE_CAPABILITIES,
        "native-ext2",
    ),
    CapabilityProfile(
        "exfat-native-rw",
        "exFAT",
        "native-exfat-rw",
        "universal-fs-rw",
        "read-write",
        RW_READ_CAPABILITIES,
        RW_WRITE_CAPABILITIES,
        "native-exfat-phase1",
    ),
)

NATIVE_CAPABILITY_MATRIX = _READ_ONLY_PROFILES + _WRITE_PROFILES
_PROFILE_CONSTRAINTS = MappingProxyType(
    {
        "fat12-native-ro": (
            "Native read-only profile; validate selected volume bounds.",
        ),
        "fat16-native-ro": (
            "Native read-only profile; validate selected volume bounds.",
        ),
        "fat32-native-ro": (
            "Native reader requires a validated FAT32 profile and bounded extent.",
        ),
        "ext2-native-ro": (
            "Clean ext2 only; allowlisted features and bounded linear directories.",
            "Unsupported inode layouts and indirect levels fail closed.",
        ),
        "ext3-native-ro": (
            "Read-only ext3; only validated supported JBD2 states are exposed.",
            "No writes or complete crash-recovery guarantee.",
        ),
        "ext4-native-ro": (
            "Bounded classic/accepted extent profile only.",
            "Unsupported 64BIT, flex_bg, bigalloc, and metadata_csum profiles fail closed.",
        ),
        "ntfs-native-ro": (
            "Bounded metadata/tree profile only; ATTRIBUTE_LIST and unsupported streams "
            "are refused.",
            "No $LogFile replay, sparse, compressed, or encrypted stream support.",
        ),
        "exfat-native-ro": (
            "Bounded one- or two-FAT profile; reads the validated selected active FAT.",
            "Dirty or media-failure volumes are refused; ClearToZero is allowed read-only.",
        ),
        "fat32-native-rw": (
            "Native bounded FAT32 writer; put/replace, mkdir, rename, and delete.",
        ),
        "ext2-native-rw": (
            "Clean ext2 allowlist only; regular files use 12 direct plus one "
            "single-indirect block.",
            "Sparse, double/triple-indirect, and indirect directory growth are refused.",
        ),
        "exfat-native-rw": (
            "Bounded one- or two-FAT profile; reads the validated active FAT and "
            "mirrors FAT writes to all declared copies.",
            "Allows only a validated ActiveFat selection (VolumeFlags 0x0000, or "
            "0x0001 with two FATs); refuses dirty, media-failure, ClearToZero, and "
            "reserved flags.",
            "Unsupported metadata layouts are refused.",
        ),
    }
)

_MAX_READ_RANGE_BYTES = MappingProxyType(
    {
        "fat12-native-ro": 16 * 1024 * 1024,
        "fat16-native-ro": 16 * 1024 * 1024,
        "fat32-native-ro": 16 * 1024 * 1024,
        "ext2-native-ro": 16 * 1024 * 1024,
        "ext3-native-ro": 16 * 1024 * 1024,
        "ext4-native-ro": 16 * 1024 * 1024,
        "ntfs-native-ro": 1 * 1024 * 1024,
        "exfat-native-ro": 16 * 1024 * 1024,
    }
)

_PROFILE_STATE_GATES = MappingProxyType(
    {
        "ext2-native-ro": (
            {
                "state": "dirty_or_recovery_required",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
        ),
        "ext3-native-ro": (
            {
                "state": "dirty_or_journal_recovery_required",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
        ),
        "ext4-native-ro": (
            {
                "state": "dirty_or_journal_recovery_required",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
        ),
        "ntfs-native-ro": (
            {
                "state": "dirty_or_logfile_recovery_required",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
        ),
        "exfat-native-ro": (
            {
                "state": "volume_dirty",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
            {
                "state": "media_failure",
                "outcome": "refuse",
                "code": "integrity_check_failed",
            },
        ),
        "exfat-native-rw": (
            {
                "state": "volume_dirty",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
            {
                "state": "media_failure",
                "outcome": "refuse",
                "code": "integrity_check_failed",
            },
            {
                "state": "clear_to_zero_or_reserved_flags",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
        "ext2-native-rw": (
            {
                "state": "dirty_or_journaled_profile",
                "outcome": "refuse",
                "code": "dirty_or_recovery_required",
            },
        ),
    }
)

_PROFILE_FEATURE_GATES = MappingProxyType(
    {
        "ext2-native-ro": (
            {
                "feature": "unsupported_ext_feature_or_inode_layout",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
        "ext3-native-ro": (
            {
                "feature": "unsupported_jbd2_or_ext_feature",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
        "ext4-native-ro": (
            {
                "feature": "64bit_flex_bg_bigalloc_metadata_csum_or_unknown",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
        "ntfs-native-ro": (
            {
                "feature": "attribute_list_bootstrap_or_sparse_compressed_encrypted_stream",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
        "exfat-native-ro": (
            {
                "feature": "fat_copy_count",
                "supported_values": [1, 2],
                "selection": "validated_active_fat",
            },
        ),
        "exfat-native-rw": (
            {
                "feature": "fat_copy_count",
                "supported_values": [1, 2],
                "selection": "validated_active_fat_and_mirror_updates",
            },
        ),
        "ext2-native-rw": (
            {
                "feature": "sparse_or_double_triple_indirect_write",
                "outcome": "refuse",
                "code": "unsupported_profile",
            },
        ),
    }
)


def _profile_known_gates(profile_id: str) -> dict[str, object]:
    """Expose selected verified gates without claiming exhaustive validation."""
    return {
        "coverage": "selected-known-gates-not-exhaustive",
        "state_gates": [
            deepcopy(rule) for rule in _PROFILE_STATE_GATES.get(profile_id, ())
        ],
        "feature_gates": [
            deepcopy(rule) for rule in _PROFILE_FEATURE_GATES.get(profile_id, ())
        ],
        "limits": {"max_read_range_bytes": _MAX_READ_RANGE_BYTES.get(profile_id)},
    }


def _profile_operation_decisions(profile: CapabilityProfile) -> list[dict[str, object]]:
    """Give every operation in the public vocabulary an explicit decision."""
    advertised = set(profile.read_capabilities) | set(profile.write_capabilities)
    return [
        {
            "operation": operation,
            "outcome": "allow" if operation in advertised else "refuse",
            "refusal_code": (
                None if operation in advertised else "operation_not_supported"
            ),
            "reason": (
                None
                if operation in advertised
                else f"not advertised by {profile.interface} profile"
            ),
        }
        for operation in OPERATION_VOCABULARY
    ]


NATIVE_CAPABILITY_PROFILES = MappingProxyType(
    {profile.profile_id: profile for profile in NATIVE_CAPABILITY_MATRIX}
)
NATIVE_CAPABILITY_PROFILES_BY_BACKEND_INTERFACE = MappingProxyType(
    {
        (profile.backend, profile.interface): profile
        for profile in NATIVE_CAPABILITY_MATRIX
    }
)
NATIVE_WRITE_CAPABILITIES_BY_STRATEGY = MappingProxyType(
    {profile.write_strategy: profile.write_capabilities for profile in _WRITE_PROFILES}
)
NATIVE_WRITE_PROFILES_BY_STRATEGY = MappingProxyType(
    {profile.write_strategy: profile for profile in _WRITE_PROFILES}
)


def get_native_capability_matrix() -> dict[str, object]:
    """Return a detached, versioned JSON-serialisable matrix snapshot."""
    return {
        "version": CAPABILITY_MATRIX_VERSION,
        "coverage": (
            "complete-operation-vocabulary; selected-feature-state-gates-not-exhaustive"
        ),
        "operation_vocabulary": list(OPERATION_VOCABULARY),
        "profiles": [profile.to_dict() for profile in NATIVE_CAPABILITY_MATRIX],
    }


def get_native_capability_profile(
    backend: str, interface: str
) -> CapabilityProfile | None:
    """Return the canonical profile for one backend/interface pair, if known."""
    return NATIVE_CAPABILITY_PROFILES_BY_BACKEND_INTERFACE.get((backend, interface))
