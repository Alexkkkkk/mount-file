"""Partition table parsing (MBR, EBR chain, GPT) and filesystem sniffing.

Pure Python, no third-party dependencies. Written for Mount File — nothing is
shelled out to ``fdisk``/``parted``, so the same code runs on Windows where
those tools do not exist.

Implemented layouts
-------------------
MBR     4 primary entries at byte offset 446, boot signature 0x55AA at 510.
EBR     linked list for extended partitions (types 0x05 / 0x0F / 0x85); each
        record holds one logical partition (LBA relative to *its own* EBR) and
        a pointer to the next EBR (LBA relative to the *extended base*).
GPT     header at LBA 1 with signature ``EFI PART``; entry array located by
        ``partition_entry_lba``. Both CRCs are recomputed with zlib.crc32 —
        a mismatch is reported as a warning instead of aborting the parse,
        because real-world dumps from programmers very often have a stale
        backup header or a truncated tail.
"""

from __future__ import annotations

import dataclasses
import os
import struct
import zlib
from typing import BinaryIO, List, Optional

MBR_SIGNATURE = b"\x55\xaa"
PROTECTIVE_TYPE = 0xEE
EBR_TYPES = {0x05, 0x0F, 0x85}
GPT_SIGNATURE = b"EFI PART"
GPT_ENTRY_SIZE = 128

SECTOR_SIZE = 512
EXT_SB = 0x400  # ext2/3/4 superblock offset from the start of the filesystem


class PartitionError(Exception):
    """Raised when the image cannot be parsed at all (too small, unreadable)."""


# --------------------------------------------------------------------------- #
# type name tables
# --------------------------------------------------------------------------- #
MBR_TYPE_NAMES = {
    0x00: "empty",
    0x01: "FAT12",
    0x04: "FAT16 <32M",
    0x05: "Extended (CHS)",
    0x06: "FAT16",
    0x07: "NTFS / exFAT",
    0x0B: "FAT32 (CHS)",
    0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)",
    0x0F: "Extended (LBA)",
    0x11: "hidden FAT12",
    0x12: "Compaq diagnostics",
    0x14: "hidden FAT16",
    0x16: "hidden FAT16 (LBA)",
    0x17: "hidden NTFS",
    0x1B: "hidden FAT32 (CHS)",
    0x1C: "hidden FAT32 (LBA)",
    0x27: "Windows recovery",
    0x42: "Windows LDM",
    0x82: "Linux swap",
    0x83: "Linux",
    0x85: "Linux extended",
    0x8E: "Linux LVM",
    0xA5: "FreeBSD",
    0xA8: "macOS UFS",
    0xAB: "macOS boot",
    0xAF: "macOS HFS/HFS+",
    0xEF: "EFI System",
    0xFD: "Linux RAID",
    0xEE: "GPT protective",
}

GPT_TYPE_NAMES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Microsoft basic data",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
}


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Partition:
    index: int                 # 1-based number as seen by the user
    scheme: str                # 'mbr' | 'logical' | 'gpt'
    offset: int                # byte offset from the start of the image
    size: int                  # bytes
    type_id: str               # '0x0b' for MBR, GUID for GPT
    type_name: str = ""
    bootable: bool = False
    name: str = ""
    guid: str = ""
    fs_name: str = ""          # filled in by scan_filesystems()
    fs_detail: str = ""

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def start_sector(self) -> int:
        return self.offset // SECTOR_SIZE

    @property
    def size_human(self) -> str:
        from .util import human_size

        return human_size(self.size)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["size_human"] = self.size_human
        d["end"] = self.end
        return d


