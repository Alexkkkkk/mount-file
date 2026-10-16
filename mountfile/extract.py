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

__all__ = ["ExtractError", "extract_file", "extract_path"]


class ExtractError(Exception):
    pass


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


def extract_file(provider, src_path, dst_path):
    """Write one file out of the image; returns the number of bytes written."""
    data = provider.read_file(src_path)
    parent = os.path.dirname(dst_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dst_path, "wb") as f:
        f.write(data)
    return len(data)


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

    total = 0
    if not is_dir:
        extract_file(provider, src, os.path.join(dst_dir, _safe_name(name)))
        if progress:
            progress(src)
        return 1

    base = dst_dir if is_root else os.path.join(dst_dir, _safe_name(name))
    os.makedirs(base, exist_ok=True)
    stack = [(src, base)]
    while stack:
        src_dir, dst_dir_now = stack.pop()
        for item in provider.list_dir(src_dir):
            child_src = _join(src_dir, item["name"])
            child_dst = os.path.join(dst_dir_now, _safe_name(item["name"]))
            if item["is_dir"]:
                os.makedirs(child_dst, exist_ok=True)
                stack.append((child_src, child_dst))
            else:
                extract_file(provider, child_src, child_dst)
                total += 1
                if progress:
                    progress(child_src)
    return total
