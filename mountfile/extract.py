# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Extraction of files and whole directories out of an image.

Works against any provider exposing `list_dir(path)`, `read_file(path)` and
`file_size(path)` - that is :class:`mountfile.vfs.TskImage`,
:class:`mountfile.fat32.Fat32Provider` and the test doubles.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile

__all__ = ["ExtractError", "extract_file", "extract_path"]


class ExtractError(Exception):
    pass


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
        try:
            os.makedirs(path, exist_ok=True)
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise ExtractError(
                "cannot safely create directory %s: %s" % (path, exc)
            ) from exc
        if stat.S_ISLNK(mode):
            raise ExtractError("refusing to follow destination symlink: %s" % path)
        if not stat.S_ISDIR(mode):
            raise ExtractError("destination component is not a directory: %s" % path)
        return

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
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except FileExistsError:
                # Re-check below: another process may have created a symlink.
                pass
            try:
                mode = os.lstat(current).st_mode
            except OSError as exc:
                raise ExtractError(
                    "cannot safely create directory %s: %s" % (current, exc)
                ) from exc
        except OSError as exc:
            raise ExtractError(
                "cannot safely inspect directory %s: %s" % (current, exc)
            ) from exc
        if stat.S_ISLNK(mode):
            raise ExtractError("refusing to follow destination symlink: %s" % current)
        if not stat.S_ISDIR(mode):
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


def _write_file(provider, src_path, dst_path, root=None):
    dst_path = os.path.abspath(dst_path)
    if root is not None:
        dst_path = _ensure_within(dst_path, root)
    parent = os.path.dirname(dst_path)
    _ensure_directory(parent, root=root)

    if _HAS_DIR_FD:
        parent_fd = (
            _open_rooted_directory(root, parent, create=True)
            if root is not None
            else os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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

            data = provider.read_file(src_path)
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
                    output.write(data)
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
            return len(data)
        except ExtractError:
            raise
        except OSError as exc:
            raise ExtractError(
                "cannot safely write destination file %s: %s" % (dst_path, exc)
            ) from exc
        finally:
            os.close(parent_fd)

    try:
        mode = os.lstat(dst_path).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise ExtractError(
            "cannot safely inspect destination file %s: %s" % (dst_path, exc)
        ) from exc
    if mode is not None and stat.S_ISLNK(mode):
        raise ExtractError("refusing to follow destination symlink: %s" % dst_path)
    if mode is not None and not stat.S_ISREG(mode):
        raise ExtractError("refusing to replace non-regular destination: %s" % dst_path)

    data = provider.read_file(src_path)
    fd, temp_path = tempfile.mkstemp(prefix=".mountfile-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, dst_path)
    except OSError as exc:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise ExtractError(
            "cannot safely write destination file %s: %s" % (dst_path, exc)
        ) from exc
    return len(data)


def extract_file(provider, src_path, dst_path):
    """Write one file out of the image; returns the number of bytes written."""
    return _write_file(provider, src_path, dst_path)


def extract_path(provider, src_path, dst_dir, progress=None):
    """Extract `src_path` (file or directory) into the host directory `dst_dir`.

    A directory is recreated under `dst_dir` with the same name, empty
    subdirectories included; the volume root ("/") is extracted directly into
    `dst_dir`. Returns the number of regular files written.
    """
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
        dst_path = _ensure_within(os.path.join(root, _safe_name(name)), root)
        _write_file(provider, src, dst_path, root=root)
        if progress:
            progress(src)
        return 1

    base = (
        root if is_root else _ensure_within(os.path.join(root, _safe_name(name)), root)
    )
    _ensure_directory(base, root=root)
    stack = [(src, base)]
    while stack:
        src_dir, dst_dir_now = stack.pop()
        for item in provider.list_dir(src_dir):
            child_src = _join(src_dir, item["name"])
            child_dst = _ensure_within(
                os.path.join(dst_dir_now, _safe_name(item["name"])), root
            )
            if item["is_dir"]:
                _ensure_directory(child_dst, root=root)
                stack.append((child_src, child_dst))
            else:
                _write_file(provider, child_src, child_dst, root=root)
                total += 1
                if progress:
                    progress(child_src)
    return total