@dataclasses.dataclass
class PartitionTable:
    scheme: str
    sector_size: int
    partitions: List[Partition] = dataclasses.field(default_factory=list)
    warnings: List[str] = dataclasses.field(default_factory=list)
    image_size: int = 0

    def by_index(self, index: int) -> Optional[Partition]:
        for p in self.partitions:
            if p.index == index:
                return p
        return None

    def containing(self, offset: int) -> Optional[Partition]:
        for p in self.partitions:
            if p.offset <= offset < p.end:
                return p
        return None

    @property
    def has_extended(self) -> bool:
        return any(p.scheme == "logical" for p in self.partitions)

    def describe(self) -> str:
        lines = [
            f"scheme={self.scheme} sector_size={self.sector_size} "
            f"image_size={self.image_size} bytes, partitions={len(self.partitions)}"
        ]
        from .util import human_size

        for p in self.partitions:
            lines.append(
                f"  #{p.index:<2} {p.scheme:<7} start={p.offset:<12} "
                f"size={p.size:<12} ({human_size(p.size):>9}) "
                f"type={p.type_id} {p.type_name or p.name}"
                + (" [boot]" if p.bootable else "")
                + (f" fs={p.fs_name}" if p.fs_name else "")
            )
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


@dataclasses.dataclass
class FsInfo:
    """Result of magic-based filesystem detection."""

    name: str
    detail: str = ""
    total_bytes: Optional[int] = None

    def __str__(self) -> str:
        return f"{self.name}" + (f" ({self.detail})" if self.detail else "")


# --------------------------------------------------------------------------- #
# little helpers
# --------------------------------------------------------------------------- #
def _u32(buf: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf: bytes, off: int = 0) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def guid_str(raw: bytes) -> str:
    """Format a 16-byte EFI GUID (mixed endianness: first 3 fields LE)."""
    if len(raw) < 16:
        return ""
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{raw[8:10].hex()}-{raw[10:16].hex()}"


def guid_bytes(text: str) -> bytes:
    """Inverse of :func:`guid_str` — textual GUID to 16 on-disk bytes.

    Needed for the planned partition editor (writing a GPT entry back), and it
    lets the test-suite verify the mixed-endian conversion in both directions.
    """
    parts = text.strip().strip("{}").split("-")
    if len(parts) != 5:
        raise ValueError(f"not a GUID: {text!r}")
    d1, d2, d3 = (int(parts[i], 16) for i in range(3))
    tail = bytes.fromhex("".join(parts[3:]))
    if len(tail) != 8:
        raise ValueError(f"not a GUID: {text!r}")
    return struct.pack("<IHH", d1, d2, d3) + tail


def _read_at(fp: BinaryIO, offset: int, length: int) -> bytes:
    fp.seek(offset)
    return fp.read(length)


# --------------------------------------------------------------------------- #
# MBR
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _MbrEntry:
    slot: int
    status: int
    ptype: int
    lba: int
    sectors: int

    @property
    def bootable(self) -> bool:
        return self.status == 0x80


def _parse_mbr_entries(sector: bytes) -> List[_MbrEntry]:
    out: List[_MbrEntry] = []
    for slot in range(4):
        base = 446 + slot * 16
        rec = sector[base:base + 16]
        if len(rec) < 16:
            break
        out.append(
            _MbrEntry(
                slot=slot,
                status=rec[0],
                ptype=rec[4],
                lba=_u32(rec, 8),
                sectors=_u32(rec, 12),
            )
        )
    return out


def _walk_ebr(fp: BinaryIO, base_lba: int, sector_size: int,
              start_index: int = 5, max_chain: int = 32) -> List[Partition]:
    """Follow the extended-partition chain. Guarded against cycles/lies."""
    found: List[Partition] = []
    next_lba = base_lba
    seen = set()
    index = start_index
    for _ in range(max_chain):
        if next_lba in seen:
            break
        seen.add(next_lba)
        sector = _read_at(fp, next_lba * sector_size, sector_size)
        if len(sector) < 512 or sector[510:512] != MBR_SIGNATURE:
            break
        entries = _parse_mbr_entries(sector)
        logical = entries[0]
        link = entries[1]
        if logical.ptype != 0 and logical.sectors:
            found.append(
                Partition(
                    index=index,
                    scheme="logical",
                    offset=(next_lba + logical.lba) * sector_size,
                    size=logical.sectors * sector_size,
                    type_id=f"0x{logical.ptype:02x}",
                    type_name=MBR_TYPE_NAMES.get(logical.ptype, "unknown"),
                    bootable=logical.bootable,
                    name=f"logical-{index}",
                )
            )
            index += 1
        if link.ptype == 0 or link.sectors == 0:
            break
        next_lba = base_lba + link.lba
    return found


