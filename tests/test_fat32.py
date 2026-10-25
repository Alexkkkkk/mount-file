# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the FAT32 engine and partition parsers (synthetic images).

Run:  python -m pytest tests/ -v
  or:  python tests/test_fat32.py
"""

import os
import struct
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.fat32 import Fat32, Fat32Error
from mountfile.partition import list_partitions

BPS, SPC, RESERVED, NFATS, ROOTC = 512, 1, 32, 1, 2
# A FAT32 volume has to declare an FSINFO sector and a backup boot sector;
# fsck.fat (dosfstools) reports their absence as a defect, so the fixture
# keeps them and stays clean for the external checker used by MF-WE.
FSINFO_SECTOR, BACKUP_BOOT_SECTOR = 1, 6
# The volume label lives in *two* places and fsck.fat compares them: the BPB
# field at offset 71 and an ATTR_VOLUME_ID entry in the root directory.
VOLUME_LABEL = b"MOUNTFILE  "
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
    """Build a small but structurally *complete* FAT32 volume.

    The image is synthetic, yet nothing a real FAT32 has is missing: the media
    descriptor (0xF8), the extended boot signature with a volume label and the
    ``FAT32`` type string, an FSINFO sector and a byte-identical backup boot
    sector at sector 6.  ``fsck.fat -n`` therefore exits 0, which matters
    because ``WriteEngine.verify()`` reports a real external checker result
    instead of silently treating the volume as clean.
    """
    data_clusters = TOTAL_SECTORS - (RESERVED + NFATS * SPF)
    free_clusters = data_clusters - 1  # cluster 2 is the (used) root directory
    with open(path, "wb") as f:
        f.truncate(TOTAL_SECTORS * BPS)
        bpb = bytearray(BPS)
        bpb[0:3] = b"\xeb\x58\x90"
        bpb[3:11] = b"MSWIN4.1"
        struct.pack_into("<H", bpb, 11, BPS)
        bpb[13] = SPC
        struct.pack_into("<H", bpb, 14, RESERVED)
        bpb[16] = NFATS
        bpb[21] = 0xF8  # media descriptor: fixed disk
        struct.pack_into("<I", bpb, 32, TOTAL_SECTORS)
        struct.pack_into("<I", bpb, 36, SPF)
        struct.pack_into("<I", bpb, 44, ROOTC)
        struct.pack_into("<H", bpb, 48, FSINFO_SECTOR)
        struct.pack_into("<H", bpb, 50, BACKUP_BOOT_SECTOR)
        bpb[64] = 0x80  # drive number
        bpb[66] = 0x29  # extended boot signature
        struct.pack_into("<I", bpb, 67, 0x20261018)  # volume id
        bpb[71:82] = VOLUME_LABEL  # volume label
        bpb[82:90] = b"FAT32   "  # filesystem type
        bpb[510:512] = b"\x55\xaa"
        f.seek(0)
        f.write(bpb)
        fsinfo = bytearray(BPS)
        struct.pack_into("<I", fsinfo, 0, 0x41615252)  # lead signature
        struct.pack_into("<I", fsinfo, 484, 0x61417272)  # structure signature
        struct.pack_into("<I", fsinfo, 488, free_clusters)
        struct.pack_into("<I", fsinfo, 492, 3)  # next free cluster
        struct.pack_into("<I", fsinfo, 508, 0xAA550000)  # trail signature
        f.seek(FSINFO_SECTOR * BPS)
        f.write(fsinfo)
        f.seek(BACKUP_BOOT_SECTOR * BPS)
        f.write(bpb)  # backup boot sector must be byte-identical
        fat = bytearray(SPF * BPS)
        struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
        struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
        struct.pack_into("<I", fat, 8, 0x0FFFFFFF)
        f.seek(RESERVED * BPS)
        f.write(fat)
        # The matching root-directory volume-label entry (attr 0x08).  A real
        # FAT32 always has one next to its BPB label, and without it fsck.fat
        # exits non-zero, which would make WriteEngine.verify() report the
        # synthetic fixture as damaged.
        label_entry = bytearray(32)
        label_entry[0:11] = VOLUME_LABEL
        label_entry[11] = 0x08  # ATTR_VOLUME_ID
        f.seek((RESERVED + NFATS * SPF) * BPS)
        f.write(label_entry)


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
        return sum(
            1 for n in range(2, self.fs.fat_size // 4) if self.fs._fat_get(n) == 0
        )

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

    def test_failed_overwrite_keeps_original_and_rolls_back_partial_allocation(self):
        self.fs.write_file("/", "protected.bin", b"original")
        target = next(e for e in self.fs.list_dir() if e.name == "protected.bin")

        # Leave exactly one free cluster, then ask for two. The allocator must
        # roll back its partial allocation without unlinking the old file.
        self.fs._write(self.fs.fat_start, b"\xff" * self.fs.fat_size)
        free_cluster = target.cluster + 1
        self.fs._fat_set(free_cluster, 0)
        free_before = self._free()

        with pytest.raises(Fat32Error, match="no free clusters left"):
            self.fs.write_file("/", "protected.bin", b"x" * (2 * self.fs.cluster_size))

        assert self.fs.read_file("/protected.bin") == b"original"
        assert self._free() == free_before == 1

    def test_cluster_allocation_scans_forward_once(self):
        original_fat_get = self.fs._fat_get
        reads = 0

        def counted_fat_get(cluster):
            nonlocal reads
            reads += 1
            return original_fat_get(cluster)

        self.fs._fat_get = counted_fat_get
        clusters = 128
        payload = b"x" * (clusters * self.fs.cluster_size)
        self.fs.write_file("/", "large.bin", payload)

        # Includes the chain walk after allocation; a repeated prefix scan
        # would require tens of thousands of extra reads for this small file.
        assert reads < clusters * 4 + 16
        assert self.fs.read_file("/large.bin") == payload

    def test_mkdir_nested(self):
        self.fs.mkdir("/", "CONFIG")
        self.fs.write_file("/CONFIG", "settings.ini", b"[main]\nkey=value\n")
        assert self.fs.read_file("/CONFIG/settings.ini") == b"[main]\nkey=value\n"

    def test_many_files_no_leak(self):
        for i in range(40):
            self.fs.write_file("/", "file_%03d.dat" % i, ("content %d" % i).encode())
        for i in range(40):
            assert (
                self.fs.read_file("/file_%03d.dat" % i) == ("content %d" % i).encode()
            )
        self.fs.write_file("/", "temp.tmp", os.urandom(1000))
        base = self._free()
        dirlen = len(self.fs.cluster_chain(self.fs.root_cluster))
        for _ in range(20):
            self.fs.write_file("/", "temp.tmp", os.urandom(1000))
        assert self._free() == base
        assert len(self.fs.cluster_chain(self.fs.root_cluster)) == dirlen
        names = [e.name for e in self.fs.list_dir() if not e.is_dir]
        assert names.count("temp.tmp") == 1

    def test_empty_file(self):
        self.fs.write_file("/", "empty.bin", b"")
        assert self.fs.read_file("/empty.bin") == b""

    def test_chain_rejects_cluster_outside_declared_volume(self):
        self.fs.write_file("/", "bounded.bin", b"bounded data")
        entry = self.fs.entry("/bounded.bin")[1]
        self.fs._fat_set(entry.cluster, self.fs.cluster_count + 2)
        with pytest.raises(Fat32Error, match="out-of-range cluster"):
            self.fs.read_file("/bounded.bin")

    def test_io_cannot_cross_bpb_extent_into_trailing_image_bytes(self):
        trailing = b"trailing evidence"
        self.fh.seek(0, 2)
        self.fh.write(trailing)
        with pytest.raises(Fat32Error, match="volume bounds"):
            self.fs._read(self.fs.volume_end, 1)
        with pytest.raises(Fat32Error, match="volume bounds"):
            self.fs._write(self.fs.volume_end, b"X")
        self.fh.seek(self.fs.volume_end)
        assert self.fh.read(len(trailing)) == trailing


def test_fat32_rejects_bpb_volume_beyond_physical_eof(tmp_path):
    image = tmp_path / "bpb-too-large.img"
    make_fat32_image(image)
    with open(image, "r+b") as handle:
        handle.seek(32)
        handle.write(struct.pack("<I", TOTAL_SECTORS + 1))
    with open(image, "rb") as handle, pytest.raises(
        Fat32Error, match="extends beyond image EOF"
    ):
        Fat32(handle)


def test_fat32_rejects_physically_truncated_volume(tmp_path):
    image = tmp_path / "truncated.img"
    make_fat32_image(image)
    with open(image, "r+b") as handle:
        handle.truncate(TOTAL_SECTORS * BPS - BPS)
    with open(image, "rb") as handle, pytest.raises(
        Fat32Error, match="extends beyond image EOF"
    ):
        Fat32(handle)


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
            ent[56 : 56 + len(name16)] = name16
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
