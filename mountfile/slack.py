# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only extraction of the physical tail of the cluster containing EOF.

Runs are ordered in *logical* file order, not by physical address. Preallocated
clusters after EOF are not file slack. Sparse/compressed/encrypted or incomplete
run maps cannot safely locate physical slack and are explicitly refused.
Filesystem offsets are byte offsets in the image; the default is a filesystem
starting at byte zero. No allocation-state discovery is performed here.
"""
from __future__ import annotations

import operator
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_MIN_SLACK = 1
DEFAULT_CHUNK = 8 * 1024 * 1024
MAX_READ = 64 * 1024 * 1024
MAX_RUNS = 1_000_000


class SlackError(RuntimeError):
    """Raised when slack cannot safely be located/read for the supplied image."""


@dataclass(frozen=True)
class SlackRegion:
    """A contiguous run of slack bytes belonging to one file."""

    path: str
    offset: int
    length: int
    allocated_size: int
    real_size: int
    kind: str = "slack"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "offset": self.offset,
            "length": self.length,
            "allocated_size": self.allocated_size,
            "real_size": self.real_size,
            "kind": self.kind,
        }


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError("bool is not an offset/size")
        number = operator.index(value)
    except TypeError as exc:
        raise SlackError(f"{name} must be an integer") from exc
    if number < 0 or (positive and number == 0):
        raise SlackError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return number


def _runs(meta_file: Any, real_size: Optional[int] = None) -> list[tuple[int, int]]:
    """Select only the unnamed default data stream and verify its dense run map.

    TSK's default data type is 1; NTFS $DATA is 128. Attribute metadata and
    alternate named streams must not be combined with the file's data runs.
    Minimal protocol objects without attribute info remain supported only when
    exactly one attribute is supplied, with dense runs in logical order.
    """
    selected = []
    for index, attribute in enumerate(meta_file):
        if index >= 256:
            raise SlackError("too many file attributes")
        info = getattr(attribute, "info", None)
        if info is None:
            selected.append(attribute)
            continue
        if getattr(info, "type", None) not in (1, 128):
            continue
        if getattr(info, "name", None) not in (None, b"", ""):
            continue
        selected.append(attribute)
    if not selected:
        if real_size:
            raise SlackError("default data stream is unavailable")
        return []
    if len(selected) != 1:
        raise SlackError("ambiguous default data stream")
    attribute = selected[0]
    info = getattr(attribute, "info", None)
    if info is not None:
        flags = _integer(getattr(info, "flags", 0), "attribute flags")
        # TSK_FS_ATTR_ENC/COMP/SPARSE/RECOVERY: physical mapping is unsafe.
        if flags & (16 | 32 | 64 | 128):
            raise SlackError("sparse/compressed/encrypted/recovery data is unsupported")
        if real_size is not None and getattr(info, "size", real_size) != real_size:
            raise SlackError("default stream size disagrees with file metadata")
        if flags & 4:  # TSK_FS_ATTR_RES: resident data has no cluster tail.
            return []
    collected = []
    cursor = 0
    for index, run in enumerate(attribute):
        if index >= MAX_RUNS:
            raise SlackError("too many data runs")
        start = _integer(getattr(run, "addr", None), "run address")
        count = _integer(getattr(run, "len", None), "run length", positive=True)
        flags = _integer(getattr(run, "flags", 0), "run flags")
        if flags:
            raise SlackError("sparse/filler/encrypted or unknown data run")
        logical = _integer(getattr(run, "offset", cursor), "logical run offset")
        collected.append((logical, start, count))
        cursor = logical + count
    collected.sort()
    if not collected and real_size:
        raise SlackError("non-resident data run map is unavailable")
    cursor = 0
    result = []
    for logical, start, count in collected:
        if logical != cursor:
            raise SlackError("incomplete or overlapping logical run map")
        result.append((start, count))
        cursor += count
    return result


def slack_region_for(
    path: str,
    real_size: int,
    runs: list[tuple[int, int]],
    block_size: int,
) -> Optional[SlackRegion]:
    """Locate EOF-cluster slack from dense runs in logical file order.

    The returned allocated_size describes all runs, but length is at most one
    block minus one byte, irrespective of fragmentation or preallocation.
    """
    real_size = _integer(real_size, "real_size")
    block_size = _integer(block_size, "block_size", positive=True)
    if len(runs) > MAX_RUNS:
        raise SlackError("too many data runs")
    checked = [
        (_integer(start, "run address"), _integer(count, "run length", positive=True))
        for start, count in runs
    ]
    allocated = sum(count for _start, count in checked) * block_size
    if not checked or real_size == 0:
        return None
    if real_size > allocated:
        raise SlackError("run map does not cover file EOF")
    remainder = real_size % block_size
    if not remainder:
        return None
    eof_block = real_size // block_size
    logical = 0
    for start, count in checked:
        if logical <= eof_block < logical + count:
            offset = (start + eof_block - logical) * block_size + remainder
            return SlackRegion(
                path=path,
                offset=offset,
                length=block_size - remainder,
                allocated_size=allocated,
                real_size=real_size,
            )
        logical += count
    raise SlackError("run map does not locate file EOF")


def _iter_file_entries(fs: Any) -> Iterator[tuple[str, Any]]:
    """Strict, depth-bounded traversal: damaged directories are not clean scans."""
    stack = [(fs.open_dir("/"), "", 0)]
    root = getattr(fs.info, "root_inum", None)
    seen = {root} if root is not None else set()
    while stack:
        directory, prefix, depth = stack.pop()
        for entry in directory:
            meta = getattr(entry.info, "meta", None)
            if meta is None:
                continue
            # Deleted directory entries do not describe allocated file slack.
            if not getattr(meta, "flags", 1) & 1:
                continue
            name = entry.info.name.name
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if not name or name in (".", ".."):
                continue
            path = f"{prefix}/{name}"
            if meta.type == 2:
                inode = meta.addr
                if inode in seen:
                    continue
                if depth >= 256:
                    raise SlackError("filesystem directory depth exceeds 256")
                seen.add(inode)
                stack.append((entry.as_directory(), path, depth + 1))
            elif meta.type == 1:
                yield path, entry


def find_slack_regions_from_fs(
    fs: Any,
    *,
    min_slack: int = DEFAULT_MIN_SLACK,
    max_file_size: Optional[int] = None,
    filesystem_offset: int = 0,
) -> list[SlackRegion]:
    """Locate slack in an open filesystem with an explicitly supplied origin."""
    min_slack = _integer(min_slack, "min_slack", positive=True)
    filesystem_offset = _integer(filesystem_offset, "filesystem_offset")
    if max_file_size is not None:
        max_file_size = _integer(max_file_size, "max_file_size")
    try:
        block_size = _integer(fs.info.block_size, "block_size", positive=True)
        block_count = getattr(fs.info, "block_count", None)
        if block_count is not None:
            block_count = _integer(block_count, "block_count", positive=True)
        regions = []
        for path, entry in _iter_file_entries(fs):
            real_size = _integer(entry.info.meta.size, "real_size")
            if max_file_size is not None and real_size > max_file_size:
                continue
            if real_size == 0 or real_size % block_size == 0:
                # No EOF-cluster tail exists, even if there is preallocation.
                continue
            runs = _runs(fs.open_meta(inode=entry.info.meta.addr), real_size)
            if block_count is not None and any(
                start + count > block_count for start, count in runs
            ):
                raise SlackError("data run extends beyond filesystem")
            region = slack_region_for(path, real_size, runs, block_size)
            if region is not None and region.length >= min_slack:
                regions.append(
                    replace(region, offset=region.offset + filesystem_offset)
                )
        return regions
    except SlackError:
        raise
    except Exception as exc:
        raise SlackError(f"cannot locate slack: {exc}") from exc


def find_slack_regions(
    image_path: os.PathLike | str, *, offset: int = 0, **kwargs: Any
) -> list[SlackRegion]:
    """Locate slack in an image (needs pytsk3), validating physical bounds."""
    from .timeline import open_tsk_fs

    offset = _integer(offset, "offset")
    if "filesystem_offset" in kwargs:
        raise SlackError("use offset to specify the image filesystem origin")
    try:
        regions = find_slack_regions_from_fs(
            open_tsk_fs(image_path, offset=offset), filesystem_offset=offset, **kwargs
        )
        size = Path(image_path).stat().st_size
        for region in regions:
            _check_bounds(region.offset, region.length, size)
        return regions
    except SlackError:
        raise
    except Exception as exc:
        raise SlackError(f"cannot open slack source: {exc}") from exc


def _check_bounds(offset: int, length: int, size: int) -> None:
    offset = _integer(offset, "offset")
    length = _integer(length, "length")
    if offset > size or length > size - offset:
        raise SlackError("slack region extends beyond image")


def read_region(image_path: os.PathLike | str, offset: int, length: int) -> bytes:
    """Read exactly a bounded image region; short reads are explicit errors."""
    _integer(offset, "offset")
    _integer(length, "length")
    if length > MAX_READ:
        raise SlackError("region too large for read_region; use iter_slack_bytes")
    try:
        with open(image_path, "rb") as stream:
            _check_bounds(offset, length, os.fstat(stream.fileno()).st_size)
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise SlackError("short read while reading slack")
            return data
    except OSError as exc:
        raise SlackError(f"cannot read slack: {exc}") from exc


def _safe_name(path: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace("\\", "_") or "root"
    return "".join(
        char if char.isalnum() or char in "._-" else "_" for char in cleaned
    )[:120]


def extract_slack(
    image_path: os.PathLike | str,
    out_dir: os.PathLike | str,
    *,
    min_slack: int = DEFAULT_MIN_SLACK,
    max_file_size: Optional[int] = None,
    regions: Optional[list[SlackRegion]] = None,
) -> list[SlackRegion]:
    """Stream regions to separate new files; never overwrite the image/artifacts."""
    min_slack = _integer(min_slack, "min_slack", positive=True)
    if max_file_size is not None:
        max_file_size = _integer(max_file_size, "max_file_size")
    path = Path(image_path)
    if not path.is_file():
        raise SlackError("image not found: %s" % path)
    located = (
        regions
        if regions is not None
        else find_slack_regions(path, min_slack=min_slack, max_file_size=max_file_size)
    )
    size = path.stat().st_size
    for region in located:
        _check_bounds(region.offset, region.length, size)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for index, region in enumerate(located):
        if region.length < min_slack:
            continue
        if max_file_size is not None and region.real_size > max_file_size:
            continue
        out = target / ("slack_%04d_%s.bin" % (index, _safe_name(region.path)))
        created = False
        try:
            with out.open("xb") as stream:
                created = True
                for _region, data in iter_slack_bytes(path, [region]):
                    stream.write(data)
            written.append(region)
        except Exception:
            if created:
                out.unlink()
            raise
    return written


def iter_slack_bytes(
    image_path: os.PathLike | str,
    regions: list[SlackRegion],
    *,
    chunk: int = DEFAULT_CHUNK,
) -> Iterator[tuple[SlackRegion, bytes]]:
    """Stream exact bounded chunks; validate all ranges before the first yield."""
    chunk = _integer(chunk, "chunk", positive=True)
    if chunk > MAX_READ:
        raise SlackError("chunk exceeds bounded read limit")
    try:
        # Do not prefetch later chunks: a subsequent short read must be visible.
        with open(image_path, "rb", buffering=0) as stream:
            size = os.fstat(stream.fileno()).st_size
            for region in regions:
                _check_bounds(region.offset, region.length, size)
            for region in regions:
                remaining = region.length
                cursor = region.offset
                while remaining:
                    want = min(chunk, remaining)
                    stream.seek(cursor)
                    data = stream.read(want)
                    if len(data) != want:
                        raise SlackError("short read while streaming slack")
                    yield region, data
                    cursor += want
                    remaining -= want
    except OSError as exc:
        raise SlackError(f"cannot stream slack: {exc}") from exc
