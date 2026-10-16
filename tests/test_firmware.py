# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for volume detection (mountfile.firmware).

Synthetic images only: small hand-built headers for the signature scan plus a
real FAT32 volume (from test_fat32) behind an MBR for the partition path.

Run:  python -m pytest tests/test_firmware.py -v
  or:  python tests/test_firmware.py
"""

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mountfile.firmware import detect_volumes, probe_filesystem
from test_fat32 import BPS, TOTAL_SECTORS, make_fat32_image


def fat32_boot_sector():
    """A valid FAT32 BPB (the header the scanner has to recognise)."""
    b = bytearray(512)
    b[0:3] = b"\xeb\x58\x90"
    b[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", b, 11, 512)
    b[13] = 1
    struct.pack_into("<H", b, 14, 32)
    b[16] = 1
    b[54:62] = b"FAT32   "
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def make_image(path, size):
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def write_at(path, offset, data):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)


class TestSignatureScan:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def _img(self, name, size=2 * 1024 * 1024):
        return make_image(os.path.join(self.tmp, name), size)

    def test_filesystem_at_offset_zero(self):
        path = self._img("unpartitioned.img")
        write_at(path, 0, fat32_boot_sector())
        vols = detect_volumes(path)
        assert len(vols) == 1
        assert vols[0].offset == 0
        assert vols[0].fstype == "FAT32"
        assert vols[0].source == "signature"

    def test_filesystem_not_at_offset_zero(self):
        path = self._img("embedded.img")
        write_at(path, 1024 * 1024, fat32_boot_sector())
        vols = detect_volumes(path)
        assert [(v.offset, v.fstype) for v in vols] == [(1024 * 1024, "FAT32")]

    def test_standard_fat32_label_at_offset_82(self):
        # A real FAT32 formatter uses EBPB offset 82 (54 is FAT12/16).
        header = bytearray(fat32_boot_sector())
        header[54:62] = b"\x00" * 8
        header[82:90] = b"FAT32   "
        path = self._img("standard-fat32.img")
        write_at(path, 0, header)
        assert [(v.offset, v.fstype) for v in detect_volumes(path)] == [(0, "FAT32")]
        path = self._img("embedded-standard-fat32.img")
        write_at(path, 1024 * 1024, header)
        assert [(v.offset, v.fstype) for v in detect_volumes(path)] == [
            (1024 * 1024, "FAT32")
        ]

    def test_lookalike_header_is_rejected(self):
        # The FAT32 label at the right place but no boot signature and no
        # plausible BPB: this is file content, not a volume.
        path = self._img("lookalike.img")
        junk = bytearray(2048)
        junk[54:62] = b"FAT32   "
        write_at(path, 0, bytes(junk))
        write_at(path, 512, bytes(junk))
        assert detect_volumes(path) == []

    def test_magic_inside_file_content_is_rejected(self):
        # A "CD001" at a place that is not an ISO9660 PVD offset.
        path = self._img("iso_fake.img")
        write_at(path, 3 * 512, b"...CD001...")
        assert detect_volumes(path) == []

    def test_ext_iso_ubifs_headers(self):
        ext = self._img("ext.img")
        write_at(ext, 1024 + 0x38, b"\x53\xef")
        assert probe_filesystem(ext) == "ext2/3/4"

        iso = self._img("iso.img", 64 * 1024)
        write_at(iso, 0x8001, b"CD001")
        assert probe_filesystem(iso) == "ISO9660"
        assert [v.fstype for v in detect_volumes(iso)] == ["ISO9660"]

        ubi = self._img("ubi.img", 64 * 1024)
        write_at(ubi, 0, b"\x31\x18\x10\x06")
        assert probe_filesystem(ubi) == "UBIFS"

    def test_empty_image_has_no_volumes(self):
        path = self._img("zeros.img")
        assert detect_volumes(path) == []
        assert probe_filesystem(path) is None


class TestPartitionVolumes:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_mbr_fat32_partition_is_reported(self):
        src = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(src)
        dst = os.path.join(self.tmp, "disk.img")

        mbr = bytearray(512)
        mbr[446 + 4] = 0x0B  # FAT32 (LBA)
        struct.pack_into("<I", mbr, 446 + 8, 2048)
        struct.pack_into("<I", mbr, 446 + 12, TOTAL_SECTORS)
        mbr[510:512] = b"\x55\xaa"

        with open(dst, "wb") as out:
            out.write(mbr)
            out.write(b"\x00" * (2048 * BPS - 512))
            with open(src, "rb") as f:
                out.write(f.read())

        vols = detect_volumes(dst)
        assert len(vols) == 1
        assert vols[0].offset == 2048 * BPS
        assert vols[0].fstype == "FAT32"
        assert vols[0].source == "table"
        assert vols[0].size == TOTAL_SECTORS * BPS
        assert "FAT32" in vols[0].desc

    def test_partition_with_unknown_filesystem(self):
        dst = os.path.join(self.tmp, "unknown.img")
        mbr = bytearray(512)
        mbr[446 + 4] = 0x83  # Linux
        struct.pack_into("<I", mbr, 446 + 8, 2048)
        struct.pack_into("<I", mbr, 446 + 12, 4096)
        mbr[510:512] = b"\x55\xaa"
        with open(dst, "wb") as f:
            f.truncate(2048 * BPS + 4096 * BPS)
            f.seek(0)
            f.write(mbr)

        vols = detect_volumes(dst)
        assert len(vols) == 1
        assert vols[0].fstype == "raw"
        assert "Linux" in vols[0].desc


if __name__ == "__main__":
    for cls in (TestSignatureScan, TestPartitionVolumes):
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                inst.setup_method()
                getattr(inst, name)()
                print("PASS", name)
    print("ALL TESTS PASSED")
