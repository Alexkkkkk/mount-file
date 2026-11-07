# SPDX-License-Identifier: MIT
"""Canonical operation vocabulary and bounded native profile matrix.

The matrix describes operations exposed by the current VFS and RW APIs.  It is
not a claim of full filesystem-format parity: a profile is available only after
its parser/writer validates the selected image and all profile-specific gates.

Schema v5 retains the v4 interface-specific capability lists for compatibility,
and adds a common operation/status projection.  Clients can therefore render a
single profile × operation × refusal view without guessing whether the selected
backend was opened through VFS or :class:`UniversalFSRW`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType

from mountfile.safety import RefusalCode, RefusalError

CAPABILITY_MATRIX_VERSION = 5

# Read-only providers surfaced through the public VFS API.  These legacy names
# are kept unchanged in the versioned JSON payload.
VFS_READ_CAPABILITIES = ("list", "stat", "read-file", "read-range", "extract")
VFS_WRITE_CAPABILITIES: tuple[str, ...] = ()

# Operations surfaced by UniversalFSRW.  These are also retained as its
# interface-specific API vocabulary.
RW_READ_CAPABILITIES = ("list", "stat", "read", "read_range", "extract")
RW_WRITE_CAPABILITIES = ("write_file", "mkdir", "delete", "rename")

# Canonical, transport-neutral operation identifiers.  Each profile publishes
# all of these so a client never infers a missing key as permission to attempt
# a write.  The interface mapping below exposes the exact legacy method name.
CANONICAL_OPERATIONS = (
    "list",
    "stat",
    "read-file",
    "read-range",
    "extract",
    "write-file",
    "mkdir",
    "delete",
    "rename",
)

_INTERFACE_OPERATION_ALIASES = MappingProxyType(
    {
        "vfs": MappingProxyType(
            {
                "list": "list",
                "stat": "stat",
                "read-file": "read-file",
                "read-range": "read-range",
                "extract": "extract",
            }
        ),
        "universal-fs-rw": MappingProxyType(
            {
                "list": "list",
                "stat": "stat",
                "read-file": "read",
                "read-range": "read_range",
                "extract": "extract",
                "write-file": "write_file",
                "mkdir": "mkdir",
                "delete": "delete",
                "rename": "rename",
            }
        ),
    }
)


@dataclass(frozen=True)
class CapabilityGate:
    """One machine-readable image-state or feature gate for a profile."""

    gate_id: str
    kind: str
    state: str
    detail: str
    refusal_code: RefusalCode | None = None

    def to_dict(self) -> dict[str, object]:
        refusal = (
            None
            if self.refusal_code is None
            else _refusal_payload(self.refusal_code, self.detail)
        )
        return {
            "id": self.gate_id,
            "kind": self.kind,
            "state": self.state,
            "detail": self.detail,
            "refusal": refusal,
        }


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

    @property
    def constraint_ids(self) -> tuple[str, ...]:
        return _PROFILE_CONSTRAINT_IDS[self.profile_id]

    @property
    def gates(self) -> tuple[CapabilityGate, ...]:
        return _PROFILE_GATES[self.profile_id]

    @property
    def recovery_contract(self) -> dict[str, str]:
        """Return evidence-bounded crash and rollback claims for this profile."""
        return dict(_RECOVERY_CONTRACTS[self.profile_id])

    def operation_matrix(
        self, *, write_enabled: bool | None = None
    ) -> dict[str, dict[str, object]]:
        """Return a complete canonical operation map for this profile.

        ``write_enabled`` is used for an opened backend status.  Passing
        ``False`` makes every mutation refused even for a profile that could
        otherwise support native writes, such as a read-only OS handle.
        """
        if write_enabled is None:
            write_enabled = bool(self.write_capabilities)
        aliases = _INTERFACE_OPERATION_ALIASES[self.interface]
        available_api_operations = set(self.read_capabilities)
        if write_enabled:
            available_api_operations.update(self.write_capabilities)

        matrix: dict[str, dict[str, object]] = {}
        for operation in CANONICAL_OPERATIONS:
            api_operation = aliases.get(operation)
            available = api_operation in available_api_operations
            if available:
                matrix[operation] = {
                    "state": "available",
                    "api_operation": api_operation,
                    "refusal": None,
                }
                continue

            if operation in _WRITE_CANONICAL_OPERATIONS and not write_enabled:
                message = "selected profile is read-only for this operation"
            else:
                message = "operation is not exposed by the selected profile interface"
            matrix[operation] = {
                "state": "refused",
                "api_operation": None,
                "refusal": _refusal_payload(
                    RefusalCode.OPERATION_NOT_SUPPORTED, message
                ),
            }
        return matrix

    def to_dict(self) -> dict[str, object]:
        """Return a detached, JSON-serialisable profile record."""
        value = asdict(self)
        value["read_capabilities"] = list(self.read_capabilities)
        value["write_capabilities"] = list(self.write_capabilities)
        value["constraints"] = list(_PROFILE_CONSTRAINTS[self.profile_id])
        value["constraint_ids"] = list(self.constraint_ids)
        value["gates"] = [gate.to_dict() for gate in self.gates]
        value["recovery"] = self.recovery_contract
        value["operations"] = self.operation_matrix()
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
        "fat12-16-native-rw",
        "FAT12",
        "native-fat-rw",
        "universal-fs-rw",
        "read-write",
        RW_READ_CAPABILITIES,
        RW_WRITE_CAPABILITIES,
        "native-fat12-16",
    ),
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
            "Explicit replay accepts classic 32-bit revoke records in a validated "
            "no-checksum JBD2 overlay.",
        ),
        "ext4-native-ro": (
            "Bounded classic/accepted extent profile only.",
            "Bounded 64BIT group-descriptor ext4 profiles are supported.",
            "flex_bg, bigalloc, and metadata_csum profiles fail closed.",
        ),
        "ntfs-native-ro": (
            "Bounded metadata/tree profile only; resident and bounded nonresident "
            "ATTRIBUTE_LIST resolution is supported for files and directories, while "
            "$MFT bootstrap extension records and unsupported streams are refused.",
            "No $LogFile replay, sparse, compressed, or encrypted stream support.",
        ),
        "exfat-native-ro": (
            "Bounded one-FAT profile only; two-FAT TexFAT is refused for native browsing and writes.",
            "Dirty or media-failure volumes are refused; ClearToZero is allowed read-only.",
            "A present root volume-label entry is validated; malformed or duplicate "
            "labels are refused.",
        ),
        "fat12-16-native-rw": (
            "Native bounded FAT12/FAT16 writer with packed and mirrored FAT updates.",
            "Fixed root directory; unsupported special or sparse cases are refused.",
        ),
        "fat32-native-rw": (
            "Native bounded FAT32 writer; put/replace, mkdir, rename, and delete.",
        ),
        "ext2-native-rw": (
            "Clean ext2 allowlist only; regular files use bounded direct, single-, "
            "and double-indirect blocks, up to 64 MiB.",
            "Sparse and triple-indirect files/directories are refused; directory "
            "growth supports bounded direct, single-, and double-indirect blocks.",
        ),
        "exfat-native-rw": (
            "Bounded one-FAT profile only; two-FAT TexFAT is refused for native browsing and writes.",
            "Requires ActiveFat=0; refuses dirty, media-failure, ClearToZero, and reserved flags.",
            "Unsupported metadata layouts are refused.",
            "A single root volume label is validated and exposed through FSInfo.label.",
        ),
    }
)

# These are claims about the evidence committed to the repository, not claims
# that a filesystem format is generally crash-safe. In particular, no native
# writer in this matrix claims power-loss atomicity.
_RECOVERY_CONTRACTS = MappingProxyType(
    {
        **{
            profile_id: {
                "rollback": "not_applicable",
                "process_kill": "not_applicable",
                "power_loss": "unsupported",
                "quarantine": "not_applicable",
            }
            for profile_id in (
                "fat12-native-ro",
                "fat16-native-ro",
                "fat32-native-ro",
                "ext2-native-ro",
                "ext3-native-ro",
                "ext4-native-ro",
                "ntfs-native-ro",
                "exfat-native-ro",
            )
        },
        "fat12-16-native-rw": {
            "rollback": "tested_on_failure",
            "process_kill": "not_proven",
            "power_loss": "unsupported",
            "quarantine": "not_proven",
        },
        "fat32-native-rw": {
            "rollback": "tested_on_failure",
            "process_kill": "not_proven",
            "power_loss": "unsupported",
            "quarantine": "not_proven",
        },
        "ext2-native-rw": {
            "rollback": "tested_on_failure",
            "process_kill": "tested_with_recovery_sidecar",
            "power_loss": "unsupported",
            "quarantine": "tested_on_recovery_failure",
        },
        "exfat-native-rw": {
            "rollback": "tested_on_failure",
            "process_kill": "not_proven",
            "power_loss": "unsupported",
            "quarantine": "not_proven",
        },
    }
)

_COMMON_GATES = (
    CapabilityGate(
        "bounded-extent",
        "image-state",
        "required",
        "The selected filesystem extent must be explicit and remain in image bounds.",
        RefusalCode.RANGE_OUT_OF_BOUNDS,
    ),
    CapabilityGate(
        "valid-metadata",
        "image-state",
        "required",
        "Malformed or inconsistent metadata is rejected before the operation.",
        RefusalCode.MALFORMED_METADATA,
    ),
)

_PROFILE_SPECIFIC_GATES = MappingProxyType(
    {
        "fat12-native-ro": (
            CapabilityGate(
                "fat12-geometry",
                "feature",
                "required",
                "FAT12 BPB geometry and cluster chains must be valid.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "fat16-native-ro": (
            CapabilityGate(
                "fat16-geometry",
                "feature",
                "required",
                "FAT16 BPB geometry and cluster chains must be valid.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "fat32-native-ro": (
            CapabilityGate(
                "fat32-geometry",
                "feature",
                "required",
                "FAT32 geometry and selected FAT metadata must be valid.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "ext2-native-ro": (
            CapabilityGate(
                "clean-volume",
                "image-state",
                "required",
                "Dirty or recovery-required ext volumes are rejected.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "allowlisted-ext2-features",
                "feature",
                "required",
                "Only the explicitly validated ext2 feature/layout profile is accepted.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "ext3-native-ro": (
            CapabilityGate(
                "clean-jbd2-state",
                "image-state",
                "required",
                "Journal recovery or unvalidated JBD2 state is rejected by default.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "validated-jbd2",
                "feature",
                "required",
                "Only the bounded, validated JBD2 record profile is accepted.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "ext4-native-ro": (
            CapabilityGate(
                "clean-volume",
                "image-state",
                "required",
                "Dirty or recovery-required ext volumes are rejected.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "allowlisted-ext4-layout",
                "feature",
                "required",
                "Unsupported layouts and unchecked metadata checksums are rejected.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "ntfs-native-ro": (
            CapabilityGate(
                "clean-logfile-state",
                "image-state",
                "required",
                "Dirty or recovery-required $LogFile state is rejected.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "supported-ntfs-stream",
                "feature",
                "required",
                "Unsupported streams and attributes are rejected instead of guessed.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "exfat-native-ro": (
            CapabilityGate(
                "single-exfat-fat",
                "feature",
                "required",
                "NumberOfFats must be 1; two-FAT TexFAT is unsupported.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
            CapabilityGate(
                "clean-exfat-state",
                "image-state",
                "required",
                "Dirty or media-failure volumes are rejected.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "validated-exfat-metadata",
                "feature",
                "required",
                "The single FAT and mandatory system metadata must validate.",
                RefusalCode.MALFORMED_METADATA,
            ),
        ),
        "fat12-16-native-rw": (
            CapabilityGate(
                "staged-write-identity",
                "image-state",
                "required",
                "Mutation rechecks source identity and uses staged read-back/rollback.",
                RefusalCode.CONCURRENT_CHANGE,
            ),
            CapabilityGate(
                "fixed-root-fat-layout",
                "feature",
                "required",
                "FAT12/FAT16 writes require validated packed FAT and fixed-root geometry.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "fat32-native-rw": (
            CapabilityGate(
                "staged-write-identity",
                "image-state",
                "required",
                "Mutation rechecks source identity and uses staged read-back/rollback.",
                RefusalCode.CONCURRENT_CHANGE,
            ),
        ),
        "ext2-native-rw": (
            CapabilityGate(
                "clean-ext2-write-state",
                "image-state",
                "required",
                "Only clean, unjournaled, allowlisted ext2 volumes are writable.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "bounded-ext2-write-layout",
                "feature",
                "required",
                "Sparse and triple-indirect files are refused; double-indirect "
                "regular files are bounded to 64 MiB.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
        ),
        "exfat-native-rw": (
            CapabilityGate(
                "single-exfat-fat",
                "feature",
                "required",
                "NumberOfFats must be 1; two-FAT TexFAT is unsupported.",
                RefusalCode.UNSUPPORTED_PROFILE,
            ),
            CapabilityGate(
                "clean-exfat-write-state",
                "image-state",
                "required",
                "Dirty, media-failure, ClearToZero and reserved volume flags are refused.",
                RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            ),
            CapabilityGate(
                "validated-exfat-fat-write",
                "feature",
                "required",
                "The single FAT and mandatory system metadata must validate before mutation.",
                RefusalCode.MALFORMED_METADATA,
            ),
        ),
    }
)

_PROFILE_GATES = MappingProxyType(
    {
        profile.profile_id: _COMMON_GATES + _PROFILE_SPECIFIC_GATES[profile.profile_id]
        for profile in NATIVE_CAPABILITY_MATRIX
    }
)
_PROFILE_CONSTRAINT_IDS = MappingProxyType(
    {
        profile_id: tuple(gate.gate_id for gate in gates)
        for profile_id, gates in _PROFILE_GATES.items()
    }
)

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
_WRITE_CANONICAL_OPERATIONS = frozenset(("write-file", "mkdir", "delete", "rename"))


def _refusal_payload(code: RefusalCode, message: str) -> dict[str, str]:
    """Return a stable serialisable refusal payload."""
    refusal = RefusalError(message, code)
    return refusal.to_dict()


def canonical_operation_for_api_operation(
    interface: str, api_operation: str
) -> str | None:
    """Map one interface-specific operation name to its canonical identifier."""
    for canonical, candidate in _INTERFACE_OPERATION_ALIASES.get(interface, {}).items():
        if candidate == api_operation:
            return canonical
    return None


def get_native_capability_matrix() -> dict[str, object]:
    """Return a detached, versioned JSON-serialisable matrix snapshot."""
    return {
        "version": CAPABILITY_MATRIX_VERSION,
        "operations": list(CANONICAL_OPERATIONS),
        "profiles": [profile.to_dict() for profile in NATIVE_CAPABILITY_MATRIX],
    }


def get_native_capability_profile(
    backend: str, interface: str
) -> CapabilityProfile | None:
    """Return the canonical profile for one backend/interface pair, if known."""
    return NATIVE_CAPABILITY_PROFILES_BY_BACKEND_INTERFACE.get((backend, interface))


def get_native_capability_status(
    profile: CapabilityProfile | str,
    *,
    backend: str | None = None,
    write_enabled: bool | None = None,
    write_rejection_reason: str | None = None,
    write_rejection_code: RefusalCode | str | None = None,
) -> dict[str, object]:
    """Build the shared serialisable status for one selected native profile.

    Runtime validators stay authoritative.  This helper only publishes the
    profile selected after validation and the operations currently enabled for
    that opened backend; it never turns a profile writable on its own.
    """
    if isinstance(profile, str):
        profile = NATIVE_CAPABILITY_PROFILES[profile]
    if not isinstance(profile, CapabilityProfile):
        raise TypeError("profile must be a native capability profile or profile id")

    if write_enabled is None:
        write_enabled = bool(profile.write_capabilities)
    effective_write = bool(write_enabled and profile.write_capabilities)
    if effective_write:
        rejection_reason = ""
        rejection_code = None
        rejection = None
    else:
        rejection_reason = write_rejection_reason or (
            "native provider is read-only; use an explicitly selected bounded writer profile"
            if not profile.write_capabilities
            else "selected native write profile is not writable for this image"
        )
        rejection_code = RefusalCode(
            write_rejection_code or RefusalCode.OPERATION_NOT_SUPPORTED
        )
        rejection = _refusal_payload(rejection_code, rejection_reason)

    return {
        "backend": backend or profile.backend,
        "filesystem": profile.filesystem,
        "interface": profile.interface,
        "mode": "read-write" if effective_write else "read-only",
        "profile_id": profile.profile_id,
        "matrix_version": CAPABILITY_MATRIX_VERSION,
        "constraints": list(_PROFILE_CONSTRAINTS[profile.profile_id]),
        "constraint_ids": list(profile.constraint_ids),
        "gates": [gate.to_dict() for gate in profile.gates],
        "read_capabilities": list(profile.read_capabilities),
        "write_capabilities": (
            list(profile.write_capabilities) if effective_write else []
        ),
        "write_enabled": effective_write,
        "write_rejection_reason": rejection_reason,
        "write_rejection_code": (
            None if rejection_code is None else rejection_code.value
        ),
        "write_rejection": rejection,
        "operations": profile.operation_matrix(write_enabled=effective_write),
        "recovery": profile.recovery_contract,
    }
