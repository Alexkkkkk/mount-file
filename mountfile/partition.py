# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""MBR / GPT partition table parser for raw disk images."""

from __future__ import annotations

import struct
import uuid

SECTOR_SIZE = 512

_TYPE_NAMES = {
    0x01: "FAT12",
    0x04: "FAT16",
    0x06: "FAT16",
    0x07: "NTFS / exFAT",
    0x0B: "FAT32",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x83: "Linux",
    0x8E: "Linux LVM",
    0xEE: "GPT protective",
    0xEF: "EFI System",
    0xA5: "FreeBSD",
    0xA6: "OpenBSD",
    0xA8: "macOS",
    0xAF: "HFS+",
    0xBF: "Solaris",
    0xEB: "BeOS / Haiku",
    0xFD: "Linux RAID",
}

_GPT_GUID_NAMES = {
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "af3dc60f-8384-7247-8e79-3d69d8477de4": "Linux /usr",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "5808c8aa-7e8f-42e0-85d2-d1f0e4ad0f00": "Microsoft LDM",
    "75894c1e-3aeb-11d3-b7c1-7b03a0000000": "HP-UX data",
}


class Partition:
    """One partition entry (from MBR or GPT)."""

    def __init__(self, index, name, start_lba, sectors, ptype, bootable=False):
        self.index = index
        self.name = name
        self.start_lba = start_lba
        self.sectors = sectors
        self.type = ptype
        self.bootable = bootable

    @property
    def offset(self):
        return self.start_lba * SECTOR_SIZE

    @property
    def size_bytes(self):
        return self.sectors * SECTOR_SIZE

    @property
    def size_mb(self):
        return self.size_bytes / (1024 * 1024)

    @property
    def type_name(self):
        if isinstance(self.type, str):
            return _GPT_GUID_NAMES.get(self.type, "GPT type " + self.type)
        return _TYPE_NAMES.get(self.type, "0x%02X" % self.type)

    def __repr__(self):
        return "<Partition %s LBA=%d+%d %s>" % (
            self.name, self.start_lba, self.sectors, self.type_name)


def _parse_mbr(f):
    f.seek(0)
    mbr = f.read(SECTOR_SIZE)
    if len(mbr) < SECTOR_SIZE or mbr[510:512] != b"\x55\xaa":
        return []
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        ptype = e[4]
        start, size = struct.unpack_from("<II", e, 8)
        if ptype == 0 or size == 0:
            continue
        parts.append(Partition(i + 1, "Partition %d" % (i + 1),
                               start, size, ptype, bool(e[0] & 0x80)))
    return parts


def _parse_gpt(f):
    f.seek(SECTOR_SIZE)
    hdr = f.read(SECTOR_SIZE)
    if len(hdr) < SECTOR_SIZE or hdr[:8] != b"EFI PART":
        return []
    entry_lba = struct.unpack_from("<Q", hdr, 72)[0]
    num_entries = struct.unpack_from("<I", hdr, 80)[0]
    entry_size = struct.unpack_from("<I", hdr, 84)[0]
    parts = []
    f.seek(entry_lba * SECTOR_SIZE)
    for i in range(num_entries):
        e = f.read(entry_size)
        if len(e) < entry_size:
            break
        type_guid = uuid.UUID(bytes_le=e[0:16])
        if type_guid.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        if last < first:
            continue
        name = e[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        parts.append(Partition(i + 1, name or "Partition %d" % (i + 1),
                               first, last - first + 1,
                               str(type_guid)))
    return parts


def list_partitions(path):
    """Return a list of Partition for a raw image file."""
    with open(path, "rb") as f:
        mbr = _parse_mbr(f)
        if any(p.type == 0xEE for p in mbr):
            gpt = _parse_gpt(f)
            if gpt:
                return gpt
        return mbr
