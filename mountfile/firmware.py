# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Volume discovery for raw images: partition tables + filesystem signatures.

The GUI asks this module for the list of mountable volumes. Two strategies
are combined:

1. MBR/GPT parsing (:mod:`mountfile.partition`) - authoritative while the
   image still carries a partition table.
2. Signature scanning - dumps taken straight from NAND/eMMC flash of embedded
   devices often have no partition table at all, so the filesystem headers are
   searched directly in the raw stream (FAT, NTFS, exFAT, ext2/3/4, ISO9660,
   HFS+, squashfs, JFFS2, UBIFS).

Signature hits are candidates only: every one of them is re-read and run
through :func:`_classify` before it is reported, so the volume list does not
fill up with false positives coming from file contents.
"""

from __future__ import annotations

import struct

from .partition import list_partitions

__all__ = ["FoundVolume", "detect_volumes", "probe_filesystem"]

#: Bytes read for one probe: enough to reach the ISO9660 primary volume
#: descriptor (sector 16 = 0x8000) and its "CD001" identifier.
PROBE_SIZE = 0x8006

#: How far into a partitionless image the signature scan walks by default.
DEFAULT_SCAN_LIMIT = 0  # 0 = "до конца файла"

_CHUNK = 1 << 20

# (magic, bytes before the magic, required alignment, filesystem name)
_MAGIC = (
    # FAT32's extended BPB puts the filesystem label at 82, not the
    # FAT12/16 offset 54. Retain the legacy location for existing dumps.
    (b"FAT32   ", 82, 512, "FAT32"),
    (b"FAT32   ", 54, 512, "FAT32"),
    (b"FAT16   ", 54, 512, "FAT16"),
    (b"FAT12   ", 54, 512, "FAT12"),
    (b"NTFS    ", 3, 512, "NTFS"),
    (b"EXFAT   ", 3, 512, "exFAT"),
    (b"\x53\xef", 1080, 1024, "ext2/3/4"),
    (b"CD001", 0x8001, 2048, "ISO9660"),
    (b"H+", 1024, 512, "HFS+"),
    (b"hsqs", 0, 512, "squashfs"),
    (b"\x31\x18\x10\x06", 0, 512, "UBIFS"),
    (b"\x19\x85", 0, 512, "JFFS2"),
    (b"\x85\x19", 0, 512, "JFFS2"),
)


class FoundVolume:
    """One mountable volume: `offset` is where the filesystem starts."""

    __slots__ = ("desc", "fstype", "offset", "size", "source")

    def __init__(self, offset, fstype, desc=None, size=None, source="signature"):
        self.offset = int(offset)
        self.fstype = fstype
        self.size = size
        self.source = source
        self.desc = desc or self._default_desc()

    def _default_desc(self):
        text = "%s @ 0x%X" % (self.fstype, self.offset)
        if self.size:
            text += "  %.1f MB" % (self.size / 1048576.0)
        return text

    def __repr__(self):
        return "<FoundVolume 0x%X %s (%s)>" % (self.offset, self.fstype, self.source)


def _classify(buf):
    """Name of the filesystem whose header starts at ``buf[0]``, or None.

    ``buf`` should hold up to :data:`PROBE_SIZE` bytes; checks that need more
    data than the buffer provides are simply skipped.
    """
    n = len(buf)

    # --- boot-sector based filesystems (FAT / NTFS / exFAT / HFS+) --------
    # A boot sector always starts with a jump instruction and ends with the
    # 0x55AA signature; both are required, otherwise random file content that
    # happens to contain a plausible BPB would be reported as a volume.
    if n >= 512 and buf[0] in (0xE9, 0xEB) and buf[510:512] == b"\x55\xaa":
        oem = buf[3:11]
        if oem == b"NTFS    ":
            return "NTFS"
        if oem == b"EXFAT   ":
            return "exFAT"
        bps = struct.unpack_from("<H", buf, 11)[0]
        spc = buf[13]
        reserved = struct.unpack_from("<H", buf, 14)[0]
        nfats = buf[16]
        if (
            bps in (512, 1024, 2048, 4096)
            and spc
            and not (spc & (spc - 1))
            and 1 <= reserved <= 64
            and 1 <= nfats <= 2
        ):
            spf16 = struct.unpack_from("<H", buf, 22)[0]
            spf32 = struct.unpack_from("<I", buf, 36)[0]
            label = buf[54:62]
            # The 8-byte label is what a formatter writes, but plenty of
            # factory images leave it empty - fall back to the geometry.
            if (
                buf[82:90] == b"FAT32   "
                or label == b"FAT32   "
                or (spf32 and not spf16)
            ):
                return "FAT32"
            if label == b"FAT16   ":
                return "FAT16"
            if label == b"FAT12   ":
                return "FAT12"
            if spf16 and not spf32:
                root_entries = struct.unpack_from("<H", buf, 17)[0]
                return "FAT16" if root_entries > 512 else "FAT12"

    # --- ext2/3/4: superblock at 1024, magic 0xEF53 at +0x38 -------------
    if n >= 1024 + 0x3A and buf[0x438:0x43A] == b"\x53\xef":
        return "ext2/3/4"

    # --- HFS+ volume header at 1024 --------------------------------------
    if n >= 1026 and buf[1024:1026] == b"H+":
        return "HFS+"

    # --- ISO9660: "CD001" at 0x8001 (primary volume descriptor) ----------
    if n >= 0x8006 and buf[0x8001:0x8006] == b"CD001":
        return "ISO9660"

    # --- raw flash filesystems, header at the volume start ---------------
    if n >= 4 and buf[0:4] == b"hsqs":
        return "squashfs"
    if n >= 4 and buf[0:4] == b"\x31\x18\x10\x06":
        return "UBIFS"
    if n >= 2 and buf[0:2] in (b"\x19\x85", b"\x85\x19"):
        return "JFFS2"

    return None


def _probe(f, offset):
    """Read a probe window at `offset` (short read near EOF is fine)."""
    if offset < 0:
        return b""
    f.seek(offset)
    return f.read(PROBE_SIZE)


def probe_filesystem(path, offset=0):
    """Return the filesystem name at `offset`, or None."""
    with open(path, "rb") as f:
        return _classify(_probe(f, offset))


def _verify(f, offset, expected):
    """Confirm a signature candidate by re-classifying the whole header."""
    got = _classify(_probe(f, offset))
    return got == expected


def _scan_signatures(f, limit):
    """Yield `(offset, fstype)` for filesystem headers inside the image.

    ``limit`` of 0 (or None) means "scan the whole image": the signature
    walk must never stop at offset 0, otherwise every header deeper in the
    dump is silently lost.
    """
    f.seek(0, 2)
    total = f.tell()
    limit = int(limit or 0)
    end = min(total, limit) if limit > 0 else total
    if end <= 0:
        return []

    hits = []
    seen = set()
    # Keep the widest look-behind plus its magic in the carry-over buffer so a
    # header split across two chunks is still found.
    carry_len = 0x8001 + 5
    base = 0
    carry = b""
    while base < end:
        f.seek(base)
        chunk = f.read(min(_CHUNK, end - base))
        if not chunk:
            break
        buf = carry + chunk
        buf_base = base - len(carry)
        for magic, back, align, name in _MAGIC:
            pos = buf.find(magic)
            while pos >= 0:
                cand = buf_base + pos - back
                if (
                    cand >= 0
                    and cand < end
                    and cand % align == 0
                    and (cand, name) not in seen
                    and _verify(f, cand, name)
                ):
                    seen.add((cand, name))
                    hits.append((cand, name))
                pos = buf.find(magic, pos + 1)
        carry = buf[-carry_len:]
        base += len(chunk)
    return hits


def detect_volumes(path, scan_limit=DEFAULT_SCAN_LIMIT):
    """Return a list of :class:`FoundVolume` for a raw image.

    Volumes from the partition table come first (they carry the partition's
    name and size); when the table is missing or holds nothing recognisable,
    the image is scanned for filesystem signatures instead.
    """
    volumes = []
    with open(path, "rb") as f:
        try:
            parts = list_partitions(path)
        except (OSError, struct.error):
            parts = []

        for p in parts:
            fstype = _classify(_probe(f, p.offset))
            volumes.append(
                FoundVolume(
                    p.offset,
                    fstype or "raw",
                    desc="%d  %s  %s  %.1f MB @ LBA %d"
                    % (p.index, p.name, fstype or p.type_name, p.size_mb, p.start_lba),
                    size=p.size_bytes,
                    source="table",
                )
            )

        covered = {v.offset for v in volumes}
        found = any(v.source == "table" and v.fstype != "raw" for v in volumes)
        if not found:
            for offset, fstype in _scan_signatures(f, scan_limit):
                if offset in covered:
                    continue
                covered.add(offset)
                volumes.append(FoundVolume(offset, fstype, source="signature"))

    volumes.sort(key=lambda v: v.offset)
    return volumes
