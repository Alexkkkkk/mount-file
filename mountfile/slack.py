# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Slack-space extraction (issue #87, Task 1.6).

Slack is the space between the end of a file and the end of the last block it
occupies.  It is located from the file's own data runs, which covers the three
layouts named in the issue:

* **NTFS non-resident** — the tail of the last run;
* **NTFS resident** — nothing, because the data lives inside the MFT record and
  occupies no cluster at all (so no false positive either);
* **FAT cluster slack** and **ext block slack** — the remainder of the final
  allocated cluster/block, which is exactly what the run arithmetic gives.

:func:`slack_region_for` is pure arithmetic on runs and is unit-tested directly;
:func:`find_slack_regions_from_fs` and :func:`extract_slack` do the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .fs_walk import block_size, entry_runs, iter_files

__all__ = [
    "SlackError",
    "SlackRegion",
    "extract_slack",
    "find_slack_regions_from_fs",
    "iter_slack_bytes",
    "slack_region_for",
]

DEFAULT_CHUNK = 1024 * 1024


class SlackError(RuntimeError):
    """Raised when slack cannot be located or read."""


@dataclass(frozen=True)
class SlackRegion:
    """One slack extent: ``length`` bytes at absolute ``offset``."""

    path: str
    offset: int
    length: int
    allocated_size: int = 0
    real_size: int = 0
    data: Optional[bytes] = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def end(self) -> int:
        return self.offset + self.length

    def as_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        payload = {
            "path": self.path,
            "offset": self.offset,
            "length": self.length,
            "allocated_size": self.allocated_size,
            "real_size": self.real_size,
        }
        if include_data:
            payload["data"] = self.data
        return payload


def slack_region_for(
    path: str,
    real_size: Any,
    runs: Iterable[Any],
    block_size_value: int,
) -> Optional[SlackRegion]:
    """Slack of a file described by ``(block_address, block_length)`` runs."""

    try:
        size = int(real_size)
    except (TypeError, ValueError):
        return None
    block = int(block_size_value)
    if size <= 0 or block <= 0:
        return None
    pairs: list[tuple[int, int]] = []
    for run in runs:
        if isinstance(run, (tuple, list)) and len(run) >= 2:
            address, length = int(run[0]), int(run[1])
        else:
            address = int(getattr(run, "addr", 0) or 0)
            length = int(getattr(run, "len", 0) or 0)
        if length > 0:
            pairs.append((address, length))
    if not pairs:
        return None
    allocated = sum(length for _address, length in pairs) * block
    slack = allocated - size
    if slack <= 0:
        return None
    last_address, last_length = pairs[-1]
    end_of_last_run = (last_address + last_length) * block
    return SlackRegion(
        path=str(path),
        offset=end_of_last_run - slack,
        length=slack,
        allocated_size=allocated,
        real_size=size,
    )


def find_slack_regions_from_fs(fs: Any, *, min_slack: int = 1) -> list[SlackRegion]:
    """Slack extents of every file in a filesystem-like object."""

    block = block_size(fs)
    regions: list[SlackRegion] = []
    for path, _entry, meta in iter_files(fs):
        runs = entry_runs(fs, meta)
        region = slack_region_for(path, getattr(meta, "size", 0), runs, block)
        if region is not None and region.length >= int(min_slack):
            regions.append(region)
    regions.sort(key=lambda region: (region.offset, region.path))
    return regions


def iter_slack_bytes(
    image: Any,
    regions: Iterable[SlackRegion],
    *,
    chunk: int = DEFAULT_CHUNK,
) -> Iterator[tuple[SlackRegion, bytes]]:
    """Yield ``(region, data)`` for each region, reading in ``chunk`` pieces."""

    path = Path(image)
    if not path.is_file():
        raise SlackError("image not found: %s" % image)
    step = max(1, int(chunk))
    with open(path, "rb") as stream:
        for region in regions:
            cursor = region.offset
            remaining = region.length
            while remaining > 0:
                stream.seek(cursor)
                data = stream.read(min(step, remaining))
                if not data:
                    break
                yield region, data
                cursor += len(data)
                remaining -= len(data)


def extract_slack(
    image: Any,
    out_dir: Any,
    *,
    regions: Optional[Iterable[SlackRegion]] = None,
    min_slack: int = 1,
    offset: int = 0,
    read_data: bool = True,
    chunk: int = DEFAULT_CHUNK,
) -> list[SlackRegion]:
    """Find slack in an image and dump every extent into ``out_dir``.

    When ``regions`` is omitted they are discovered from the image with
    ``pytsk3``; supplying them keeps the call usable without it.
    """

    path = Path(image)
    if not path.is_file():
        raise SlackError("image not found: %s" % image)
    if regions is None:
        try:
            import pytsk3  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on install
            raise SlackError(
                "discovering slack needs the optional 'pytsk3' package; install "
                "it with 'python -m pip install pytsk3' or pass regions="
            ) from exc
        try:
            img = pytsk3.Img_Info(str(path))
            fs = pytsk3.FS_Info(img, int(offset))
        except Exception as exc:
            raise SlackError("cannot open %s as a filesystem: %s" % (image, exc)) from exc
        discovered = find_slack_regions_from_fs(fs, min_slack=min_slack)
    else:
        discovered = [
            region for region in regions if region.length >= int(min_slack)
        ]

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[SlackRegion] = []
    for index, region in enumerate(discovered):
        payload = b""
        if read_data:
            payload = b"".join(data for _r, data in iter_slack_bytes(path, [region], chunk=chunk))
        safe = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in region.name
        ) or "unnamed"
        destination = directory / (
            "slack_%04d_off%08x_%s.bin" % (index, region.offset, safe)
        )
        destination.write_bytes(payload)
        written.append(
            SlackRegion(
                path=region.path,
                offset=region.offset,
                length=region.length,
                allocated_size=region.allocated_size,
                real_size=region.real_size,
                data=payload if read_data else None,
            )
        )
    return written
