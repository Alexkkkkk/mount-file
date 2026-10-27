# SPDX-License-Identifier: MIT
"""Small shared contracts for read-only VFS providers.

This module contains provider helpers only; the public VFS implementation and
routing remain in :mod:`mountfile.vfs`.
"""

from __future__ import annotations


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
        return ("list", "stat", "read-range", "extract")