# --------------------------------------------------------------------------- #
# GPT
# --------------------------------------------------------------------------- #
def _parse_gpt(fp: BinaryIO, sector_size: int, warnings: List[str]) -> PartitionTable:
    header = _read_at(fp, sector_size, sector_size)
    if header[:8] != GPT_SIGNATURE:
        raise PartitionError("protective MBR present but GPT header signature is missing")

    header_size = _u32(header, 12)
    header_crc = _u32(header, 16)
    current_lba = _u64(header, 24)
    backup_lba = _u64(header, 32)
    first_usable = _u64(header, 40)
    last_usable = _u64(header, 48)
    entries_lba = _u64(header, 72)
    num_entries = _u32(header, 80)
    entry_size = _u32(header, 84)
    array_crc = _u32(header, 88)

    # CRC of the header is computed with the CRC field itself zeroed out.
    check = bytearray(header[:header_size])
    if header_size >= 20:
        check[16:20] = b"\x00\x00\x00\x00"
    if zlib.crc32(bytes(check)) & 0xFFFFFFFF != header_crc:
        warnings.append("GPT header CRC32 mismatch (header may be damaged)")

    if not 128 <= entry_size <= 4096:
        warnings.append(f"implausible GPT entry size {entry_size}, using 128")
        entry_size = GPT_ENTRY_SIZE
    if num_entries == 0:
        warnings.append("GPT header declares 0 entries, assuming 128")
        num_entries = 128
    elif num_entries > 512:
        warnings.append(f"implausible GPT entry count {num_entries}, clamped to 512")
        num_entries = 512

    array_bytes = _read_at(fp, entries_lba * sector_size, num_entries * entry_size)
    if len(array_bytes) < num_entries * entry_size:
        warnings.append("GPT entry array is truncated")
    if zlib.crc32(array_bytes) & 0xFFFFFFFF != array_crc:
        warnings.append("GPT entry-array CRC32 mismatch (entry array may be damaged)")

    partitions: List[Partition] = []
    for i in range(num_entries):
        rec = array_bytes[i * entry_size:(i + 1) * entry_size]
        if len(rec) < 56:
            break
        if rec[:16] == b"\x00" * 16:
            continue  # unused slot
        first = _u64(rec, 32)
        last = _u64(rec, 40)
        if last < first:
            warnings.append(f"GPT entry {i + 1} has last_lba < first_lba, skipped")
            continue
        type_guid = guid_str(rec[:16])
        raw_name = rec[56:128]
        try:
            name = raw_name.decode("utf-16-le", "replace").split("\x00")[0]
        except Exception:  # pragma: no cover - decode is already forgiving
            name = ""
        partitions.append(
            Partition(
                index=len(partitions) + 1,
                scheme="gpt",
                offset=first * sector_size,
                size=(last - first + 1) * sector_size,
                type_id=type_guid,
                type_name=GPT_TYPE_NAMES.get(type_guid, "unknown"),
                name=name or f"partition-{len(partitions) + 1}",
                guid=guid_str(rec[16:32]),
            )
        )

    table = PartitionTable("gpt", sector_size, partitions, warnings)
    table.gpt = {  # type: ignore[attr-defined]
        "header_lba": current_lba,
        "backup_lba": backup_lba,
        "first_usable_lba": first_usable,
        "last_usable_lba": last_usable,
        "entry_count": num_entries,
        "entry_size": entry_size,
    }
    return table


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def parse_partition_table(path: str, sector_size: int = SECTOR_SIZE) -> PartitionTable:
    """Detect and parse the partition table of ``path``."""
    image_size = os.path.getsize(path)
    with open(path, "rb") as fp:
        return parse_partition_table_fp(fp, sector_size, image_size)


