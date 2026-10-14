"""Tests for the FAT32 engine and partition parsers (synthetic images).

Run:  python -m pytest tests/ -v
  or:  python tests/test_fat32.py
"""

import os
import struct
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.partition import list_partitions
from mountfile.fat32 import Fat32

BPS, SPC, RESERVED, NFATS, ROOTC = 512, 1, 32, 1, 2
TOTAL_SECTORS = 70000


def _spf():
    spf = 600
    while True:
        clusters = TOTAL_SECTORS - (RESERVED + NFATS * spf)
        if (clusters + 2) * 4 <= spf * BPS and clusters >= 65525:
            return spf
        spf += 1


SPF = _spf()


def make_fat32_image(path):
    with open(path, "wb") as f:
        f.truncate(TOTAL_SECTORS * BPS)
        bpb = bytearray(BPS)
        bpb[0:3] = b"\xeb\x58\x90"
        bpb[3:11] = b"MSWIN4.1"
        struct.pack_into("<H", bpb, 11, BPS)
        bpb[13] = SPC
        struct.pack_into("<H", bpb, 14, RESERVED)
        bpb[16] = NFATS
        struct.pack_into("<I", bpb, 32, TOTAL_SECTORS)
        struct.pack_into("<I", bpb, 36, SPF)
        struct.pack_into("<I", bpb, 44, ROOTC)
        bpb[64] = 0x80
        bpb[510:512] = b"\x55\xaa"
        f.seek(0)
        f.write(bpb)
        fat = bytearray(SPF * BPS)
        struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
        struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
        struct.pack_into("<I", fat, 8, 0x0FFFFFFF)  # root = EOC
        f.seek(RESERVED * BPS)
        f.write(fat)


class TestFat32:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(self.path)
        self.fh = open(self.path, "r+b")
        self.fs = Fat32(self.fh)

    def teardown_method(self):
        self.fh.close()

    def _free(self):
        return sum(1 for n in range(2, self.fs.fat_size // 4)
                   if self.fs._fat_get(n) == 0)

    def test_short_name_roundtrip(self):
        self.fs.write_file("/", "HELLO.TXT", b"Hello, FAT32 world!")
        assert self.fs.read_file("/HELLO.TXT") == b"Hello, FAT32 world!"

    def test_lfn_multicluster(self):
        payload = bytes((i * 7 + 3) % 256 for i in range(5000))
        name = "My Very Long File Name 12345.bin"
        self.fs.write_file("/", name, payload)
        assert self.fs.read_file("/" + name) == payload

    def test_overwrite_smaller_and_bigger(self):
        payload = os.urandom(5000)
        name = "data.bin"
        self.fs.write_file("/", name, payload)
        self.fs.write_file("/", name, b"tiny")
        assert self.fs.read_file("/" + name) == b"tiny"
        self.fs.write_file("/", name, payload + b"X" * 2000)
        assert self.fs.read_file("/" + name) == payload + b"X" * 2000

    def test_mkdir_nested(self):
        self.fs.mkdir("/", "CONFIG")
        self.fs.write_file("/CONFIG", "settings.ini", b"[main]\nkey=value\n")
        assert self.fs.read_file("/CONFIG/settings.ini") == \
            b"[main]\nkey=value\n"

    def test_many_files_no_leak(self):
        for i in range(40):
            self.fs.write_file("/", "file_%03d.dat" % i,
                               ("content %d" % i).encode())
        for i in range(40):
            assert self.fs.read_file("/file_%03d.dat" % i) == \
                ("content %d" % i).encode()
        self.fs.write_file("/", "temp.tmp", os.urandom(1000))
        base, dirlen = self._free(), len(
            self.fs.cluster_chain(self.fs.root_cluster))
        for _ in range(20):
            self.fs.write_file("/", "temp.tmp", os.urandom(1000))
        assert self._free() == base
        assert len(self.fs.cluster_chain(self.fs.root_cluster)) == dirlen
        names = [e.name for e in self.fs.list_dir() if not e.is_dir]
        assert names.count("temp.tmp") == 1

    def test_empty_file(self):
        self.fs.write_file("/", "empty.bin", b"")
        assert self.fs.read_file("/empty.bin") == b""


class TestPartitions:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_mbr(self):
        path = os.path.join(self.tmp, "mbr.img")
        with open(path, "wb") as f:
            f.truncate(50 * 1024 * 1024)
            mbr = bytearray(512)
            mbr[446 + 4] = 0x0B
            struct.pack_into("<I", mbr, 446 + 8, 2048)
            struct.pack_into("<I", mbr, 446 + 12, 60000)
            mbr[510:512] = b"\x55\xaa"
            f.seek(0)
            f.write(mbr)
        parts = list_partitions(path)
        assert len(parts) == 1
        assert parts[0].type_name == "FAT32"
        assert parts[0].offset == 2048 * 512

    def test_gpt(self):
        path = os.path.join(self.tmp, "gpt.img")
        with open(path, "wb") as f:
            f.truncate(80 * 1024 * 1024)
            pmbr = bytearray(512)
            pmbr[446 + 4] = 0xEE
            struct.pack_into("<I", pmbr, 446 + 8, 1)
            struct.pack_into("<I", pmbr, 446 + 12, 0xFF)
            pmbr[510:512] = b"\x55\xaa"
            f.seek(0)
            f.write(pmbr)
            hdr = bytearray(512)
            hdr[:8] = b"EFI PART"
            struct.pack_into("<Q", hdr, 72, 2)
            struct.pack_into("<I", hdr, 80, 128)
            struct.pack_into("<I", hdr, 84, 128)
            f.seek(512)
            f.write(hdr)
            ent = bytearray(128)
            g = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")
            ent[0:16] = g.bytes_le
            struct.pack_into("<Q", ent, 32, 2048)
            struct.pack_into("<Q", ent, 40, 2048 + 99999)
            name16 = "RootFS".encode("utf-16-le")
            ent[56:56 + len(name16)] = name16
            f.seek(2 * 512)
            f.write(ent)
        parts = list_partitions(path)
        assert len(parts) == 1
        assert parts[0].name == "RootFS"
        assert parts[0].offset == 2048 * 512


if __name__ == "__main__":
    t = TestFat32()
    for m in dir(t):
        if m.startswith("test_"):
            t.setup_method()
            getattr(t, m)()
            t.teardown_method()
            print("PASS", m)
    p = TestPartitions()
    for m in dir(p):
        if m.startswith("test_"):
            p.setup_method()
            getattr(p, m)()
            print("PASS", m)
    print("ALL TESTS PASSED")
