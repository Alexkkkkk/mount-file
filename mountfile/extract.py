# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Extraction of files and whole directories out of an image.

Works against providers exposing `list_dir(path)`, `read_file(path)` and
`file_size(path)`. Providers with `read_file_range` are copied in bounded
chunks; older providers retain the whole-file compatibility path.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
import unicodedata
from dataclasses import dataclass

__all__ = ["ExtractError", "ExtractionLimits", "extract_file", "extract_path"]


class ExtractError(Exception):
    pass


@dataclass(frozen=True)
class ExtractionLimits:
    """Resource ceilings for one file or one directory-tree extraction.

    The tree byte limit applies to bytes actually written; directory limits
    stop pathological depth and entry counts. A later failure can leave earlier
    files from a tree extraction in place; tree-wide rollback is not promised.
    """

    max_file_size: int = 1 << 30
    max_total_size: int = 4 << 30
    max_files: int = 100_000
    max_entries: int = 200_000
    max_depth: int = 64

    def __post_init__(self):
        for name in (
            "max_file_size",
            "max_total_size",
            "max_files",
            "max_entries",
            "max_depth",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("%s must be a non-negative integer" % name)


_DEFAULT_EXTRACTION_LIMITS = ExtractionLimits()


def _limits_or_default(limits):
    if limits is None:
        return _DEFAULT_EXTRACTION_LIMITS
    if not isinstance(limits, ExtractionLimits):
        raise TypeError("limits must be an ExtractionLimits instance")
    return limits


_HAS_DIR_FD = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)


def _safe_name(name):
    """Turn an on-image name into a name that is safe on the host FS."""
    cleaned = str(name).replace("/", "_").replace("\\", "_")
    cleaned = cleaned.replace(os.sep, "_").replace(":", "_")
    if cleaned in (".", "..", ""):
        raise ExtractError("refusing to write suspicious name %r" % name)
    return cleaned


def _join(path, name):
    return (path.rstrip("/") + "/" + name) if path != "/" else "/" + name


def _checked_directory_entries(provider, path, max_entries):
    """Reject sibling names that can map to the same host name.

    Preflight each directory before writing any of its entries. This is
    deliberately conservative across host filesystems; it does not roll back
    the complete extraction if a later directory fails.
    """
    entries = []
    seen = {}
    for entry in provider.list_dir(path):
        if len(entries) >= max_entries:
            raise ExtractError(
                "directory tree exceeds the configured entry-count limit"
            )
        name = entry["name"]
        safe_name = _safe_name(name)
        # Windows ignores trailing spaces and dots; case and Unicode rules also
        # differ across filesystems. Reject ambiguous names on every platform.
        key = unicodedata.normalize("NFKC", safe_name).casefold().rstrip(" .")
        if not key:
            raise ExtractError("refusing ambiguous destination name %r" % name)
        if key in seen:
            raise ExtractError(
                "destination name collision in %s: %r and %r" % (path, seen[key], name)
            )
        seen[key] = name
        entries.append(entry)
    return entries


def _normalize_system_path_alias(path):
    """Canonicalize only well-known POSIX system aliases before safe walking.

    macOS exposes temporary directories under /var/folders, while /var and
    /tmp are symlinks to /private/var and /private/tmp. Resolve those fixed
    prefixes only; never resolve arbitrary destination components, which must
    continue to be rejected by the no-follow walk.
    """
    path = os.path.abspath(path)
    if os.name != "posix":
        return path
    for alias in ("/var", "/tmp"):
        if (path == alias or path.startswith(alias + os.sep)) and os.path.islink(alias):
            target = os.path.realpath(alias)
            suffix = path[len(alias) :].lstrip(os.sep)
            return os.path.join(target, suffix) if suffix else target
    return path


def _find(provider, parent, name):
    """Case-insensitive lookup inside `parent`; returns the provider entry."""
    for item in provider.list_dir(parent or "/"):
        if item["name"].upper() == name.upper():
            return item
    raise ExtractError("not found in image: %s" % _join(parent or "/", name))