def parse_partition_table_fp(fp: BinaryIO, sector_size: int = SECTOR_SIZE,
                             image_size: Optional[int] = None) -> PartitionTable:
    warnings: List[str] = []
    if image_size is None:
        fp.seek(0, os.SEEK_END)
        image_size = fp.tell()

    sector0 = _read_at(fp, 0, sector_size)
    if len(sector0) < 512:
        raise PartitionError("image is smaller than one 512-byte sector")

    if sector0[510:512] != MBR_SIGNATURE:
        warnings.append("no 0x55AA boot signature in LBA0 (wiped or raw filesystem image?)")

    entries = [e for e in _parse_mbr_entries(sector0) if e.ptype != 0 and e.sectors != 0]

    if any(e.ptype == PROTECTIVE_TYPE for e in entries):
        table = _parse_gpt(fp, sector_size, warnings)
        table.image_size = image_size
        # keep the protective MBR note last
        table.warnings = warnings + table.warnings
        return table

    partitions: List[Partition] = []
    extended_base: Optional[int] = None
    for e in entries:
        if e.ptype in EBR_TYPES:
            extended_base = e.lba
            continue
        partitions.append(
            Partition(
                index=len(partitions) + 1,
                scheme="mbr",
                offset=e.lba * sector_size,
                size=e.sectors * sector_size,
                type_id=f"0x{e.ptype:02x}",
                type_name=MBR_TYPE_NAMES.get(e.ptype, "unknown"),
                bootable=e.bootable,
                name=f"primary-{e.slot + 1}",
            )
        )
        if e.bootable:
            partitions[-1].name += " (active)"

    if extended_base is not None:
        logical = _walk_ebr(fp, extended_base, sector_size,
                            start_index=len(partitions) + 1)
        if not logical:
            warnings.append("extended partition header found but no logical volumes parsed")
        partitions.extend(logical)

    table = PartitionTable("mbr", sector_size, partitions, warnings, image_size)
    return table


# --------------------------------------------------------------------------- #
# filesystem sniffing (independent of pytsk3, so it can describe a partition
# even when TSK cannot open it)
# --------------------------------------------------------------------------- #
def sniff_filesystem(path: str, offset: int = 0, probe: int = 8192) -> FsInfo:
    with open(path, "rb") as fp:
        fp.seek(offset)
        head = fp.read(probe)
    return sniff_bytes(head)


