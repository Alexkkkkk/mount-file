# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only crypto-container identification and optional adapter helpers.

This module deliberately stops at the container boundary.  Header inspection
never decrypts, mounts, or writes a source image.  The optional command
helpers are explicit and only invoke an installed adapter when the caller
asks for it; they do not mount the resulting mapping.

Passwords are accepted only as transient ``password`` arguments and are sent
through subprocess stdin when the adapter supports that interface.  They are
never included in command arguments, exception text, or log messages.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Callable, Dict, Optional, Union

# Public markers are useful to callers building synthetic, read-only fixtures.
LUKS_MAGIC = b"LUKS\xba\xbe"
BITLOCKER_MAGIC = b"\xeb\x52\x90-FVE-FS-"
APFS_CONTAINER_MAGIC = b"NXSB"
APFS_VOLUME_MAGIC = b"APSB"
CORESTORAGE_MAGIC = b"CS"

# APFS and CoreStorage metadata are at known offsets in a volume block.  A
# bounded read also includes the secondary LUKS2 header at 0x4000.
_HEADER_SIZE = 64 * 1024
_APFS_OBJECT_MAGIC_OFFSET = 32
_CORESTORAGE_MAGIC_OFFSET = 88
_CORESTORAGE_ENCRYPTION_OFFSET = 172
_LUKS2_SECONDARY_OFFSET = 0x4000


class ContainerKind(str, Enum):
    """Known container families returned by :func:`detect_container`."""

    UNKNOWN = "unknown"
    LUKS = "luks"
    BITLOCKER = "bitlocker"
    APFS = "apfs"
    FILEVAULT = "filevault"


# Backward/forward-compatible spelling for clients that prefer ``CryptoKind``.
CryptoKind = ContainerKind


class CryptoError(RuntimeError):
    """Base error for explicit adapter operations."""


class AdapterUnavailable(CryptoError):
    """Raised when the requested optional system adapter is not on PATH."""


class CryptoOperationError(CryptoError):
    """Raised when an adapter operation fails without exposing its secret."""


@dataclass(frozen=True)
class ContainerInfo:
    """Non-sensitive result of inspecting one container header.

    ``kind`` identifies a format family, while ``signature`` names the
    marker that matched.  ``encrypted`` is intentionally optional: an APFS
    header proves APFS, not that FileVault is enabled.  CoreStorage's ``CS``
    marker is reported as ``filevault`` because it is the legacy FileVault
    container marker, but the field still remains ``None`` unless the header
    contains the documented AES-XTS method value.
    """

    kind: str
    signature: str
    offset: int = 0
    version: Optional[int] = None
    encrypted: Optional[bool] = None
    adapter: Optional[str] = None

    @property
    def format(self) -> str:
        """Alias for callers that use ``format`` rather than ``kind``."""

        return self.kind

    @property
    def type(self) -> str:
        """Alias retained for simple discovery APIs."""

        return self.kind

    @property
    def is_known(self) -> bool:
        return self.kind != ContainerKind.UNKNOWN.value


@dataclass(frozen=True)
class AdapterStatus:
    """Safe availability result for one optional executable.

    Availability means only that the executable was found through
    :func:`shutil.which`; no device is opened and no command is run.
    """

    name: str
    executable: str
    available: bool
    path: Optional[str] = None
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.available


# Keep adapter names explicit and stable.  ``apfs-fuse`` is intentionally only
# an availability hint: this module does not claim to decrypt APFS/FileVault.
ADAPTER_EXECUTABLES: Mapping[str, str] = {
    "cryptsetup": "cryptsetup",
    "dislocker": "dislocker",
    "apfs-fuse": "apfs-fuse",
}
_ADAPTER_ALIASES: Mapping[str, str] = {
    "luks": "cryptsetup",
    "bitlocker": "dislocker",
    "apfs": "apfs-fuse",
    "filevault": "apfs-fuse",
}
_KIND_ADAPTERS: Mapping[str, str] = {
    "luks": "cryptsetup",
    "bitlocker": "dislocker",
    "apfs": "apfs-fuse",
    "filevault": "apfs-fuse",
}


Source = Union[str, os.PathLike, bytes, bytearray, memoryview, BinaryIO]
Runner = Callable[..., Any]


