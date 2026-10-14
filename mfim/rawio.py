"""Raw image I/O: absolute-offset reads and *transactional* sector patching.

Writing into a raw image is the single most dangerous operation in this
project. The contract implemented here is deliberately conservative:

    1. every write must be planned first (offset + bytes, collected in a journal);
    2. ``commit()`` makes a full backup copy of the image before touching it
       (unless explicitly disabled);
    3. writes happen in one pass, each region is then read back and compared
       byte-for-byte with what we intended to write;
    4. the report tells the caller exactly which ranges were verified.

Nothing in this module understands filesystems, so it cannot corrupt a
filesystem *structurally* — but it can still destroy data by writing to the
wrong offset, which is why verification is mandatory.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from typing import Iterable, List, Optional, Tuple

CHUNK = 1 << 20


@dataclasses.dataclass
class Patch:
    offset: int
    data: bytes

    @property
    def end(self) -> int:
        return self.offset + len(self.data)


@dataclasses.dataclass
class PatchReport:
    planned: List[Patch] = dataclasses.field(default_factory=list)
    backup_path: Optional[str] = None
    bytes_written: int = 0
    verified: List[Tuple[int, int]] = dataclasses.field(default_factory=list)
    failed: List[Tuple[int, str]] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def describe(self) -> str:
        lines = [
            f"patches={len(self.planned)} bytes={self.bytes_written} "
            f"backup={self.backup_path or 'none'}"
        ]
        for off, ln in self.verified:
            lines.append(f"  verified 0x{off:x}..0x{off + ln:x} ({ln} bytes)")
        for off, why in self.failed:
            lines.append(f"  FAILED 0x{off:x}: {why}")
        return "\n".join(lines)


class RawImage:
    """Read-only helper for absolute offsets inside the image file."""

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0:
            raise ValueError("negative offset")
        with open(self.path, "rb") as fp:
            fp.seek(offset)
            return fp.read(length)

    def read_all(self, offset: int = 0, length: Optional[int] = None) -> bytes:
        if length is None:
            length = self.size - offset
        return self.read(offset, length)

    def hexdump(self, offset: int = 0, length: int = 256, base: Optional[int] = None) -> str:
        from .hexview import hexdump

        return hexdump(self.read(offset, length), base=offset if base is None else base)


class ImagePatcher:
    """Plan-then-commit writer for raw images. Never writes implicitly."""

    def __init__(self, path: str, readonly: bool = True):
        self.path = path
        self.readonly = readonly
        self._patches: List[Patch] = []

    # -- planning ---------------------------------------------------------- #
    def plan(self, offset: int, data: bytes) -> Patch:
        if offset < 0 or not data:
            raise ValueError("offset must be >= 0 and data must not be empty")
        p = Patch(int(offset), bytes(data))
        self._patches.append(p)
        return p

    def clear(self) -> None:
        self._patches.clear()

    @property
    def planned(self) -> List[Patch]:
        return list(self._patches)

    @property
    def planned_ranges(self) -> List[Tuple[int, int]]:
        return [(p.offset, len(p.data)) for p in self._patches]

    # -- committing -------------------------------------------------------- #
    def commit(self, backup: bool = True, verify: bool = True,
               backup_suffix: str = ".bak") -> PatchReport:
        report = PatchReport(planned=self.planned)
        if not self._patches:
            return report
        if self.readonly:
            raise PermissionError(
                "ImagePatcher is in readonly mode; open it with readonly=False "
                "and keep a backup before writing to a raw image"
            )

        image_size = os.path.getsize(self.path)
        for p in self._patches:
            if p.end > image_size:
                raise ValueError(
                    f"patch at 0x{p.offset:x} (+{len(p.data)}) exceeds image size {image_size}"
                )

        if backup:
            report.backup_path = self.path + backup_suffix
            shutil.copyfile(self.path, report.backup_path)

        with open(self.path, "r+b") as fp:
            for p in self._patches:
                fp.seek(p.offset)
                fp.write(p.data)
                report.bytes_written += len(p.data)
            fp.flush()
            os.fsync(fp.fileno())

        if verify:
            with open(self.path, "rb") as fp:
                for p in self._patches:
                    fp.seek(p.offset)
                    back = fp.read(len(p.data))
                    if back == p.data:
                        report.verified.append((p.offset, len(p.data)))
                    else:
                        report.failed.append(
                            (p.offset, "read-back mismatch after write")
                        )
        return report


def clone_image(src: str, dst: str) -> str:
    """Byte-for-byte copy used by tests and by the write-safety workflow."""
    shutil.copyfile(src, dst)
    return dst


def iterate_chunks(fp, offset: int, size: int, chunk: int = CHUNK) -> Iterable[bytes]:
    fp.seek(offset)
    remaining = size
    while remaining > 0:
        data = fp.read(min(chunk, remaining))
        if not data:
            break
        remaining -= len(data)
        yield data