def sniff_bytes(head: bytes) -> FsInfo:
    if len(head) < 8:
        return FsInfo("unknown", "short read")

    # --- NTFS / exFAT (OEM id at offset 3) ---
    oem = head[3:11] if len(head) >= 11 else b""
    if len(head) >= 512 and oem == b"NTFS    ":
        bps = struct.unpack_from("<H", head, 11)[0]
        total = struct.unpack_from("<Q", head, 40)[0]
        label = head[0x30:0x48].decode("utf-16-le", "replace").split("\x00")[0]
        size = bps * total if bps and total else None
        return FsInfo("ntfs", label, size)
    if len(head) >= 512 and oem == b"EXFAT   ":
        bps = 1 << head[108] if head[108] < 16 else 512
        vol_len = struct.unpack_from("<Q", head, 72)[0]
        return FsInfo("exfat", "", bps * vol_len if vol_len else None)

    # --- EXT2/3/4 ---
    # The superblock is 1024 bytes into the filesystem (boot block is skipped),
    # so the magic sits at 0x400 + 0x38, NOT at 0x438. Reading the wrong base
    # silently shifts every field by 1 KiB (a real bug this suite caught).
    if len(head) >= 0x600 and struct.unpack_from("<H", head, EXT_SB + 0x38)[0] == 0xEF53:
        return _sniff_ext(head)

    # --- FAT12/16 (label at 0x36) and FAT32 (label at 0x52) ---
    if head[510:512] == MBR_SIGNATURE and head[0] in (0xE9, 0xEB):
        bps = struct.unpack_from("<H", head, 11)[0] or 512
        spc = head[13] or 1
        if head[0x52:0x5A].startswith(b"FAT32"):
            total = struct.unpack_from("<I", head, 32)[0] or struct.unpack_from("<H", head, 19)[0]
            label = head[0x47:0x52].decode("ascii", "replace").strip()
            return FsInfo("fat32", label, total * bps)
        if head[0x36:0x3E].startswith(b"FAT1"):
            kind = "fat16" if head[0x36:0x3E] == b"FAT16   " else "fat12"
            total = struct.unpack_from("<H", head, 19)[0] or struct.unpack_from("<I", head, 32)[0]
            label = head[0x2B:0x36].decode("ascii", "replace").strip()
            return FsInfo(kind, label, total * bps)
        return FsInfo("fat", "boot sector, flavour not stated")

    # --- others ---
    if head[:4] in (b"hsqs", b"sqsh"):
        s_major = struct.unpack_from("<H", head, 28)[0]
        s_minor = struct.unpack_from("<H", head, 30)[0]
        bs = struct.unpack_from("<I", head, 12)[0]
        return FsInfo("squashfs", f"v{s_major}.{s_minor}, block {bs}")
    if head[0x8001:0x8006] == b"CD001":
        return FsInfo("iso9660", head[0x8028:0x8033].decode("ascii", "replace").strip())
    if head[:4] == b"UBI#":
        return FsInfo("ubifs", f"version {head[4]}")
    if head[:2] == b"\x19\x85":
        return FsInfo("jffs2", "big-endian magic")
    if head[:2] == b"\x85\x19":
        return FsInfo("jffs2", "little-endian magic")
    if head[512:516] == b"UBI#":
        return FsInfo("ubi", "EC header at LBA1 (UBI volume)")
    if head[:4] == b"LUKS":
        version = struct.unpack_from(">H", head, 6)[0] if len(head) > 8 else 0
        return FsInfo("luks", f"version {version}")
    if head[:4] == b"\x00\x00\x00\x00" and head[4:8] == b"\x00\x00\x00\x00":
        return FsInfo("unknown", "first sectors are zero-filled")
    return FsInfo("unknown", head[:8].hex())


def _sniff_ext(head: bytes) -> FsInfo:
    b = EXT_SB
    s_inodes = struct.unpack_from("<I", head, b + 0x00)[0]
    s_blocks = struct.unpack_from("<I", head, b + 0x04)[0]
    log_bs = struct.unpack_from("<I", head, b + 0x18)[0]
    feature_compat = struct.unpack_from("<I", head, b + 0x5C)[0]
    feature_incompat = struct.unpack_from("<I", head, b + 0x60)[0]
    label = head[b + 0x78:b + 0x88].split(b"\x00")[0].decode("utf-8", "replace")
    block_size = 1024 << log_bs if log_bs < 8 else 4096
    blocks = s_blocks
    if feature_incompat & 0x80 and len(head) >= b + 0x154:  # 64BIT -> hi word
        blocks |= struct.unpack_from("<I", head, b + 0x150)[0] << 32
    total = blocks * block_size
    # journal present + extents/flex_bg used -> ext4, journal only -> ext3
    if feature_incompat & 0x40 or feature_incompat & 0x200:
        name = "ext4"
    elif feature_compat & 0x04:
        name = "ext3"
    else:
        name = "ext2"
    detail = f"label={label or '-'} inodes={s_inodes} block={block_size}"
    return FsInfo(name, detail, total)


def scan_filesystems(path: str, table: Optional[PartitionTable] = None) -> PartitionTable:
    """Attach magic-based filesystem names to every partition of ``table``.

    Also tries the whole image at offset 0 — a raw dump of a single filesystem
    (typical for SPI flash dumps) has no partition table at all.
    """
    if table is None:
        table = parse_partition_table(path)
    for p in table.partitions:
        try:
            info = sniff_filesystem(path, p.offset)
        except OSError:
            continue
        p.fs_name = info.name
        p.fs_detail = info.detail
    if not table.partitions:
        info = sniff_filesystem(path, 0)
        if info.name != "unknown":
            table.warnings.append(
                f"no partition table; whole image looks like {info.name} ({info.detail})"
            )
    return table