def _ensure_directory(path, root=None):
    """Create directories without following symlinks inside the output tree."""
    path = os.path.abspath(path)
    if root is None:
        path = _normalize_system_path_alias(path)
        if _HAS_DIR_FD:
            try:
                fd = _open_rooted_directory(os.path.sep, path, create=True)
            except OSError as exc:
                raise ExtractError(
                    "cannot safely create directory %s: %s" % (path, exc)
                ) from exc
            os.close(fd)
            return
        # Avoid following an intermediate link while creating a parent on
        # platforms without descriptor-relative APIs.
        drive, _ = os.path.splitdrive(path)
        root = drive + os.sep

    root = os.path.abspath(root)
    path = _ensure_within(path, root)
    if _HAS_DIR_FD:
        try:
            fd = _open_rooted_directory(root, path, create=True)
        except OSError as exc:
            raise ExtractError(
                "cannot safely create directory %s: %s" % (path, exc)
            ) from exc
        os.close(fd)
        return

    current = root
    relative = os.path.relpath(path, root)
    for component in () if relative == "." else relative.split(os.sep):
        current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except FileExistsError:
                # Re-check below: another process may have created a symlink.
                pass
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise ExtractError(
                    "cannot safely create directory %s: %s" % (current, exc)
                ) from exc
        except OSError as exc:
            raise ExtractError(
                "cannot safely inspect directory %s: %s" % (current, exc)
            ) from exc
        reparse_point = bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse_point:
            raise ExtractError("refusing to follow destination symlink: %s" % current)
        if not stat.S_ISDIR(info.st_mode):
            raise ExtractError("destination component is not a directory: %s" % current)


def _ensure_within(path, root):
    """Reject output paths that are lexically outside the extraction root."""
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    try:
        if os.path.commonpath((root, path)) != root:
            raise ExtractError("destination escapes extraction directory")
    except ValueError as exc:
        raise ExtractError("destination escapes extraction directory") from exc
    return path