def _source_bytes(source: Source, offset: int, size: int) -> bytes:
    """Read a bounded header without changing a seekable file object's cursor."""

    if offset < 0:
        raise ValueError("offset must be non-negative")
    if size <= 0:
        return b""

    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)[offset : offset + size]

    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as fh:
            fh.seek(offset)
            return fh.read(size)

    # A file-like source is convenient for tests and existing image readers.
    # Preserve its current position whenever it can seek/tell.
    try:
        position = source.tell()
    except (AttributeError, OSError, ValueError):
        position = None
    try:
        source.seek(offset)
        data = source.read(size)
    finally:
        if position is not None:
            try:
                source.seek(position)
            except (OSError, ValueError):
                pass
    return bytes(data or b"")


def read_header(source: Source, *, offset: int = 0, size: int = _HEADER_SIZE) -> bytes:
    """Read at most ``size`` bytes for inspection, without writing the source."""

    return _source_bytes(source, offset, size)


def _luks_version(header: bytes, start: int = 0) -> Optional[int]:
    version_at = start + 6
    if len(header) < version_at + 2:
        return None
    return struct.unpack_from(">H", header, version_at)[0]


def _find_bitlocker(header: bytes) -> bool:
    # The canonical marker is at byte 3 after the x86 jump/NOP prologue.  The
    # bounded fallback accepts known forensic fixtures that contain the full
    # marker but omit the prologue, while remaining limited to the boot block.
    return (
        header[3:11] == b"-FVE-FS-"
        or (len(header) <= 512 and BITLOCKER_MAGIC in header[:64])
        or b"-FVE-FS-" in header[:64]
    )


def inspect_container(source: Source, *, offset: int = 0) -> ContainerInfo:
    """Identify a container from a header; never decrypts or mounts it.

    ``offset`` is the byte offset of the candidate volume inside a raw image.
    Passing a partition offset is therefore safe and avoids any dependency on
    partition parsing.  Unknown or truncated data returns an ``unknown``
    result rather than guessing.
    """

    header = read_header(source, offset=offset)

    # LUKS1 and LUKS2 share the magic.  LUKS2 also keeps a secondary header at
    # 0x4000, which matters when the primary header is damaged or a fixture
    # starts with that copy.
    luks_at = None
    if header.startswith(LUKS_MAGIC):
        luks_at = 0
    elif header[_LUKS2_SECONDARY_OFFSET : _LUKS2_SECONDARY_OFFSET + 6] == LUKS_MAGIC:
        luks_at = _LUKS2_SECONDARY_OFFSET
    if luks_at is not None:
        version = _luks_version(header, luks_at)
        return ContainerInfo(
            kind=ContainerKind.LUKS.value,
            signature="LUKS",
            offset=offset + luks_at,
            version=version,
            encrypted=True,
            adapter="cryptsetup",
        )

    if _find_bitlocker(header):
        return ContainerInfo(
            kind=ContainerKind.BITLOCKER.value,
            signature="-FVE-FS-",
            offset=offset,
            encrypted=True,
            adapter="dislocker",
        )

    # APFS stores a common 32-byte object header followed by NXSB (container)
    # or APSB (volume).  The superblock itself is enough to identify APFS, but
    # cannot establish FileVault status without keys/metadata interpretation.
    apfs_marker = header[_APFS_OBJECT_MAGIC_OFFSET : _APFS_OBJECT_MAGIC_OFFSET + 4]
    if apfs_marker in (APFS_CONTAINER_MAGIC, APFS_VOLUME_MAGIC):
        return ContainerInfo(
            kind=ContainerKind.APFS.value,
            signature=apfs_marker.decode("ascii"),
            offset=offset,
            encrypted=None,
            adapter="apfs-fuse",
        )

    # Legacy FileVault 2 used an Apple CoreStorage physical-volume header.
    # The CS marker is a format marker, not a password or key.  Value 2 at the
    # documented method offset indicates AES-XTS; anything else stays unknown.
    if (
        header[_CORESTORAGE_MAGIC_OFFSET : _CORESTORAGE_MAGIC_OFFSET + 2]
        == CORESTORAGE_MAGIC
    ):
        encrypted = None
        method_at = _CORESTORAGE_ENCRYPTION_OFFSET
        if len(header) >= method_at + 4:
            if struct.unpack_from("<I", header, method_at)[0] == 2:
                encrypted = True
        return ContainerInfo(
            kind=ContainerKind.FILEVAULT.value,
            signature="CS",
            offset=offset,
            encrypted=encrypted,
            adapter="apfs-fuse",
        )

    return ContainerInfo(
        kind=ContainerKind.UNKNOWN.value,
        signature="",
        offset=offset,
        encrypted=None,
        adapter=None,
    )


