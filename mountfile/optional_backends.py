# SPDX-License-Identifier: MIT
"""Discover optional filesystem backends without enabling unsafe writes.

The inventory is deliberately descriptive: an installed parser is not an
acceptance oracle, and a system writer is not automatically used by the native
transactional paths.  Callers can use the result in evidence reports and show
``unavailable`` instead of silently treating a missing dependency as success.
"""

from __future__ import annotations

import ctypes.util
import importlib.util
import shutil
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class BackendStatus:
    """Availability and explicitly bounded capability of one backend."""

    name: str
    kind: str
    available: bool
    capability: str
    detail: str
    license: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _module(
    name: str,
    *,
    import_name: str,
    capability: str,
    license: str,
    detail: str,
) -> BackendStatus:
    available = importlib.util.find_spec(import_name) is not None
    return BackendStatus(
        name=name,
        kind="python-module",
        available=available,
        capability=capability if available else "unavailable",
        detail=detail if available else f"{import_name} is not installed",
        license=license,
    )


def _tool(
    name: str, *, capability: str, detail: str, license: str | None
) -> BackendStatus:
    path = shutil.which(name)
    return BackendStatus(
        name=name,
        kind="system-tool",
        available=path is not None,
        capability=capability if path else "unavailable",
        detail=f"{path}: {detail}" if path else f"{name} is not on PATH",
        license=license,
    )


def inventory() -> dict[str, BackendStatus]:
    """Return the optional backend inventory used by evidence tooling.

    ``libfsntfs`` and ``dfir_ntfs`` are read-only parsers.  ``ntfs-3g`` tools
    may write an image, but are external GPL utilities and are not the native
    replay implementation.  The inventory therefore never reports them as a
    product ``write/replay`` backend.
    """
    return {
        "libext2fs": BackendStatus(
            name="libext2fs",
            kind="shared-library",
            available=ctypes.util.find_library("ext2fs") is not None,
            capability="native-ext4-write-gated",
            detail=(
                "used by mountfile.ext4_native_writer through ctypes"
                if ctypes.util.find_library("ext2fs")
                else "install the e2fsprogs libext2fs runtime"
            ),
            license="LGPL-2.1-or-later",
        ),
        "pyfsntfs": _module(
            "pyfsntfs",
            import_name="pyfsntfs",
            capability="read-only-independent-checker-candidate",
            detail="LGPL libfsntfs Python binding; no write or replay API",
            license="LGPL-3.0-or-later",
        ),
        "dfir_ntfs": _module(
            "dfir_ntfs",
            import_name="dfir_ntfs",
            capability="read-only-secondary-parser",
            detail="external GPL parser; never bundled or used for publication",
            license="GPL-3.0",
        ),
        "e2fsck": _tool(
            "e2fsck",
            capability="authoritative-ext4-checker",
            detail="read-only -fn checks are accepted by the harness",
            license="GPL-2.0-or-later",
        ),
        "ntfsfix": _tool(
            "ntfsfix",
            capability="secondary-ntfs-diagnostic",
            detail="never authoritative for Windows parity",
            license="GPL-2.0-or-later",
        ),
        "ntfs-3g": _tool(
            "ntfs-3g",
            capability="external-ntfs-driver-not-native-replay",
            detail="not invoked by the native replay executor",
            license="GPL-2.0-or-later",
        ),
        "ntfscp": _tool(
            "ntfscp",
            capability="staged-ntfs-file-write-helper",
            detail="used only on disposable copies with read-back",
            license="GPL-2.0-or-later",
        ),
        "ntfscat": _tool(
            "ntfscat",
            capability="staged-ntfs-read-back-helper",
            detail="used as a secondary read-back check",
            license="GPL-2.0-or-later",
        ),
    }


def inventory_dict() -> dict[str, dict[str, Any]]:
    """Return JSON-serializable backend inventory data."""
    return {name: status.as_dict() for name, status in inventory().items()}


__all__ = ["BackendStatus", "inventory", "inventory_dict"]