def _open_rooted_directory(root, path, create=False):
    """Open a directory beneath root without following symlink components."""
    root = os.path.abspath(root)
    path = _ensure_within(path, root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        relative = os.path.relpath(path, root)
        components = () if relative == "." else relative.split(os.sep)
        for component in components:
            if component in ("", ".", ".."):
                raise ExtractError("destination escapes extraction directory")
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


_COPY_CHUNK_SIZE = 1 << 20
_DEFAULT_PROVIDER_READ_LIMIT = 1 << 20


def _write_provider_contents(provider, src_path, output, max_bytes):
    """Stream providers with ranged reads; retain compatibility with old providers."""
    read_range = getattr(provider, "read_file_range", None)
    get_size = getattr(provider, "file_size", None)
    if not callable(read_range) or not callable(get_size):
        data = provider.read_file(src_path)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ExtractError("provider returned data that is not bytes-like")
        if len(data) > max_bytes:
            raise ExtractError("file exceeds the configured extraction size limit")
        output.write(data)
        return len(data)

    size = get_size(src_path)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ExtractError("provider returned an invalid file size")
    if size > max_bytes:
        raise ExtractError("file exceeds the configured extraction size limit")
    provider_limit = getattr(
        provider, "max_read_range_bytes", _DEFAULT_PROVIDER_READ_LIMIT
    )
    if (
        not isinstance(provider_limit, int)
        or isinstance(provider_limit, bool)
        or provider_limit <= 0
    ):
        provider_limit = _DEFAULT_PROVIDER_READ_LIMIT
    chunk_size = min(_COPY_CHUNK_SIZE, provider_limit)
    written = 0
    while written < size:
        requested = min(chunk_size, size - written)
        chunk = read_range(src_path, written, requested)
        if not chunk:
            raise ExtractError("provider returned a short read during extraction")
        if len(chunk) > requested:
            raise ExtractError("provider returned more data than requested")
        output.write(chunk)
        written += len(chunk)
    return written


def _write_file(provider, src_path, dst_path, root=None, max_bytes=None):
    if max_bytes is None:
        max_bytes = _DEFAULT_EXTRACTION_LIMITS.max_file_size
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    dst_path = os.path.abspath(dst_path)
    if root is not None:
        dst_path = _ensure_within(dst_path, root)
    else:
        dst_path = _normalize_system_path_alias(dst_path)
    parent = os.path.dirname(dst_path)
    _ensure_directory(parent, root=root)

    if _HAS_DIR_FD:
        parent_fd = (
            _open_rooted_directory(root, parent, create=True)
            if root is not None
            else _open_rooted_directory(os.path.sep, parent, create=True)
        )
        try:
            name = os.path.basename(dst_path)
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode):
                raise ExtractError(
                    "refusing to follow destination symlink: %s" % dst_path
                )
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ExtractError(
                    "refusing to replace non-regular destination: %s" % dst_path
                )

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            temp_name = None
            fd = None
            for _attempt in range(8):
                candidate = ".mountfile-%s.tmp" % secrets.token_hex(8)
                try:
                    fd = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
                    temp_name = candidate
                    break
                except FileExistsError:
                    continue
            if fd is None:
                raise OSError("could not allocate a unique temporary file")
            try:
                with os.fdopen(fd, "wb") as output:
                    fd = None
                    written = _write_provider_contents(
                        provider, src_path, output, max_bytes
                    )
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(
                    temp_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temp_name = None
            finally:
                if fd is not None:
                    os.close(fd)
                if temp_name is not None:
                    try:
                        os.unlink(temp_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            return written
        except ExtractError:
            raise
        except OSError as exc:
            raise ExtractError(
                "cannot safely write destination file %s: %s" % (dst_path, exc)
            ) from exc
        finally:
            os.close(parent_fd)

    try:
        info = os.lstat(dst_path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise ExtractError(
            "cannot safely inspect destination file %s: %s" % (dst_path, exc)
        ) from exc
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise ExtractError("refusing to follow destination symlink: %s" % dst_path)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise ExtractError("refusing to replace non-regular destination: %s" % dst_path)

    fd, temp_path = tempfile.mkstemp(prefix=".mountfile-", suffix=".tmp", dir=parent)
    written = 0
    try:
        with os.fdopen(fd, "wb") as output:
            fd = None
            written = _write_provider_contents(provider, src_path, output, max_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, dst_path)
    except BaseException as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        if isinstance(exc, OSError):
            raise ExtractError(
                "cannot safely write destination file %s: %s" % (dst_path, exc)
            ) from exc
        raise
    return written


def extract_file(provider, src_path, dst_path, *, limits=None):
    """Write one file out of the image; returns the number of bytes written."""
    limits = _limits_or_default(limits)
    return _write_file(
        provider,
        src_path,
        dst_path,
        max_bytes=min(limits.max_file_size, limits.max_total_size),
    )


def extract_path(provider, src_path, dst_dir, progress=None, *, limits=None):
    """Extract `src_path` (file or directory) into the host directory `dst_dir`.

    A directory is recreated under `dst_dir` with the same name, empty
    subdirectories included; the volume root ("/") is extracted directly into
    `dst_dir`. Returns the number of regular files written.
    """
    limits = _limits_or_default(limits)
    src = "/" + str(src_path or "/").strip().strip("/")
    if src == "/":
        parent, name, is_root = "/", "", True
    else:
        is_root = False
        slash = src.rfind("/")
        parent = src[:slash] or "/"
        name = src[slash + 1 :]

    if is_root:
        is_dir = True
    else:
        is_dir = bool(_find(provider, parent, name)["is_dir"])

    root = os.path.abspath(dst_dir)
    _ensure_directory(root)
    root = os.path.realpath(root)
    total = 0
    if not is_dir:
        if limits.max_files < 1:
            raise ExtractError("extraction exceeds the configured file-count limit")
        dst_path = _ensure_within(os.path.join(root, _safe_name(name)), root)
        _write_file(
            provider,
            src,
            dst_path,
            root=root,
            max_bytes=min(limits.max_file_size, limits.max_total_size),
        )
        if progress:
            progress(src)
        return 1

    base = (
        root if is_root else _ensure_within(os.path.join(root, _safe_name(name)), root)
    )
    _ensure_directory(base, root=root)
    total_bytes = 0
    total_entries = 0
    stack = [(src, base, 0)]
    while stack:
        src_dir, dst_dir_now, depth = stack.pop()
        entries = _checked_directory_entries(
            provider, src_dir, limits.max_entries - total_entries
        )
        total_entries += len(entries)
        for item in entries:
            child_src = _join(src_dir, item["name"])
            child_dst = _ensure_within(
                os.path.join(dst_dir_now, _safe_name(item["name"])), root
            )
            if item["is_dir"]:
                child_depth = depth + 1
                if child_depth > limits.max_depth:
                    raise ExtractError(
                        "directory tree exceeds the configured depth limit"
                    )
                _ensure_directory(child_dst, root=root)
                stack.append((child_src, child_dst, child_depth))
            else:
                if total >= limits.max_files:
                    raise ExtractError(
                        "directory tree exceeds the configured file-count limit"
                    )
                remaining = limits.max_total_size - total_bytes
                max_bytes = min(limits.max_file_size, remaining)
                written = _write_file(
                    provider,
                    child_src,
                    child_dst,
                    root=root,
                    max_bytes=max_bytes,
                )
                total_bytes += written
                total += 1
                if progress:
                    progress(child_src)
    return total
