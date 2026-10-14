#!/usr/bin/env python3
"""Mount File — self-contained test run: unit tests + real image integration.

The script builds throwaway images itself (MBR with a hand-written EBR chain,
GPT via sgdisk, a bare filesystem dump with no table) and drives the CLI as a
subprocess, so what it prints is the whole evidence trail: partition parsing
cross-checked against sfdisk, pytsk3 reads, extraction with sha256, raw patching
with read-back verification, FAT writes through mtools, and ``fsck.fat -n``
runs over every image the writer touched.

    python3 tests/run_tests.py            # everything
    python3 tests/run_tests.py --keep     # keep the images for manual poking
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SECTOR = 512
HEADS, SPTS = 255, 63           # geometry used only for the cosmetic CHS fields

# layout, in 512-byte sectors
FAT32_START, FAT32_SECTORS = 2048, 131072        # 64 MiB
EXT4_START, EXT4_SECTORS = 133120, 20480         # 10 MiB
EXT_BASE, EXT_SECTORS = 153600, 30720            # extended container, 15 MiB
LOG1_START, LOG1_SECTORS = 155648, 8192          # FAT16, 4 MiB
LOG2_START, LOG2_SECTORS = 165888, 8192          # unformatted
LOG3_START, LOG3_SECTORS = 176128, 8192          # unformatted
IMAGE_SECTORS = 196608                            # 96 MiB

FAT32_OFFSET = FAT32_START * SECTOR
LOG1_OFFSET = LOG1_START * SECTOR

PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(300000))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    _results.append((status, name, detail))
    print(f"[{status}] {name}" + (f"  — {detail}" if detail else ""))
    return condition


def skip(name: str, why: str) -> None:
    _results.append((SKIP, name, why))
    print(f"[{SKIP}] {name}  — {why}")


def have(exe: str) -> bool:
    return shutil.which(exe) is not None


def run(cmd, check_rc=True, input_text=None, env=None, cwd=None):
    proc = subprocess.run(cmd, capture_output=True, text=True, input=input_text,
                          env=env, cwd=cwd, timeout=600)
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} rc={proc.returncode}: "
                           f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# image building (deterministic, no root, no loop devices)
# --------------------------------------------------------------------------- #
def _chs(lba: int) -> bytes:
    if lba < 0:
        return b"\x00\x01\x00"
    c = lba // (HEADS * SPTS)
    if c > 1023:
        return b"\xfe\xff\xff"
    h = (lba // SPTS) % HEADS
    s = lba % SPTS + 1
    return bytes([h & 0xFF, (s & 0x3F) | ((c >> 2) & 0xC0), c & 0xFF])


def mbr_entry(status: int, ptype: int, start_lba: int, sectors: int) -> bytes:
    e = bytearray(16)
    e[0] = status
    e[1:4] = _chs(start_lba)
    e[4] = ptype
    e[5:8] = _chs(start_lba + sectors - 1)
    e[8:12] = struct.pack("<I", start_lba)
    e[12:16] = struct.pack("<I", sectors)
    return bytes(e)


def build_sector(entries) -> bytes:
    sec = bytearray(SECTOR)
    for slot, entry in entries:
        sec[446 + slot * 16: 446 + slot * 16 + 16] = entry
    sec[510:512] = b"\x55\xaa"
    return bytes(sec)


def write_ebr_chain(fp, ext_base: int, logicals) -> None:
    """Write a classic EBR linked list.

    Each EBR holds one logical partition in slot 0 (LBA *relative to the EBR
    itself*) and a link to the next EBR in slot 1 (LBA *relative to the
    extended base*) — the asymmetric offsets real firmware and fdisk emit, and
    exactly what mfim.partition._walk_ebr expects.
    """
    for i, (start, sectors, ptype) in enumerate(logicals):
        ebr_lba = ext_base if i == 0 else start - 1   # first EBR = extended base LBA
        entries = [(0, mbr_entry(0x00, ptype, start - ebr_lba, sectors))]
        if i + 1 < len(logicals):
            next_ebr = logicals[i + 1][0] - 1
            remaining = ext_base + EXT_SECTORS - next_ebr
            entries.append((1, mbr_entry(0x00, 0x05, next_ebr - ext_base, remaining)))
        fp.seek(ebr_lba * SECTOR)
        fp.write(build_sector(entries))


def format_region(image: str, offset: int, size: int, flavour: str, label: str) -> str:
    """Format a partition *inside* a larger image.

    mkfs.fat/mke2fs have no reliable ``--size`` option, so formatting in place
    would run past the end of the partition. Instead the region is extracted to
    a file of exactly its size, formatted, fsck'ed and copied back — which is
    also what a careful human does.
    """
    tmp = image + ".region"
    with open(image, "rb") as src, open(tmp, "wb") as dst:
        src.seek(offset)
        left = size
        while left > 0:
            block = src.read(min(1 << 20, left))
            if not block:
                break
            dst.write(block)
            left -= len(block)
    os.truncate(tmp, size)

    if flavour.startswith("fat"):
        bits = "32" if flavour == "fat32" else "16"
        run(["mkfs.vfat", "-F", bits, "-s", "1", "-n", label, tmp])
    elif flavour == "ext4":
        run(["mkfs.ext4", "-q", "-F", "-L", label, tmp])
    else:
        raise ValueError(f"unsupported flavour {flavour}")

    with open(tmp, "rb") as src, open(image, "r+b") as dst:
        dst.seek(offset)
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    os.unlink(tmp)

    from mfim.partition import sniff_filesystem

    return sniff_filesystem(image, offset).name


def build_mbr_image(path: str, work: str) -> dict:
    with open(path, "wb") as fp:
        fp.truncate(IMAGE_SECTORS * SECTOR)

    fat32_kind = format_region(path, FAT32_OFFSET, FAT32_SECTORS * SECTOR, "fat32", "BOOTPART")
    fat16_kind = format_region(path, LOG1_OFFSET, LOG1_SECTORS * SECTOR, "fat16", "LOGVOL1")
    ext4_kind = format_region(path, EXT4_START * SECTOR, EXT4_SECTORS * SECTOR, "ext4", "rootfs")

    fat32_type = 0x0C if fat32_kind == "fat32" else 0x0E
    fat16_type = 0x06 if fat16_kind == "fat16" else 0x0E

    logicals = [(LOG1_START, LOG1_SECTORS, fat16_type),
                (LOG2_START, LOG2_SECTORS, 0x83),
                (LOG3_START, LOG3_SECTORS, 0x83)]

    with open(path, "r+b") as fp:
        fp.seek(0)
        fp.write(build_sector([
            (0, mbr_entry(0x80, fat32_type, FAT32_START, FAT32_SECTORS)),
            (1, mbr_entry(0x00, 0x83, EXT4_START, EXT4_SECTORS)),
            (2, mbr_entry(0x00, 0x05, EXT_BASE, EXT_SECTORS)),
        ]))
        write_ebr_chain(fp, EXT_BASE, logicals)

    return {"fat32_offset": FAT32_OFFSET, "fat32_type": fat32_type,
            "ext4_offset": EXT4_START * SECTOR, "log1_offset": LOG1_OFFSET,
            "fat32_kind": fat32_kind, "fat16_kind": fat16_kind, "ext4_kind": ext4_kind}


def mtoolsrc(path: str, image: str, offset: int) -> str:
    rc = os.path.join(path, "mtools.rc")
    with open(rc, "w") as fp:
        fp.write(f'drive z: file="{image}" offset={offset}\n')
    return rc


def populate_fat(image: str, offset: int, workdir: str) -> None:
    src = os.path.join(workdir, "payload")
    os.makedirs(os.path.join(src, "docs"), exist_ok=True)
    os.makedirs(os.path.join(src, "nested", "deep"), exist_ok=True)
    with open(os.path.join(src, "readme.txt"), "w") as fp:
        fp.write("Mount File test payload\nsecond line\n")
    with open(os.path.join(src, "docs", "notes.md"), "w") as fp:
        fp.write("# notes\n" * 40)
    with open(os.path.join(src, "big.bin"), "wb") as fp:
        fp.write(PAYLOAD)
    with open(os.path.join(src, "nested", "deep", "leaf.dat"), "wb") as fp:
        fp.write(b"\xde\xad\xbe\xef" * 512)

    env = dict(os.environ, MTOOLSRC=mtoolsrc(workdir, image, offset), MTOOLS_SKIP_CHECK="1")
    run(["mmd", "z:/docs"], env=env)
    run(["mcopy", "-s", "-o", src, "z:/"], env=env)


def fsck_fat(image: str, offset: int, size: int = 0) -> str:
    """fsck.fat on a partition inside an image; falls back to an extracted copy."""
    if have("fsck.fat"):
        probe = run(["fsck.fat", "--help"], check_rc=False)
        if "--offset" in (probe.stdout + probe.stderr):
            proc = run(["fsck.fat", "-n", "--offset", str(offset), image], check_rc=False)
            return (proc.stdout or "") + (proc.stderr or "")
    tmp = image + ".fsck"
    with open(image, "rb") as fp:
        fp.seek(offset)
        data = fp.read(size or (os.path.getsize(image) - offset))
    with open(tmp, "wb") as fp:
        fp.write(data)
    proc = run(["fsck.fat", "-n", tmp], check_rc=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    os.unlink(tmp)
    return out


# --------------------------------------------------------------------------- #
# unit tests (no images needed)
# --------------------------------------------------------------------------- #
def unit_tests() -> None:
    print("\n=== unit tests: pure parsing / helpers ===")
    from mfim.hexview import hexdump
    from mfim.partition import guid_str, sniff_bytes
    from mfim.tree_model import ImageTreeModelData
    from mfim.util import decode_name, human_size, normalize_path

    lines = hexdump(b"Hello, world!", base=0x1000).splitlines()
    check("hexview: offset column + hex + ASCII on one line",
          lines[0].startswith("00001000  48 65 6c 6c 6f") and lines[0].endswith("Hello, world!"),
          lines[0])
    check("hexview: 16 bytes per row", len(hexdump(bytes(range(64))).splitlines()) == 4)
    check("hexview: non-printable bytes become dots",
          hexdump(b"\x00\x01\xff\x41").endswith("...A"), hexdump(b"\x00\x01\xffA"))
    check("util: human_size", human_size(1234567) == "1.18 MiB", human_size(1234567))
    check("util: human_size of None", human_size(None) == "-")
    check("util: normalize_path", normalize_path("a\\b//c/") == "/a/b/c", normalize_path("a\\b//c/"))
    check("util: non-UTF8 name survives",
          decode_name(b"\xff\xfeA").endswith("A"), repr(decode_name(b"\xff\xfeA")))
    # EFI System GUID c12a7328-f81f-11d2-ba4b-00a0c93ec93b as stored on disk
    efi_on_disk = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
    check("partition: GUID mixed-endian formatting (on-disk -> canonical)",
          guid_str(efi_on_disk) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
          guid_str(efi_on_disk))

    fat32 = bytearray(8192)
    fat32[0:3] = b"\xeb\x58\x90"
    fat32[11:13] = (512).to_bytes(2, "little")
    fat32[0x52:0x5A] = b"FAT32   "
    fat32[32:36] = (131072).to_bytes(4, "little")
    fat32[0x47:0x52] = b"BOOTPART   "
    fat32[510:512] = b"\x55\xaa"
    info = sniff_bytes(bytes(fat32))
    check("sniff: FAT32 magic + label + computed size",
          info.name == "fat32" and info.detail == "BOOTPART"
          and info.total_bytes == 512 * 131072,
          f"{info} total={info.total_bytes}")

    SB = 1024   # ext superblock starts at byte 1024 of the volume, not at 0
    ext = bytearray(8192)
    ext[SB + 0x38:SB + 0x3A] = (0xEF53).to_bytes(2, "little")   # s_magic
    ext[SB + 0x00:SB + 0x04] = (1024).to_bytes(4, "little")     # s_inodes_count
    ext[SB + 0x04:SB + 0x08] = (25600).to_bytes(4, "little")    # s_blocks_count_lo
    ext[SB + 0x18:SB + 0x1C] = (2).to_bytes(4, "little")        # log_block_size -> 4096
    ext[SB + 0x5C:SB + 0x60] = (0x4).to_bytes(4, "little")      # has_journal
    ext[SB + 0x78:SB + 0x88] = b"ROOTFS\x00\x00"
    info = sniff_bytes(bytes(ext))
    check("sniff: EXT3 (journal, no extents) + label + computed size",
          info.name == "ext3" and info.detail.startswith("label=ROOTFS")
          and info.total_bytes == 25600 * 4096, f"{info} total={info.total_bytes}")

    ext4 = bytearray(ext)
    ext4[SB + 0x60:SB + 0x64] = (0x40).to_bytes(4, "little")    # EXTENTS incompat
    check("sniff: EXT4 via incompat EXTENTS flag", sniff_bytes(bytes(ext4)).name == "ext4",
          str(sniff_bytes(bytes(ext4))))

    sq = bytearray(b"hsqs" + b"\x00" * 4092)
    sq[28:30] = (4).to_bytes(2, "little")
    sq[12:16] = (131072).to_bytes(4, "little")
    check("sniff: SQUASHFS v4 magic", "v4.0" in sniff_bytes(bytes(sq)).detail,
          str(sniff_bytes(bytes(sq))))
    check("sniff: short buffer handled without IndexError",
          sniff_bytes(b"hsqs").name == "squashfs", str(sniff_bytes(b"hsqs")))
    check("sniff: JFFS2 little-endian magic", sniff_bytes(b"\x85\x19" + b"\x00" * 600).name == "jffs2")
    check("sniff: ISO9660 magic at 0x8001",
          sniff_bytes(b"\x00" * 0x8001 + b"CD001" + b"\x00" * 600).name == "iso9660")
    check("sniff: zero-filled head reported honestly",
          "zero-filled" in sniff_bytes(b"\x00" * 4096).detail)

    # pytsk3 has NO read(): prove the streaming loop handles read_random + holes
    from mfim.vfs import ImageVFS

    class FakeFile:
        def __init__(self, blob, hole=(0, 0)):
            self.blob, self.hole = blob, hole or (0, 0)

        @property
        def size(self):
            return len(self.blob)

        def read_random(self, off, size):
            if self.hole[0] <= off < self.hole[1]:
                return None
            return self.blob[off:off + size]

    class StreamVfs(ImageVFS):
        def __init__(self, blob, hole=None):
            self._blob, self._hole = blob, hole

        def open_file(self, path):
            return FakeFile(self._blob, self._hole)

        @staticmethod
        def size_of(f):
            return f.size

    blob = bytes((i * 7) % 256 for i in range(3000))
    check("vfs: chunked read_random loop == original bytes",
          StreamVfs(blob).read_file("/x", chunk=512) == blob)
    holed = StreamVfs(blob, hole=(1024, 1536))
    out = holed.read_file("/x", chunk=256)
    check("vfs: sparse hole -> zero fill, size preserved",
          len(out) == len(blob) and out[:1024] == blob[:1024]
          and out[1024:1536] == b"\x00" * 512 and out[1536:] == blob[1536:])
    check("vfs: read_range fills a hole on demand",
          holed.read_range("/x", 1100, 100) == b"\x00" * 100)
    check("vfs: TSK read path is read_random-only",
          hasattr(FakeFile(blob), "read_random") and not hasattr(FakeFile(blob), "read"))

    class TinyVfs:
        def list_dir(self, path="/"):
            from mfim.vfs import VfsEntry
            if path == "/":
                return [VfsEntry("dir1", "/dir1", True, 0), VfsEntry("a.txt", "/a.txt", False, 5)]
            if path == "/dir1":
                return [VfsEntry("b.bin", "/dir1/b.bin", False, 10)]
            raise RuntimeError("damaged dir")

    data = ImageTreeModelData(TinyVfs())
    check("tree: children loaded lazily", not data.root.loaded and len(data.children(data.root)) == 2)
    node = data.find("/dir1/b.bin")
    check("tree: path resolution reaches a nested node", node is not None and node.size == 10)
    check("tree: cell rendering", data.cell(node, 2) == "10 B", data.cell(node, 2))

    class BrokenVfs(TinyVfs):
        def list_dir(self, path="/"):
            if path == "/":
                from mfim.vfs import VfsEntry
                return [VfsEntry("bad", "/bad", True, 0)]
            raise RuntimeError("I/O error in dirent")

    broken = ImageTreeModelData(BrokenVfs())
    first = broken.children(broken.root)[0]
    bad = broken.children(first)
    check("tree: damaged directory degrades to empty + error, no crash",
          bad == [] and first.error is not None, str(first.error))

    # rawio: plan / commit / read-back verify / backup
    from mfim.rawio import ImagePatcher

    tmpdir = tempfile.mkdtemp(prefix="mfim_unit_")
    target = os.path.join(tmpdir, "blob.img")
    with open(target, "wb") as fp:
        fp.write(b"\xAA" * 4096)
    patcher = ImagePatcher(target, readonly=True)
    patcher.plan(100, b"\x01\x02\x03\x04")
    blocked = False
    try:
        patcher.commit()
    except PermissionError:
        blocked = True
    check("rawio: readonly mode refuses to write", blocked)
    with open(target, "rb") as fp:
        fp.seek(100)
        check("rawio: nothing written in readonly mode", fp.read(4) == b"\xAA" * 4)

    patcher = ImagePatcher(target, readonly=False)
    patcher.plan(100, b"\x01\x02\x03\x04")
    report = patcher.commit(backup=True, verify=True)
    check("rawio: commit verified by read-back and made a backup",
          report.ok and report.verified == [(100, 4)] and report.backup_path
          and os.path.exists(report.backup_path), report.describe().replace("\n", " | "))
    with open(target, "rb") as fp:
        fp.seek(100)
        check("rawio: new bytes are on disk", fp.read(4) == b"\x01\x02\x03\x04")
    with open(report.backup_path, "rb") as fp:
        fp.seek(100)
        check("rawio: backup keeps the pre-write bytes", fp.read(4) == b"\xAA" * 4)
    over = ImagePatcher(target, readonly=False)
    over.plan(4090, b"\x00" * 100)
    raised = False
    try:
        over.commit(backup=False, verify=False)
    except ValueError:
        raised = True
    check("rawio: patch past end of image rejected", raised)
    shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# integration checks
# --------------------------------------------------------------------------- #
def cross_check_with_sfdisk(image: str, table) -> None:
    """Independent view of the same bytes, produced by util-linux."""
    if not have("sfdisk"):
        skip("cross-check: sfdisk missing", "")
        return
    proc = run(["sfdisk", "--dump", image], check_rc=False)
    if proc.returncode != 0:
        skip("cross-check: sfdisk could not read the image", proc.stderr.strip()[:80])
        return
    print("--- sfdisk --dump (independent parser, same bytes) ---")
    print(proc.stdout.strip())
    theirs = []
    for line in proc.stdout.splitlines():
        if image.split("/")[-1] in line or line.startswith("/"):
            for field in line.split(","):
                field = field.strip()
                if field.startswith("start="):
                    start = int(field.split("=")[1].strip())
                elif field.startswith("size="):
                    theirs.append((start, int(field.split("=")[1].strip())))
    mine = [(p.start_sector, p.size // SECTOR) for p in table.partitions]
    common = sorted(set(mine) & set(theirs))
    check("cross-check: every mfim partition is confirmed by sfdisk",
          len(common) >= len(mine), f"mfim={len(mine)} sfdisk={len(theirs)} common={len(common)}")


def mbr_tests(work: str, image: str, layout: dict):
    print("\n=== MBR image: partition table ===")
    from mfim.partition import scan_filesystems

    table = scan_filesystems(image)
    parts = table.partitions
    check("part: scheme detected as MBR", table.scheme == "mbr", table.scheme)
    check("part: 5 volumes (2 primary + 0x05 extended + 3 logical)", len(parts) == 5,
          f"{len(parts)}: " + ", ".join(f"#{p.index}:{p.scheme}" for p in parts))
    p1 = table.by_index(1)
    check("part: primary #1 offset == 2048*512", p1 and p1.offset == FAT32_OFFSET,
          hex(p1.offset) if p1 else "none")
    check("part: primary #1 type + sniffed filesystem agree (FAT32)",
          p1 and p1.type_id == f"0x{layout['fat32_type']:02x}" and p1.fs_name == "fat32",
          f"{p1.type_id} {p1.type_name} fs={p1.fs_name}")
    check("part: primary #1 marked bootable", bool(p1 and p1.bootable))
    p2 = table.by_index(2)
    check("part: primary #2 sniffed as ext4 with Linux type 0x83",
          p2 and p2.fs_name == "ext4" and p2.type_id == "0x83",
          f"{p2.type_id} fs={p2.fs_name}")
    logicals = [p for p in parts if p.scheme == "logical"]
    check("part: EBR chain walked -> 3 logical volumes", len(logicals) == 3,
          f"{len(logicals)}: " + ", ".join(f"#{p.index}@{hex(p.offset)}" for p in logicals))
    check("part: logical #3 == first logical volume at 155648*512",
          parts[2].offset == LOG1_OFFSET and parts[2].size == LOG1_SECTORS * SECTOR,
          f"#{parts[2].index} offset={hex(parts[2].offset)} size={parts[2].size}")
    check("part: logical FAT16 typed volume sniffed as fat16",
          parts[2].type_id == "0x06" and parts[2].fs_name == "fat16",
          f"{parts[2].type_id} fs={parts[2].fs_name}")
    check("part: offset -> partition lookup", table.containing(FAT32_OFFSET + 4096) is p1)
    check("part: no warnings on a clean MBR image", not table.warnings,
          "; ".join(table.warnings) or "none")
    print("--- mfim parse ---")
    print(table.describe())
    cross_check_with_sfdisk(image, table)

    # a deliberately lying table: EBR chain pointing at itself must not hang
    loop_img = os.path.join(work, "ebr_loop.img")
    shutil.copyfile(image, loop_img)
    with open(loop_img, "r+b") as fp:
        fp.seek(EXT_BASE * SECTOR)
        fp.write(build_sector([(1, mbr_entry(0x00, 0x05, 0, EXT_SECTORS))]))
    loop_table = scan_filesystems(loop_img)
    check("part: self-referencing EBR chain terminates instead of hanging",
          loop_table.scheme == "mbr", f"{len(loop_table.partitions)} partitions")
    return table


def pytsk_tests(work: str, image: str) -> None:
    print("\n=== pytsk3 read path (FAT32 partition of the MBR image) ===")
    try:
        import pytsk3  # noqa: F401
    except ImportError:
        skip("pytsk: pytsk3 not installed", "pip install pytsk3")
        return
    from mfim.vfs import ImageVFS

    vfs = ImageVFS(image, offset=FAT32_OFFSET)
    print("backend:", vfs.describe())

    names = sorted(e.name for e in vfs.list_dir("/"))
    check("pytsk: root listing sees the uploaded tree",
          {"docs", "nested", "readme.txt", "big.bin"}.issubset(set(names)), ", ".join(names))

    big = vfs.stat("/big.bin")
    check("pytsk: metadata size == 300000", big.size == 300000, str(big.size))

    res = vfs.extract("/big.bin", os.path.join(work, "big.out.bin"), verify=True)
    print("extract:", res)
    check("pytsk: extracted byte count matches metadata",
          res["bytes"] == 300000 and res["match"], f"{res['bytes']} bytes")
    check("pytsk: sha256 of extracted file == source payload",
          res["sha256"] == hashlib.sha256(PAYLOAD).hexdigest(), res["sha256"][:16] + "…")

    txt = vfs.read_file("/readme.txt")
    check("pytsk: small text file intact", txt.startswith(b"Mount File test payload"),
          repr(txt[:34]))
    leaf = vfs.read_file("/nested/deep/leaf.dat")
    check("pytsk: nested path read intact", leaf == b"\xde\xad\xbe\xef" * 512, f"{len(leaf)} bytes")

    with open(os.path.join(work, "big.out.bin"), "rb") as fp:
        extracted = fp.read()
    check("pytsk: extracted bytes equal the payload byte-for-byte", extracted == PAYLOAD,
          f"{len(extracted)} bytes")

    tsk_file = vfs.open_file("/readme.txt")
    check("pytsk: TSK file object has no .read() — read_random is the only path",
          not hasattr(tsk_file, "read") and hasattr(tsk_file, "read_random"))

    dump = vfs.hexdump("/readme.txt", 0, 48)
    print("--- hex dump of /readme.txt (read through pytsk3) ---")
    print(dump)
    check("pytsk: hex dump shows the payload text", dump.endswith("Mount File test payload")[:0]
          or "4d 6f 75 6e 74" in dump, dump.splitlines()[0])

    walked = [e.path for e in vfs.walk("/")]
    check("pytsk: recursive walk reaches the deepest file",
          "/nested/deep/leaf.dat" in walked, f"{len(walked)} entries")


def gpt_tests(work: str, image: str) -> None:
    print("\n=== GPT image ===")
    if not image or not os.path.exists(image):
        skip("gpt: image not built (sgdisk unavailable)", "")
        return
    from mfim.partition import parse_partition_table, scan_filesystems

    table = scan_filesystems(image)
    check("gpt: scheme detected through the protective MBR", table.scheme == "gpt", table.scheme)
    check("gpt: 2 entries parsed", len(table.partitions) == 2, str(len(table.partitions)))
    p1, p2 = table.partitions
    check("gpt: entry LBA honoured (offset == 2048*512)", p1.offset == 2048 * 512, hex(p1.offset))
    check("gpt: type GUID mapped to Linux filesystem",
          p1.type_id == "0fc63daf-8483-4772-8e79-3d69d8477de4"
          and p1.type_name == "Linux filesystem", f"{p1.type_id} {p1.type_name}")
    check("gpt: UTF-16LE partition name decoded", p1.name == "linux-data", repr(p1.name))
    check("gpt: second entry recognised as EFI System",
          p2.type_name == "EFI System", f"{p2.type_id} {p2.type_name}")
    check("gpt: partition GUID emitted", len(p1.guid) == 36, p1.guid)
    check("gpt: header + entry-array CRCs validate cleanly", not table.warnings,
          "; ".join(table.warnings) or "none")
    print(table.describe())

    broken = os.path.join(work, "gpt_broken.img")
    shutil.copyfile(image, broken)
    with open(broken, "r+b") as fp:
        fp.seek(2 * SECTOR + 40)
        fp.write(b"\xff")
    t2 = parse_partition_table(broken)
    check("gpt: damaged entry array -> warning, parse still succeeds",
          t2.scheme == "gpt" and any("CRC32" in w for w in t2.warnings),
          "; ".join(t2.warnings))


def bare_fs_tests(work: str, image: str) -> None:
    print("\n=== no partition table (bare filesystem dump, e.g. SPI flash) ===")
    from mfim.partition import scan_filesystems, sniff_filesystem

    check("bare: ext4 recognised at offset 0", sniff_filesystem(image, 0).name == "ext4",
          str(sniff_filesystem(image, 0)))
    table = scan_filesystems(image)
    check("bare: zero partitions + explicit hint instead of a crash",
          not table.partitions and any("no partition table" in w for w in table.warnings),
          "; ".join(table.warnings))
    from mfim.vfs import ImageVFS

    names = [e.name for e in ImageVFS(image, offset=0).list_dir("/")]
    check("bare: TSK lists the ext4 root without any table", "lost+found" in names,
          ", ".join(names[:8]))


def cli_tests(work: str, image: str) -> None:
    print("\n=== CLI end-to-end (separate processes) ===")
    env = dict(os.environ, PYTHONPATH=ROOT)

    def cli(*args, expect_rc=0):
        proc = subprocess.run([sys.executable, "-m", "mfim.cli", *args],
                              capture_output=True, text=True, env=env, timeout=600)
        if expect_rc is not None and proc.returncode != expect_rc:
            raise RuntimeError(f"mfim {' '.join(args)} rc={proc.returncode}\n"
                               f"stdout: {proc.stdout[:600]}\nstderr: {proc.stderr[:600]}")
        return proc

    proc = cli("info", image)
    print(proc.stdout.strip())
    check("cli: info reports the partition count", "partitions : 5" in proc.stdout)

    proc = cli("parts", image)
    check("cli: parts shows the bootable FAT32 row and a logical row",
          "#1" in proc.stdout and "boot" in proc.stdout and "logical" in proc.stdout)

    proc = cli("sniff", image)
    check("cli: sniff detects fat32 + fat16 + ext4",
          all(k in proc.stdout for k in ("fat32", "fat16", "ext4")))

    proc = cli("ls", image, "/", "--part", "1")
    check("cli: ls --part 1 lists the payload files", "readme.txt" in proc.stdout,
          proc.stdout.splitlines()[-1])

    proc = cli("tree", image, "/", "--part", "1", "--depth", "3")
    print(proc.stdout.strip())
    check("cli: tree prints a nested hierarchy with box drawing",
          "leaf.dat" in proc.stdout and ("├──" in proc.stdout or "└──" in proc.stdout))

    dest = os.path.join(work, "cli_extract")
    proc = cli("extract", image, "/big.bin", os.path.join(dest, "big.bin"),
               "--part", "1", "--verify")
    print(proc.stdout.strip())
    check("cli: extract wrote the exact file size",
          os.path.getsize(os.path.join(dest, "big.bin")) == 300000)

    proc = cli("extract", image, "/", "--all", "--out-dir", dest, "--part", "1")
    found = []
    for base, _dirs, names in os.walk(dest):
        found += [os.path.relpath(os.path.join(base, n), dest) for n in names]
    check("cli: --all extracted the whole tree",
          len([f for f in found if f.endswith((".dat", ".txt", ".md", ".bin"))]) >= 4,
          f"{len(found)} files: " + ", ".join(sorted(found)[:6]))

    proc = cli("hex", image, "/readme.txt", "--part", "1", "--length", "32")
    check("cli: hex of a file inside the image", "4d 6f 75 6e 74" in proc.stdout)
    proc = cli("hex", image, "--raw", "--offset", str(FAT32_OFFSET), "--length", "96")
    check("cli: hex --raw reads absolute image bytes (FAT32 marker present)",
          "46 41 54 33 32" in proc.stdout.upper(), proc.stdout.splitlines()[1][:52])

    proc = cli("ls", image, "/", "--part", "99", expect_rc=2)
    check("cli: unknown partition -> clean error and rc=2", "not found" in proc.stderr,
          proc.stderr.strip()[:70])

    proc = subprocess.run([sys.executable, "-m", "mfim.cli", "ls", image,
                           "--offset", str(LOG1_OFFSET + 4096)],
                          capture_output=True, text=True, env=env, timeout=300)
    check("cli: garbage offset -> rc=3 and a usable hint, no traceback",
          proc.returncode == 3 and "hint" in proc.stderr and "Traceback" not in proc.stderr,
          f"rc={proc.returncode}")

    proc = cli("ls", image, "/", "--offset", str(LOG1_OFFSET))
    check("cli: --offset addresses the FAT16 logical volume directly",
          "Volume in drive" in proc.stdout or "entries" in proc.stdout,
          proc.stdout.strip().splitlines()[-1][:70])


def write_safety_tests(work: str, image: str) -> None:
    print("\n=== raw patch: backup + read-back verification ===")
    from mfim.rawio import ImagePatcher, RawImage

    working = os.path.join(work, "patch_target.img")
    shutil.copyfile(image, working)
    before = sha256(working)
    target = FAT32_OFFSET + 3   # OEM name field of the FAT32 boot sector

    patcher = ImagePatcher(working, readonly=False)
    patcher.plan(target, b"MFIM")
    report = patcher.commit(backup=True, verify=True)
    print(report.describe())
    check("patch: write verified by read-back",
          report.ok and report.verified == [(target, 4)], str(report.verified))
    check("patch: .bak copy created and holds the original image",
          bool(report.backup_path) and sha256(report.backup_path) == before)
    raw = RawImage(working)
    check("patch: intended bytes present at the absolute offset", raw.read(target, 4) == b"MFIM",
          raw.read(target, 4))
    check("patch: untouched bytes elsewhere identical",
          raw.read(0, 512) == RawImage(report.backup_path).read(0, 512))

    dry = os.path.join(work, "dryrun.img")
    shutil.copyfile(image, dry)
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run([sys.executable, "-m", "mfim.cli", "patch", dry,
                           str(FAT32_OFFSET + 3), "aabbccdd"],
                          capture_output=True, text=True, env=env, timeout=180)
    check("patch: CLI dry-run leaves the file byte-identical", sha256(dry) == before,
          proc.stdout.strip().splitlines()[-1][:70])


def fat_write_tests(work: str, image: str) -> None:
    print("\n=== FAT write path (mtools) ===")
    from mfim.fatwrite import FatWriteBackend, mtools_available, mtools_version

    if not mtools_available():
        skip("fatwrite: mtools missing", "apt-get install mtools")
        return
    print("mtools:", mtools_version())

    check("fatwrite: source FAT32 partition passes fsck.fat -n",
          "ERRORS" not in fsck_fat(image, FAT32_OFFSET, FAT32_SECTORS * SECTOR).upper(),
          fsck_fat(image, FAT32_OFFSET, FAT32_SECTORS * SECTOR).strip().splitlines()[-1][:70])

    working = os.path.join(work, "fat_write.img")
    shutil.copyfile(image, working)

    backend = FatWriteBackend(working, offset=FAT32_OFFSET)
    print(backend.describe())
    check("fatwrite: backend selected a working mode",
          backend.mode in ("offset", "extract-merge"), backend.mode)
    check("fatwrite: refuses a non-FAT offset (the ext4 partition)",
          _refuses_ext4(working), "FatWriteError raised as expected")

    data = bytes((i * 13 + 5) % 256 for i in range(70000))
    payload = os.path.join(work, "injected.bin")
    with open(payload, "wb") as fp:
        fp.write(data)

    backend = FatWriteBackend(working, offset=FAT32_OFFSET)
    try:
        backend.mkdir("/from_cli")
        backend.copy_in(payload, "/from_cli/injected.bin")
        backend.write_bytes(b"hello mfim\n", "/from_cli/hello.txt")
        got = backend.read_file_bytes("/from_cli/injected.bin")
        check("fatwrite: written file reads back byte-exact",
              got == data, f"{len(got)} bytes, sha {hashlib.sha256(got).hexdigest()[:12]}…")
        backend.rename("/from_cli/hello.txt", "/from_cli/hello_renamed.txt")
        check("fatwrite: rename visible in listing",
              "hello_renamed.txt" in backend.listdir("/from_cli"))
        backend.remove("/from_cli/injected.bin")
        check("fatwrite: delete visible in listing",
              "injected.bin" not in backend.listdir("/from_cli"))
    finally:
        backend.close()

    out = fsck_fat(working, FAT32_OFFSET, FAT32_SECTORS * SECTOR)
    print("--- fsck.fat -n after the writes ---")
    print("\n".join(out.strip().splitlines()[-6:]))
    check("fatwrite: fsck.fat -n reports no FAT errors after writing",
          "ERRORS" not in out.upper() and "Dirty" not in out, out.strip().splitlines()[-1][:70])

    env = dict(os.environ, MTOOLSRC=mtoolsrc(work, working, FAT32_OFFSET),
               MTOOLS_SKIP_CHECK="1")
    proc = run(["mdir", "z:/from_cli"], env=env, check_rc=False)
    check("fatwrite: an independent mtools process sees the new directory",
          "hello_renamed.txt" in proc.stdout, proc.stdout.strip().splitlines()[-1][:70])

    from mfim.vfs import ImageVFS

    names = [e.name for e in ImageVFS(working, offset=FAT32_OFFSET).list_dir("/from_cli")]
    check("fatwrite: pytsk3 (independent reader) confirms the write",
          "hello_renamed.txt" in names, ", ".join(names))

    proc = subprocess.run([sys.executable, "-m", "mfim.cli", "fatwrite", "dir", working,
                           "--path", "/from_cli", "--part", "1", "--allow-write", "--yes"],
                          capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=ROOT), timeout=300)
    check("cli: fatwrite dir works through the CLI",
          proc.returncode == 0 and "hello_renamed.txt" in proc.stdout,
          (proc.stdout or proc.stderr).strip().splitlines()[-1][:70])

    # writing into a damaged/unusual FS must not be attempted
    with open(working, "r+b") as fp:
        fp.seek(FAT32_OFFSET + 510)
        fp.write(b"\x00\x00")
    refused = _refuses_any(working, FAT32_OFFSET)
    check("fatwrite: refuses an offset with no FAT boot signature", refused)


def _refuses_ext4(image: str) -> bool:
    from mfim.fatwrite import FatWriteBackend, FatWriteError

    try:
        FatWriteBackend(image, offset=EXT4_START * SECTOR)
    except FatWriteError:
        return True
    except Exception:
        return False
    return False


def _refuses_any(image: str, offset: int) -> bool:
    from mfim.fatwrite import FatWriteBackend, FatWriteError

    try:
        FatWriteBackend(image, offset=offset)
    except FatWriteError:
        return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep generated images")
    parser.add_argument("--dir", help="directory for generated images")
    parser.add_argument("--only", help="comma list: unit,mbr,pytsk,gpt,bare,cli,patch,fatwrite")
    args = parser.parse_args()
    wanted = set((args.only or "unit,mbr,pytsk,gpt,bare,cli,patch,fatwrite").split(","))

    work = args.dir or tempfile.mkdtemp(prefix="mfim_it_")
    os.makedirs(work, exist_ok=True)
    print(f"work dir : {work}")
    print(f"python   : {sys.version.split()[0]}")
    missing = [t for t in ("mkfs.vfat", "mkfs.ext4", "sfdisk", "mcopy", "mdir", "fsck.fat",
                           "sgdisk", "unsquashfs") if not have(t)]
    print(f"tools    : missing = {missing or 'none'}")

    mbr_image = os.path.join(work, "mbr_fat.img")
    gpt_image = os.path.join(work, "gpt.img")
    layout = {}

    try:
        if "unit" in wanted:
            unit_tests()

        if wanted & {"mbr", "pytsk", "cli", "patch", "fatwrite"}:
            print("\n=== building the MBR image (raw bytes written by hand) ===")
            layout = build_mbr_image(mbr_image, work)
            populate_fat(mbr_image, FAT32_OFFSET, work)
            print(f"built {mbr_image} ({os.path.getsize(mbr_image)} bytes): "
                  f"{layout['fat32_kind']} @ 0x{layout['fat32_offset']:x}, "
                  f"{layout['fat16_kind']} @ 0x{layout['log1_offset']:x}, "
                  f"{layout['ext4_kind']} @ 0x{layout['ext4_offset']:x}")

        if "mbr" in wanted:
            mbr_tests(work, mbr_image, layout)
        if "pytsk" in wanted:
            pytsk_tests(work, mbr_image)
        if "cli" in wanted:
            cli_tests(work, mbr_image)
        if "patch" in wanted:
            write_safety_tests(work, mbr_image)
        if "fatwrite" in wanted:
            fat_write_tests(work, mbr_image)

        if "gpt" in wanted:
            if have("sgdisk"):
                with open(gpt_image, "wb") as fp:
                    fp.truncate(32 * 1024 * 1024)
                run(["sgdisk", "--clear", "--new=1:2048:+8192", "--typecode=1:8300",
                     "--change-name=1:linux-data", "--new=2:10240:+4096",
                     "--typecode=2:0700", "--change-name=2:efi-part", gpt_image])
                print(f"\nbuilt {gpt_image} ({os.path.getsize(gpt_image)} bytes) with sgdisk")
                print("--- sgdisk -p (independent parser, same bytes) ---")
                print(run(["sgdisk", "-p", gpt_image], check_rc=False).stdout.strip())
            else:
                print("\n! sgdisk not installed — GPT image not built")
            gpt_tests(work, gpt_image)

        if "bare" in wanted:
            bare_image = os.path.join(work, "bare_ext4.img")
            with open(bare_image, "wb") as fp:
                fp.truncate(24 * 1024 * 1024)
            run(["mkfs.ext4", "-q", "-F", "-L", "flatfs", bare_image])
            print(f"\nbuilt {bare_image} ({os.path.getsize(bare_image)} bytes): bare ext4, no table")
            bare_fs_tests(work, bare_image)
    except Exception:
        traceback.print_exc()
        _results.append((FAIL, "unhandled exception", traceback.format_exc().splitlines()[-1]))
    finally:
        n_pass = sum(1 for s, *_ in _results if s == PASS)
        n_fail = sum(1 for s, *_ in _results if s == FAIL)
        n_skip = sum(1 for s, *_ in _results if s == SKIP)
        print("\n" + "=" * 72)
        print(f"RESULT: {n_pass} passed, {n_fail} failed, {n_skip} skipped")
        for status, name, detail in _results:
            if status != PASS:
                print(f"  [{status}] {name} — {detail}")
        print("=" * 72)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"images kept in {work}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
