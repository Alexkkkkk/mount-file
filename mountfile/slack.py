# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Slack space extraction (Task 1.6, issue #87).

"Slack" is the tail of the last allocated cluster/block of a file that lies
beyond the file's real end.  It frequently holds the *previous* content of the
file -- a gold mine for DFIR -- and it is invisible through a normal mount.

Unlike a naive implementation that records the slack length but not where it
lives, this module computes the **absolute offset** of the slack: the slack
always sits at the end of the last allocated run, so::

    slack_end    = (last_run.addr + last_run.len) * block_size
    slack_offset = slack_end - (allocated_size - real_size)

Read-only: nothing is written into the evidence image, extracted bytes go to a
separate output directory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .timeline import iter_file_entries

#: Slack shorter than this is not worth a separate artefact.
DEFAULT_MIN_SLACK = 1
DEFAULT_CHUNK = 8 * 1024 * 1024


class SlackError(RuntimeError):
    """Raised when slack cannot be located for the supplied image."""


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


def _runs(meta_file: Any) -> list[tuple[int, int]]:
    """Return ``[(start_block, block_count), ...]`` for one inode."""

    collected: list[tuple[int, int]] = []
    for attribute in meta_file:
        for run in attribute:
            length = int(getattr(run, "len", 0) or 0)
            if length <= 0:
                continue
            collected.append((int(getattr(run, "addr", 0) or 0), length))
    return collected


def slack_region_for(
    path: str,
    real_size: int,
    runs: list[tuple[int, int]],
    block_size: int,
) -> Optional[SlackRegion]:
    """Compute the slack region of one file, or ``None`` when there is none."""

    if real_size <= 0 or not runs or block_size <= 0:
        return None
    allocated = sum(count for _start, count in runs) * block_size
    if allocated <= real_size:
        return None
    slack_length = allocated - real_size
    last_start, last_count = runs[-1]
    slack_end = (last_start + last_count) * block_size
    offset = slack_end - slack_length
    if offset < 0:
        return None
    return SlackRegion(
        path=path,
        offset=offset,
        length=slack_length,
        allocated_size=allocated,
        real_size=real_size,
    )


def find_slack_regions_from_fs(
    fs: Any,
    *,
    min_slack: int = DEFAULT_MIN_SLACK,
    max_file_size: Optional[int] = None,
) -> list[SlackRegion]:
    """Locate the slack region of every file in an open pytsk3 filesystem."""

    block_size = int(getattr(fs.info, "block_size", 512) or 512)
    regions: list[SlackRegion] = []
    for path, entry in iter_file_entries(fs):
        meta = entry.info.meta
        real_size = int(getattr(meta, "size", 0) or 0)
        if max_file_size is not None and real_size > max_file_size:
            continue
        try:
            meta_file = fs.open_meta(inode=meta.addr)
        except Exception:  # pragma: no cover - damaged inode
            continue
        region = slack_region_for(path, real_size, _runs(meta_file), block_size)
        if region is not None and region.length >= min_slack:
            regions.append(region)
    return regions


def find_slack_regions(image_path: os.PathLike | str, **kwargs: Any) -> list[SlackRegion]:
    """Locate slack regions in ``image_path`` (needs pytsk3)."""

    from .timeline import open_tsk_fs

    return find_slack_regions_from_fs(open_tsk_fs(image_path), **kwargs)


def read_region(image_path: os.PathLike | str, offset: int, length: int) -> bytes:
    """Read ``length`` bytes at ``offset`` from the image."""

    if length <= 0:
        return b""
    with open(image_path, "rb") as stream:
        stream.seek(offset)
        return stream.read(length)


def _safe_name(path: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace("\\", "_") or "root"
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in cleaned)[:120]


def extract_slack(
    image_path: os.PathLike | str,
    out_dir: os.PathLike | str,
    *,
    min_slack: int = DEFAULT_MIN_SLACK,
    max_file_size: Optional[int] = None,
    regions: Optional[list[SlackRegion]] = None,
) -> list[SlackRegion]:
    """Write every slack region to ``out_dir``; returns the regions written."""

    path = Path(image_path)
    if not path.exists():
        raise SlackError("image not found: %s" % path)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    located = (
        regions
        if regions is not None
        else find_slack_regions(path, min_slack=min_slack, max_file_size=max_file_size)
    )
    written: list[SlackRegion] = []
    for index, region in enumerate(located):
        data = read_region(path, region.offset, region.length)
        if not data:
            continue
        name = "slack_%04d_%s.bin" % (index, _safe_name(region.path))
        (target / name).write_bytes(data)
        written.append(region)
    return written


def iter_slack_bytes(
    image_path: os.PathLike | str,
    regions: list[SlackRegion],
    *,
    chunk: int = DEFAULT_CHUNK,
) -> Iterator[tuple[SlackRegion, bytes]]:
    """Stream slack bytes without materializing whole regions in memory."""

    with open(image_path, "rb") as stream:
        for region in regions:
            remaining = region.length
            cursor = region.offset
            while remaining > 0:
                stream.seek(cursor)
                data = stream.read(min(chunk, remaining))
                if not data:
                    break
                yield region, data
                cursor += len(data)
                remaining -= len(data)
