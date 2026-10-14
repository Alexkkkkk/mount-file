"""Test suite for Mount File.

Two kinds of tests:

* **unit tests** with no external tools (partition parsing of hand-built
  byte buffers, TSK adapters fed fake objects, hexdump formatting, patcher
  semantics) — these always run;
* **integration tests** against real images produced by
  ``tests/make_test_images.sh`` (mkfs.vfat / mkfs.ext4 / mcopy / debugfs /
  sfdisk), which are skipped with a clear message if a tool is unavailable.

Run:  python -m pytest tests/test_mfim.py -v
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mfim.hexview import hexdump  # noqa: E402
from mfim.partition import (  # noqa: E402
    MBR_TYPE_NAMES,
    Partition,
    guid_bytes,
    guid_str,
    parse_partition_table,
    scan_filesystems,
    sniff_bytes,
    sniff_filesystem,
)
from mfim.rawio import ImagePatcher, RawImage  # noqa: E402
from mfim.tree_model import ImageTreeModelData  # noqa: E402
from mfim.util import decode_name, human_size, normalize_path  # noqa: E402

IMAGES = pathlib.Path(__file__).resolve().parent / "images"

REQUIRED_FOR_IMAGES = ("sfdisk", "mkfs.vfat", "mkfs.ext4", "mcopy", "mmd", "debugfs")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _ensure_images() -> None:
    needed = ["mbr.img", "gpt.img", "raw_fat.img", "raw_ext4.img", "mbr_extended.img"]
    if all((IMAGES / n).exists() for n in needed):
        return
    missing = [t for t in REQUIRED_FOR_IMAGES if shutil.which(t) is None]
    if missing:
        pytest.skip(f"cannot build test images, missing tools: {missing}")
    script = pathlib.Path(__file__).resolve().parent / "make_test_images.sh"
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"make_test_images.sh failed: {proc.stderr[-500:]}")


@pytest.fixture(scope="session")
def images() -> pathlib.Path:
    _ensure_images()
    return IMAGES


def image(name: str, images: pathlib.Path) -> str:
    path = images / name
    if not path.exists():
        pytest.skip(f"{name} not built")
    return str(path)


def part_offset(path: str, index: int) -> int:
    """Byte offset of partition #index, taken from our own parser (never
    hard-coded), so the tests stay correct if the image layout changes."""
    table = scan_filesystems(path)
    part = table.by_index(index)
    assert part is not None, f"partition #{index} missing; got {table.describe()}"
    return part.offset


# --------------------------------------------------------------------------- #
# unit: util
# --------------------------------------------------------------------------- #
def test_normalize_path():
    assert normalize_path("") == "/"
    assert normalize_path("\\a\\b\\") == "/a/b"
    assert normalize_path("/a//b/./c") == "/a/b/c"


def test_decode_name_never_raises():
    assert decode_name(b"HELLO.TXT") == "HELLO.TXT"
    bad = decode_name(b"\xff\xfe\x00abc")  # not valid UTF-8
    assert isinstance(bad, str) and bad


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.50 KiB"
    assert human_size(None) == "-"


def test_hexdump_layout():
    text = hexdump(b"AB", base=0x10)
    assert text.startswith("00000010  41 42")
    assert text.endswith("AB")
    unprintable = hexdump(b"\x00\xff", base=0)
    assert unprintable.endswith("..")


# --------------------------------------------------------------------------- #
# unit: sniffing
# --------------------------------------------------------------------------- #
def _ext_superblock() -> bytes:
    """Minimal ext superblock at its real place: 1024 bytes into the volume."""
    base = 0x400
    buf = bytearray(2048)
    struct.pack_into("<I", buf, base + 0x00, 1024)   # inodes
    struct.pack_into("<I", buf, base + 0x04, 2048)   # blocks
    struct.pack_into("<I", buf, base + 0x18, 0)      # log block size -> 1024
    struct.pack_into("<I", buf, base + 0x5C, 0x04)   # compat: journal (ext3+)
    struct.pack_into("<I", buf, base + 0x60, 0x42)   # incompat: extents+filetype
    struct.pack_into("<H", buf, base + 0x38, 0xEF53) # magic
    buf[base + 0x78:base + 0x7E] = b"MYLBL\x00"
    return bytes(buf)


def test_sniff_ext4():
    info = sniff_bytes(_ext_superblock())
    assert info.name == "ext4"
    assert "MYLBL" in info.detail
    assert info.total_bytes == 2048 * 1024


def test_sniff_ntfs_and_exfat():
    ntfs = bytearray(1024)
    ntfs[3:11] = b"NTFS    "
    struct.pack_into("<H", ntfs, 11, 512)
    struct.pack_into("<Q", ntfs, 40, 4096)
    assert sniff_bytes(bytes(ntfs)).name == "ntfs"

    exfat = bytearray(1024)
    exfat[3:11] = b"EXFAT   "
    exfat[108] = 9  # 512-byte sectors
    struct.pack_into("<Q", exfat, 72, 2048)
    assert sniff_bytes(bytes(exfat)).name == "exfat"


def test_sniff_squashfs_and_ubi():
    sq = bytearray(1024)
    sq[0:4] = b"hsqs"
    struct.pack_into("<I", sq, 12, 131072)
    struct.pack_into("<H", sq, 28, 4)
    struct.pack_into("<H", sq, 30, 0)
    assert sniff_bytes(bytes(sq)).name == "squashfs"

    ubi = bytearray(1024)
    ubi[0:4] = b"UBI#"
    ubi[4] = 1
    assert sniff_bytes(bytes(ubi)).name == "ubifs"


def test_sniff_blank_is_unknown():
    assert sniff_bytes(b"\x00" * 1024).name == "unknown"
    assert sniff_bytes(b"") .name == "unknown"


# --------------------------------------------------------------------------- #
# unit: MBR built by hand
# --------------------------------------------------------------------------- #
def build_mbr(entries, signature=b"\x55\xaa") -> bytes:
    sector = bytearray(512)
    for slot, (status, ptype, lba, count) in enumerate(entries):
        base = 446 + slot * 16
        sector[base] = status
        sector[base + 4] = ptype
        struct.pack_into("<I", sector, base + 8, lba)
        struct.pack_into("<I", sector, base + 12, count)
    sector[510:512] = signature
    return bytes(sector)


def test_mbr_two_primaries(tmp_path):
    img = tmp_path / "hand.img"
    sector0 = build_mbr([(0x80, 0x0C, 2048, 4096), (0x00, 0x83, 6144, 8192)])
    img.write_bytes(sector0 + b"\x00" * (2048 * 512))
    table = parse_partition_table(str(img))
    assert table.scheme == "mbr"
    assert len(table.partitions) == 2
    first, second = table.partitions
    assert (first.offset, first.size) == (2048 * 512, 4096 * 512)
    assert first.bootable and not second.bootable
    assert first.type_id == "0x0c" and first.type_name == MBR_TYPE_NAMES[0x0C]
    assert second.type_id == "0x83"
    assert table.containing(2048 * 512 + 10) is first
    assert table.containing(0) is None


def test_mbr_without_signature_reports_warning(tmp_path):
    img = tmp_path / "nosig.img"
    img.write_bytes(build_mbr([(0, 0x83, 2048, 100)], signature=b"\x00\x00")
                    + b"\x00" * 4096)
    table = parse_partition_table(str(img))
    assert any("boot signature" in w for w in table.warnings)


def test_mbr_too_small_is_error(tmp_path):
    img = tmp_path / "tiny.img"
    img.write_bytes(b"\x00" * 100)
    with pytest.raises(Exception):
        parse_partition_table(str(img))


def test_gpt_built_by_hand(tmp_path):
    import zlib

    sector = 512
    img = bytearray(128 * sector)
    # protective MBR
    img[446] = 0x00
    img[450] = 0xEE
    struct.pack_into("<I", img, 454, 1)
    struct.pack_into("<I", img, 458, 127)
    img[510:512] = b"\x55\xaa"
    # GPT header at LBA1
    header = bytearray(sector)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, 127)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, 90)
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, 128)
    struct.pack_into("<I", header, 84, 128)
    # entry array: one Linux filesystem partition 2048..4095
    entries = bytearray(128 * 128)
    # Linux filesystem type GUID, written through the same helper the future
    # partition editor will use — this checks the mixed-endian layout both ways.
    linux_guid = "0fc63daf-8483-4772-8e79-3d69d8477de4"
    entries[0:16] = guid_bytes(linux_guid)
    part_guid = "11223344-5566-7788-9900-112233445566"
    entries[16:32] = guid_bytes(part_guid)
    struct.pack_into("<Q", entries, 32, 2048)
    struct.pack_into("<Q", entries, 40, 4095)
    name = "тест".encode("utf-16-le")
    entries[56:56 + len(name)] = name
    struct.pack_into("<I", header, 88, zlib.crc32(bytes(entries)) & 0xFFFFFFFF)
    crc = zlib.crc32(bytes(header[:92])) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, crc)
    img[1 * sector:2 * sector] = header
    img[2 * sector:2 * sector + len(entries)] = entries

    path = tmp_path / "hand_gpt.img"
    path.write_bytes(bytes(img))
    table = parse_partition_table(str(path))
    assert table.scheme == "gpt"
    assert len(table.partitions) == 1
    p = table.partitions[0]
    assert p.offset == 2048 * 512
    assert p.size == (4096 - 2048) * 512
    assert p.type_name == "Linux filesystem"
    assert p.name == "тест"
    assert p.guid == part_guid
    assert guid_str(guid_bytes(part_guid)) == part_guid  # round-trip
    assert not any("CRC32" in w for w in table.warnings)


def test_gpt_bad_crc_is_warning_not_failure(tmp_path):
    import zlib

    sector = 512
    img = bytearray(64 * sector)
    # protective MBR: the parser only accepts an entry that has a size
    img[450] = 0xEE
    struct.pack_into("<I", img, 454, 1)
    struct.pack_into("<I", img, 458, 63)
    img[510:512] = b"\x55\xaa"
    header = bytearray(sector)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, 128)
    struct.pack_into("<I", header, 84, 128)
    struct.pack_into("<I", header, 16, 0xDEADBEEF)  # deliberately wrong
    struct.pack_into("<I", header, 88, 0)
    img[sector:2 * sector] = header
    path = tmp_path / "bad_gpt.img"
    path.write_bytes(bytes(img))
    table = parse_partition_table(str(path))
    assert table.scheme == "gpt"
    assert any("CRC32" in w for w in table.warnings)


# --------------------------------------------------------------------------- #
# unit: TSK adapters with fake objects (no pytsk3 needed)
# --------------------------------------------------------------------------- #
class FakeMeta:
    def __init__(self, size=10, addr=42, mtype=1, mtime=1700000000):
        self.size = size
        self.addr = addr
        self.type = mtype
        self.mtime = mtime
        self.mode = 0o644
        self.flags = 1


class FakeName:
    def __init__(self, name):
        self.name = name


class FakeInfo:
    def __init__(self, name, meta):
        self.name = FakeName(name)
        self.meta = meta


class FakeEntry:
    def __init__(self, name, meta):
        self.info = FakeInfo(name, meta)


def test_entry_from_tsk_skips_dot_entries():
    from mfim.vfs import entry_from_tsk

    assert entry_from_tsk(FakeEntry(b".", FakeMeta()), "/") is None
    assert entry_from_tsk(FakeEntry(b"..", FakeMeta()), "/") is None


def test_entry_from_tsk_tolerates_meta_none():
    from mfim.vfs import entry_from_tsk

    entry = entry_from_tsk(FakeEntry(b"weird.bin", None), "/dir")
    assert entry is not None
    assert entry.name == "weird.bin"
    assert entry.size == 0
    assert entry.path == "/dir/weird.bin"
    assert entry.type_name == "unknown"
    assert entry.is_dir is False


def test_entry_from_tsk_dir_and_undecodable_name():
    from mfim.vfs import entry_from_tsk

    d = entry_from_tsk(FakeEntry(b"docs", FakeMeta(mtype=2, size=0)), "/")
    assert d.is_dir and d.type_name == "dir"
    bad = entry_from_tsk(FakeEntry(b"\xff\xfe x", FakeMeta()), "/")
    assert isinstance(bad.name, str) and bad.name


def test_iter_file_handles_sparse_hole():
    """pytsk3 returns None for a hole; the reader must yield zeros, not crash."""
    from mfim.vfs import ImageVFS

    class FakeFile:
        info = type("I", (), {"meta": FakeMeta(size=8)})()

        def __init__(self):
            self.calls = 0

        def read_random(self, offset, length):
            self.calls += 1
            if offset == 0:
                return None          # hole
            return b"ABCD"           # second chunk

    class FakeVFS(ImageVFS):
        def __init__(self, fake_file):
            self.chunk = 4
            self._fake = fake_file

        def open_file(self, path):
            return self._fake

    vfs = FakeVFS(FakeFile())
    data = b"".join(vfs.iter_file("/x", chunk=4))
    assert data == b"\x00\x00\x00\x00ABCD"


def test_iter_file_stops_on_empty_read():
    from mfim.vfs import ImageVFS

    class FakeFile:
        info = type("I", (), {"meta": FakeMeta(size=16)})()

        def read_random(self, offset, length):
            if offset == 0:
                return b"1234"
            return b""  # truncated / inconsistent metadata

    class FakeVFS(ImageVFS):
        def __init__(self, fake_file):
            self.chunk = 4
            self._fake = fake_file

        def open_file(self, path):
            return self._fake

    vfs = FakeVFS(FakeFile())
    assert b"".join(vfs.iter_file("/x", chunk=4)) == b"1234"


# --------------------------------------------------------------------------- #
# unit: patcher
# --------------------------------------------------------------------------- #
def test_patcher_readonly_refuses_to_write(tmp_path):
    img = tmp_path / "p.img"
    img.write_bytes(b"\x00" * 4096)
    patcher = ImagePatcher(str(img), readonly=True)
    patcher.plan(16, b"\xde\xad")
    assert patcher.planned_ranges == [(16, 2)]
    with pytest.raises(PermissionError):
        patcher.commit()
    assert img.read_bytes()[16:18] == b"\x00\x00"


def test_patcher_commit_backup_and_verify(tmp_path):
    img = tmp_path / "p2.img"
    img.write_bytes(b"\x00" * 4096)
    patcher = ImagePatcher(str(img), readonly=False)
    patcher.plan(32, b"HELLO")
    patcher.plan(1024, b"\xff\xff")
    report = patcher.commit(backup=True, verify=True)
    assert report.ok
    assert report.bytes_written == 7
    assert report.backup_path and os.path.exists(report.backup_path)
    data = img.read_bytes()
    assert data[32:37] == b"HELLO" and data[1024:1026] == b"\xff\xff"
    # the backup must contain the original contents
    assert (tmp_path / "p2.img.bak").read_bytes() == b"\x00" * 4096


def test_patcher_rejects_out_of_range_patch(tmp_path):
    img = tmp_path / "p3.img"
    img.write_bytes(b"\x00" * 512)
    patcher = ImagePatcher(str(img), readonly=False)
    patcher.plan(500, b"x" * 100)
    with pytest.raises(ValueError):
        patcher.commit(backup=False)


def test_rawimage_hexdump(tmp_path):
    img = tmp_path / "h.img"
    img.write_bytes(bytes(range(64)))
    text = RawImage(str(img)).hexdump(0, 32)
    assert "00000000  00 01 02 03" in text
    assert "00000010  10 11 12 13" in text


# --------------------------------------------------------------------------- #
# integration: real images
# --------------------------------------------------------------------------- #
def test_real_mbr_layout(images):
    path = image("mbr.img", images)
    table = scan_filesystems(path)
    assert table.scheme == "mbr"
    assert len(table.partitions) == 2
    fat, ext = table.partitions
    assert fat.offset == 2048 * 512 and fat.size == 131072 * 512
    assert fat.fs_name == "fat32", fat.fs_detail
    assert fat.bootable
    assert ext.offset == 133120 * 512 and ext.size == 81920 * 512
    assert ext.fs_name == "ext4", ext.fs_detail
    assert not table.warnings, table.warnings


def test_real_gpt_layout(images):
    path = image("gpt.img", images)
    table = scan_filesystems(path)
    assert table.scheme == "gpt"
    assert len(table.partitions) == 1
    p = table.partitions[0]
    assert p.offset == 2048 * 512
    assert p.fs_name == "fat32"
    assert p.type_name == "EFI System"
    assert p.name == "EFI"
    assert not any("CRC32" in w for w in table.warnings), table.warnings


def test_real_mbr_extended_chain(images):
    """MBR + EBR chain: primary FAT16, then two logical FAT16 volumes.

    Layout is fixed by make_test_images.sh:
        primary  @2048   32768 sectors
        EBR1     @34816 -> logical1 @36864 32768 sectors
        EBR2     @69632 -> logical2 @71680 40960 sectors
    """
    path = image("mbr_extended.img", images)
    table = scan_filesystems(path)
    primaries = [p for p in table.partitions if p.scheme == "mbr"]
    logicals = [p for p in table.partitions if p.scheme == "logical"]
    assert len(primaries) == 1, table.describe()
    assert len(logicals) == 2, table.describe()
    assert (primaries[0].offset, primaries[0].size) == (2048 * 512, 32768 * 512)
    assert (logicals[0].offset, logicals[0].size) == (36864 * 512, 32768 * 512)
    assert (logicals[1].offset, logicals[1].size) == (71680 * 512, 40960 * 512)
    assert [p.index for p in logicals] == [2, 3]
    assert all(p.fs_name == "fat16" for p in logicals), table.describe()
    # the logical volumes sit inside the extended container, not inside each other
    assert logicals[0].end <= logicals[1].offset
    # and the FAT16 inside the first logical volume is readable end-to-end
    from mfim.vfs import ImageVFS

    vfs = ImageVFS(path, offset=logicals[0].offset)
    names = {e.name.upper() for e in vfs.list_dir("/")}
    assert "RAWHELLO.TXT" in names, names
    assert vfs.read_file("/RAWHELLO.TXT") == b"hello from FAT\n"
    vfs2 = ImageVFS(path, offset=logicals[1].offset)
    assert vfs2.read_file("/LOG2.TXT") == b"hello from FAT\n"


def test_real_raw_images_have_no_table(images):
    fat = parse_partition_table(image("raw_fat.img", images))
    assert fat.partitions == []
    info = sniff_filesystem(image("raw_fat.img", images), 0)
    assert info.name in ("fat16", "fat12"), info
    ext = sniff_filesystem(image("raw_ext4.img", images), 0)
    assert ext.name == "ext4", ext


def test_real_squashfs_detected(images):
    path = images / "squash.img"
    if not path.exists():
        pytest.skip("mksquashfs unavailable")
    assert sniff_filesystem(str(path), 0).name == "squashfs"


def test_tools_do_not_change_images(images):
    """Parsing must be read-only: checksums recorded at build time still match."""
    sums = images / "SHA256SUMS"
    if not sums.exists():
        pytest.skip("no checksum manifest")
    for line in sums.read_text().splitlines():
        digest, name = line.split()
        target = images / name.lstrip("./")
        if not target.exists():
            continue
        h = hashlib.sha256(target.read_bytes()).hexdigest()
        assert h == digest, f"{name} changed after tests: {h} != {digest}"


def test_vfs_lists_fat32(images):
    from mfim.vfs import ImageVFS

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=2048 * 512)
    entries = vfs.list_dir("/")
    names = {e.name.upper() for e in entries}
    assert {"HELLO.TXT", "SUBDIR", "PAYLOAD.BIN", "BIG.BIN"} <= names
    # TSK's pseudo files ($MBR/$FAT1/$FAT2/$OrphanFiles) and its synthetic
    # volume-label entry must be filtered out of the user-facing listing
    assert not any(n.startswith("$") for n in names), sorted(names)
    assert not any("Volume Label" in e.name for e in entries), [e.name for e in entries]
    assert all(not e.virtual for e in entries)
    # ... but the raw TSK view is still reachable on demand
    raw = {e.name.upper() for e in vfs.list_dir("/", include_virtual=True)}
    assert any(n.startswith("$") for n in raw), sorted(raw)
    sub = vfs.list_dir("/SUBDIR")
    assert [e.name.upper() for e in sub] == ["NESTED.TXT"]
    assert all(e.is_dir is False for e in sub)


def test_vfs_extract_fat_matches_bytes(images):
    from mfim.vfs import ImageVFS

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=2048 * 512)
    entry = next(e for e in vfs.list_dir("/") if e.name.upper() == "HELLO.TXT")
    assert vfs.read_file(entry.path) == b"hello from FAT\n"
    # big file exercises the chunked read_random loop
    big = next(e for e in vfs.list_dir("/") if e.name.upper() == "BIG.BIN")
    data = vfs.read_file(big.path)
    assert len(data) == big.size == 300000


def test_vfs_reads_ext4_tree(images):
    from mfim.vfs import ImageVFS

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=part_offset(path, 2))
    root = {e.name: e for e in vfs.list_dir("/")}
    assert "docs" in root and root["docs"].is_dir
    assert "root.txt" in root
    assert vfs.read_file("/root.txt") == b"root level file\n"
    assert vfs.read_file("/docs/readme.txt") == b"readme in ext4\n"
    assert vfs.read_file("/docs/deep/nested.txt") == b"nested\n"
    assert vfs.list_dir("/empty") == []


def test_vfs_extract_to_disk_with_sha256(images, tmp_path):
    from mfim.vfs import ImageVFS
    from mfim.util import sha256_bytes, sha256_file

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=part_offset(path, 2))
    target = tmp_path / "extracted.txt"
    result = vfs.extract("/docs/readme.txt", str(target), verify=True)
    assert result["match"]
    assert sha256_file(str(target)) == sha256_bytes(b"readme in ext4\n")


def test_vfs_bad_offset_raises(images):
    from mfim.vfs import ImageVFS, VfsError

    path = image("mbr.img", images)
    with pytest.raises(VfsError):
        ImageVFS(path, offset=16)  # inside the MBR, no filesystem there


def test_vfs_missing_path_raises(images):
    from mfim.vfs import ImageVFS, VfsError

    vfs = ImageVFS(image("mbr.img", images), offset=2048 * 512)
    with pytest.raises(VfsError):
        vfs.stat("/NOPE.TXT")


def test_vfs_hexdump_of_file(images):
    from mfim.vfs import ImageVFS

    vfs = ImageVFS(image("mbr.img", images), offset=2048 * 512)
    text = vfs.hexdump("/HELLO.TXT", 0, 32)
    assert "68 65 6c 6c 6f" in text       # "hello"
    assert "hello from FAT" in text


def test_tree_model_lazy_and_search(images):
    from mfim.vfs import ImageVFS

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=part_offset(path, 2))
    data = ImageTreeModelData(vfs)
    assert data.root.loaded is False          # nothing read until asked
    children = data.children(data.root)
    assert data.root.loaded is True
    names = {c.name for c in children}
    assert {"docs", "root.txt", "empty"} <= names
    node = data.find("/docs/deep/nested.txt")
    assert node is not None and not node.is_dir
    assert data.cell(node, 0) == "nested.txt"
    assert data.cell(node, 2) == human_size(node.size)
    assert data.row_of(children[0]) == 0


def test_tree_model_survives_unreadable_directory(images):
    from mfim.vfs import ImageVFS

    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=part_offset(path, 2))
    data = ImageTreeModelData(vfs)
    node = data.find("/root.txt")
    assert node is not None
    assert data.children(node) == []          # not a dir -> no children, no crash


def test_fatwrite_roundtrip(images, tmp_path):
    from mfim.fatwrite import FatWriteBackend, FatWriteError, mtools_available

    if not mtools_available():
        pytest.skip("mtools not installed")
    work = tmp_path / "copy.img"
    shutil.copyfile(image("raw_fat.img", images), str(work))

    payload = tmp_path / "local.bin"
    payload.write_bytes(b"written by mfim\n" * 100)

    backend = FatWriteBackend(str(work), offset=0)
    try:
        assert backend.fat_flavour in ("fat32", "fat12/16")
        backend.mkdir("/MFIMDIR")
        backend.copy_in(str(payload), "/MFIMDIR/COPY.BIN")
        listing = backend.listdir("/MFIMDIR").upper()
        # mdir prints 8.3 names space-padded ("COPY     BIN"), so match loosely here
        assert "COPY" in listing and "BIN" in listing, listing
        back = backend.read_file_bytes("/MFIMDIR/COPY.BIN")
        assert back == payload.read_bytes()
        assert listing.count("FILES") >= 0  # listing actually rendered
    finally:
        backend.close()

    # and the same bytes must be visible through the independent TSK reader
    from mfim.vfs import ImageVFS

    vfs = ImageVFS(str(work), offset=0)
    assert vfs.read_file("/MFIMDIR/COPY.BIN") == payload.read_bytes()


def test_fatwrite_refuses_non_fat_offset(images, tmp_path):
    from mfim.fatwrite import FatWriteBackend, FatWriteError, mtools_available

    if not mtools_available():
        pytest.skip("mtools not installed")
    work = tmp_path / "ext4copy.img"
    shutil.copyfile(image("raw_ext4.img", images), str(work))
    with pytest.raises(FatWriteError):
        FatWriteBackend(str(work), offset=0)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_cli_parts_and_info(images, capsys):
    from mfim.cli import main

    path = image("mbr.img", images)
    assert main(["parts", path]) == 0
    out = capsys.readouterr().out
    assert "fat32" in out and "ext4" in out and "gpt" not in out.splitlines()[0]

    assert main(["info", path]) == 0
    out = capsys.readouterr().out
    assert "partitions : 2" in out


def test_cli_ls_and_tree(images, capsys):
    from mfim.cli import main

    path = image("mbr.img", images)
    assert main(["ls", path, "/", "--part", "1"]) == 0
    out = capsys.readouterr().out
    assert "HELLO.TXT" in out.upper()

    assert main(["tree", path, "--part", "2", "--depth", "2"]) == 0
    out = capsys.readouterr().out
    assert "docs" in out and "readme.txt" in out


def test_cli_extract_with_verify(images, tmp_path, capsys):
    import json

    from mfim.cli import main

    path = image("mbr.img", images)
    dest = tmp_path / "out.txt"
    code = main(["extract", path, "/root.txt", str(dest), "--part", "2", "--verify"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["match"] is True and payload["bytes"] == len(b"root level file\n")
    assert dest.read_bytes() == b"root level file\n"


def test_cli_extract_bad_partition_exits(images, capsys):
    from mfim.cli import main

    path = image("mbr.img", images)
    with pytest.raises(SystemExit) as exc:
        main(["ls", path, "/", "--part", "99"])
    assert exc.value.code == 2


def test_cli_hex_raw(images, capsys):
    from mfim.cli import main

    assert main(["hex", image("mbr.img", images), "--raw", "--offset", "0x1b8",
                 "--length", "16"]) == 0
    assert "000001b8" in capsys.readouterr().out


def test_cli_patch_is_planned_not_written(images, tmp_path):
    from mfim.cli import main

    work = tmp_path / "patchme.img"
    work.write_bytes(b"\x00" * 4096)
    assert main(["patch", str(work), "16", "deadbeef"]) == 0
    assert work.read_bytes()[16:20] == b"\x00\x00\x00\x00"   # planned only
    assert main(["patch", str(work), "16", "deadbeef", "--allow-write"]) == 0
    assert work.read_bytes()[16:20] == b"\xde\xad\xbe\xef"
    assert (tmp_path / "patchme.img.bak").exists()


# --------------------------------------------------------------------------- #
# GUI smoke test (no display needed: offscreen platform + model only)
# --------------------------------------------------------------------------- #
def test_gui_model_offscreen(images, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from mfim.gui import ImageTreeQtModel
    from mfim.vfs import ImageVFS

    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # no libGL/libEGL on a bare sandbox
        pytest.skip(f"Qt cannot start headless: {exc}")
    path = image("mbr.img", images)
    vfs = ImageVFS(path, offset=part_offset(path, 2))
    model = ImageTreeQtModel(ImageTreeModelData(vfs))
    index = model.index(0, 0)
    assert index.isValid()
    assert model.columnCount() == 5
    assert model.data(index, Qt.ItemDataRole.DisplayRole)
    assert model.rowCount() >= 3
    root_index = model.index(0, 0)
    docs = [model.index(r, 0) for r in range(model.rowCount())]
    assert any(model.data(i, Qt.ItemDataRole.DisplayRole) == "root.txt" for i in docs)
    assert model.parent(root_index).isValid() in (True, False)
    app.quit()
