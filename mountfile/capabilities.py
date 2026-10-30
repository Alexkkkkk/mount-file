# SPDX-License-Identifier: MIT
"""Canonical operation vocabulary and bounded native profile matrix.

The matrix describes operations exposed by the current VFS and RW APIs. It is
not a claim of full filesystem-format parity: a profile is available only after
its parser/writer validates the selected image and all profile-specific gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType

CAPABILITY_MATRIX_VERSION = 1

# Read-only providers surfaced through the public VFS API.
VFS_READ_CAPABILITIES = ("list", "stat", "read-range", "extract")
VFS_WRITE_CAPABILITIES: tuple[str, ...] = ()

# Operations surfaced by UniversalFSRW; this is a distinct API vocabulary.
RW_READ_CAPABILITIES = ("list", "read", "extract")
RW_WRITE_CAPABILITIES = ("write_file", "mkdir", "delete", "rename")


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
NATIVE_CAPABILITY_PROFILES = MappingProxyType(
    {profile.profile_id: profile for profile in NATIVE_CAPABILITY_MATRIX}
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
        "profiles": [profile.to_dict() for profile in NATIVE_CAPABILITY_MATRIX],
    }