def detect_container(source: Source, *, offset: int = 0) -> str:
    """Return ``luks``, ``bitlocker``, ``apfs``, ``filevault`` or ``unknown``.

    This string-returning function preserves the small API used by earlier
    prototypes.  Use :func:`inspect_container` when metadata is needed.
    """

    return inspect_container(source, offset=offset).kind


# Clear aliases for callers using a crypto-specific name.
detect_crypto = detect_container
detect_crypto_container = detect_container


class CryptoContainer:
    """Explicit, read-only facade for a candidate crypto container.

    Construction and inspection are side-effect free.  Adapter operations are
    opt-in and do not mount a filesystem; callers receive the adapter's
    mapping/FUSE path and own any later cleanup.
    """

    def __init__(self, image: Source, *, offset: int = 0):
        self.image = image
        self.offset = offset

    def inspect(self) -> ContainerInfo:
        return inspect_container(self.image, offset=self.offset)

    def detect(self) -> str:
        return self.inspect().kind

    @property
    def kind(self) -> str:
        return self.detect()

    @property
    def info(self) -> ContainerInfo:
        return self.inspect()

    def adapter_status(self) -> Optional[AdapterStatus]:
        adapter = self.info.adapter
        return check_adapter(adapter) if adapter else None

    def can_open(self) -> bool:
        status = self.adapter_status()
        return bool(status and status.available)

    def open_luks(
        self,
        name: str,
        *,
        password: Optional[Union[str, bytes]] = None,
        key_file: Optional[Union[str, os.PathLike]] = None,
    ) -> str:
        """Explicitly ask ``cryptsetup`` to create a mapping; never mounts it."""

        return unlock_luks(
            self.image,
            name,
            offset=self.offset,
            password=password,
            key_file=key_file,
        )

    def open_bitlocker(
        self,
        mountpoint: Union[str, os.PathLike],
        *,
        password: Optional[Union[str, bytes]] = None,
    ) -> str:
        """Explicitly ask ``dislocker`` for its FUSE output; never mounts it."""

        return unlock_bitlocker(
            self.image,
            mountpoint,
            offset=self.offset,
            password=password,
        )

    def open_apfs(self, mountpoint: Union[str, os.PathLike]) -> str:
        """Fail explicitly: APFS/FileVault decryption is not implemented here."""

        raise CryptoOperationError(
            "APFS/FileVault was identified, but this module does not decrypt or mount it"
        )

    def close(self) -> None:
        """No-op for compatibility; adapter lifecycle is owned by the caller."""

        return None


def _canonical_adapter(name: str) -> str:
    if isinstance(name, Enum):
        name = name.value
    value = str(name or "").strip().lower()
    canonical = _ADAPTER_ALIASES.get(value, value)
    if canonical not in ADAPTER_EXECUTABLES:
        raise ValueError("unknown crypto adapter")
    return canonical


def check_adapter(
    name: str, *, which: Optional[Callable[[str], Optional[str]]] = None
) -> AdapterStatus:
    """Check one adapter by name or container kind without executing it."""

    canonical = _canonical_adapter(name)
    executable = ADAPTER_EXECUTABLES[canonical]
    finder = shutil.which if which is None else which
    path = finder(executable)
    if path:
        return AdapterStatus(canonical, executable, True, path=path)
    return AdapterStatus(
        canonical,
        executable,
        False,
        path=None,
        reason="executable not found on PATH",
    )


def check_adapters(
    *, which: Optional[Callable[[str], Optional[str]]] = None
) -> Dict[str, AdapterStatus]:
    """Return explicit availability records for all supported adapters."""

    return {name: check_adapter(name, which=which) for name in ADAPTER_EXECUTABLES}


def available_adapters(
    *, which: Optional[Callable[[str], Optional[str]]] = None
) -> Dict[str, bool]:
    """Return a compact ``executable -> available`` mapping."""

    return {
        name: status.available for name, status in check_adapters(which=which).items()
    }


def adapter_available(
    name: str, *, which: Optional[Callable[[str], Optional[str]]] = None
) -> bool:
    """Return only the availability boolean for one explicit adapter."""

    return check_adapter(name, which=which).available


