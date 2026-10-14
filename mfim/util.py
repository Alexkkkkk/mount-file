"""Small helpers shared by the rest of the package."""

from __future__ import annotations

import hashlib
import os
from typing import Optional

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(n: Optional[int]) -> str:
    """1234567 -> '1.18 MiB'. ``None`` renders as '-'."""
    if n is None:
        return "-"
    n = float(n)
    for unit in _UNITS:
        if abs(n) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"


def human_time(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    import datetime

    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "-"


def decode_name(raw, errors: str = "replace") -> str:
    """Decode an on-disk name.

    On-disk names are bytes and are frequently not valid UTF-8 (Windows-1251
    labels, truncated multi-byte sequences, deleted entries with garbage).
    We never let a bad name kill a directory listing.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    return raw.decode("utf-8", errors)


def normalize_path(path: str) -> str:
    """'/a\\\\b//c/' -> '/a/b/c'. Filesystem-internal paths are always '/'-rooted."""
    if path is None or path == "":
        return "/"
    p = str(path).replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    return "/" + "/".join(parts)


def join_fs_path(parent: str, name: str) -> str:
    return normalize_path(parent) + ("" if parent.endswith("/") else "/") + name


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            data = fp.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_size(path: str) -> int:
    return os.path.getsize(path)
