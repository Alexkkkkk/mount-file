# SPDX-License-Identifier: MIT
"""Small shared contracts for read-only VFS providers.

This module contains provider helpers only; the public VFS implementation and
routing remain in :mod:`mountfile.vfs`.
"""

from __future__ import annotations

from mountfile.capabilities import VFS_READ_CAPABILITIES, VFS_WRITE_CAPABILITIES


class ReadOnlyVFSStatMixin:
    """Expose a stable ``stat(path)`` result through the provider's own listing.

    Providers keep ownership of parsing and path resolution in ``list_dir``.
    The mixin therefore does not add a second path resolver or read metadata
    directly from the image. A provider may opt into case-insensitive matching
    when that matches its on-disk namespace.
    """

    case_insensitive_paths = False

    def stat(self, path: str) -> dict:
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise ValueError("invalid VFS path")
        parts = []
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError("parent traversal is not allowed in VFS paths")
            parts.append(part)
        if not parts:
            return {
                "path": "/",
                "name": "/",
                "is_dir": True,
                "size": 0,
                "date": "",
            }

        actual_parts = []
        entry = None
        for index, name in enumerate(parts):
            parent = "/" + "/".join(actual_parts) if actual_parts else "/"
            candidates = self.list_dir(parent)
            entry = next(
                (item for item in candidates if item.get("name") == name), None
            )
            if entry is None and self.case_insensitive_paths:
                folded = name.casefold()
                entry = next(
                    (
                        item
                        for item in candidates
                        if str(item.get("name", "")).casefold() == folded
                    ),
                    None,
                )
            if entry is None:
                raise FileNotFoundError("VFS path not found: " + "/".join(parts))
            actual_parts.append(str(entry["name"]))
            if index < len(parts) - 1 and not bool(entry.get("is_dir", False)):
                raise NotADirectoryError("VFS path component is not a directory")

        actual_path = "/" + "/".join(actual_parts)
        result = dict(entry)
        result["name"] = str(entry["name"])
        result["path"] = actual_path
        result["is_dir"] = bool(entry.get("is_dir", False))
        result["size"] = int(entry.get("size", 0))
        result.setdefault("date", "")
        return result

    @property
    def read_capabilities(self) -> tuple[str, ...]:
        """Operations implemented by every reader using this API contract."""
        return VFS_READ_CAPABILITIES

    @property
    def write_capabilities(self) -> tuple[str, ...]:
        """Mutations exposed by this provider.

        Native readers are deliberately read-only. Keeping this explicit in
        the provider contract prevents callers from inferring write support
        from the fact that a filesystem is browsable.
        """
        return VFS_WRITE_CAPABILITIES

    @property
    def backend_status(self) -> dict:
        """Return a stable, serialisable capability/status description.

        The result is metadata-only. It does not probe the image again and it
        never claims that a legacy writer is available. Mutable backends may
        override this property and report their own bounded write capabilities.
        """
        backend = str(getattr(self, "backend_name", type(self).__name__))
        return {
            "backend": backend,
            "mode": "read-only",
            "read_capabilities": list(self.read_capabilities),
            "write_capabilities": list(self.write_capabilities),
            "write_enabled": False,
            "write_rejection_reason": (
                "native provider is read-only; use an explicitly selected "
                "bounded writer profile"
            ),
        }