def _require_adapter(name: str) -> str:
    status = check_adapter(name)
    if not status.available:
        raise AdapterUnavailable("required crypto adapter is not installed")
    # Use the resolved path, not a mutable PATH lookup during execution.
    return status.path or status.executable


def _image_argument(image: Source) -> str:
    if isinstance(image, (str, os.PathLike)):
        return os.fspath(image)
    raise TypeError("adapter operations require a filesystem image path")


def _secret_input(value: Optional[Union[str, bytes]]) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, str):
        secret = value.encode("utf-8")
    elif isinstance(value, bytes):
        secret = value
    else:
        raise TypeError("password must be str or bytes")
    if not secret:
        raise ValueError("password must not be empty")
    # cryptsetup/dislocker prompt modes consume a line; the newline is added
    # only in the private stdin buffer and is never present in argv/logs.
    return secret + b"\n"


def _validate_mapper_name(name: str) -> str:
    value = str(name)
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", value):
        raise ValueError("mapping name contains unsafe characters")
    return value


def _run_private(
    command: Sequence[str], *, secret: Optional[bytes], runner: Runner
) -> Any:
    """Run an adapter with captured output and no secret in argv or errors."""

    kwargs: Dict[str, Any] = {
        "check": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if secret is not None:
        kwargs["input"] = secret
    try:
        result = runner(list(command), **kwargs)
    except OSError as exc:
        raise CryptoOperationError("crypto adapter could not be started") from exc
    returncode = getattr(result, "returncode", 0)
    if returncode:
        raise CryptoOperationError("crypto adapter operation failed")
    return result


def unlock_luks(
    image: Union[str, os.PathLike],
    name: str,
    *,
    offset: int = 0,
    key_file: Optional[Union[str, os.PathLike]] = None,
    password: Optional[Union[str, bytes]] = None,
    runner: Optional[Runner] = None,
) -> str:
    """Create a LUKS mapping with ``cryptsetup`` without mounting it.

    A supplied password is passed through stdin using ``--key-file=-``.  It is
    never put in argv, environment variables, logs, or exception messages.
    A detached image offset is represented by cryptsetup's ``--offset`` in
    512-byte sectors; callers should prefer a partition-specific image when
    possible because not every cryptsetup build handles image offsets alike.
    """

    if offset < 0 or offset % 512:
        raise ValueError("offset must be a non-negative multiple of 512")
    if key_file is not None and password is not None:
        raise ValueError("provide key_file or password, not both")
    executable = _require_adapter("cryptsetup")
    mapping = _validate_mapper_name(name)
    secret = _secret_input(password)
    command = [executable, "luksOpen"]
    if offset:
        command.extend(["--offset", str(offset // 512)])
    if key_file is not None:
        command.extend(["--key-file", os.fspath(key_file)])
    elif secret is not None:
        command.append("--key-file=-")
    command.extend([_image_argument(image), mapping])
    _run_private(command, secret=secret, runner=runner or subprocess.run)
    return "/dev/mapper/" + mapping


def unlock_bitlocker(
    image: Union[str, os.PathLike],
    mountpoint: Union[str, os.PathLike],
    *,
    offset: int = 0,
    password: Optional[Union[str, bytes]] = None,
    runner: Optional[Runner] = None,
) -> str:
    """Run ``dislocker`` in its explicit FUSE-output mode, without mounting.

    ``dislocker`` documents ``-p`` without an attached value as an interactive
    recovery-password prompt.  When a password is supplied we feed that prompt
    through private stdin; the password is never attached to ``-p``.  Callers
    still own the returned FUSE output and must unmount/clean it according to
    their platform policy.
    """

    if offset < 0:
        raise ValueError("offset must be non-negative")
    executable = _require_adapter("dislocker")
    secret = _secret_input(password)
    command = [executable, "-V", _image_argument(image)]
    if offset:
        command.extend(["-o", str(offset)])
    command.extend(["-p", "--", os.fspath(mountpoint)])
    _run_private(command, secret=secret, runner=runner or subprocess.run)
    return os.fspath(mountpoint)


# Historical names retained as explicit aliases; they do not add decryption or
# mounting behavior beyond the safe helpers above.
check_adapter_available = adapter_available
adapter_statuses = check_adapters
