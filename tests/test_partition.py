# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the partition table parser: extended MBR chains, GPT hardening.

Run:  python -m pytest tests/test_partition.py -v
  or:  python tests/test_partition.py
"""

import os
import struct
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile.partition import SECTOR_SIZE, list_partitions

IMG_SECTORS = 128 * 1024  # 64 MB is plenty for these tables


def _blank_image(tmpdir, name="disk.img"):
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.truncate(IMG_SECTORS * SECTOR_SIZE)
    return path


def _put_sector(path, lba, data):
    with open(path, "r+b") as f:
        f.seek(lba * SECTOR_SIZE)
        f.write(data)


def _mbr_entry(sector, slot, ptype, start, size, boot=False):
    off = 446 + 16 * slot
    sector[off] = 0x80 if boot else 0x00
    sector[off + 4] = ptype
    struct.pack_into("<II", sector, off + 8, start, size)


def _mbr(entries):
    s = bytearray(SECTOR_SIZE)
    for slot, (ptype, start, size) in enumerate(entries):
        _mbr_entry(s, slot, ptype, start, size)
    s[510:512] = b"\x55\xaa"
    return bytes(s)


def _ebr(logical=None, nxt=None):
    s = bytearray(SECTOR_SIZE)
    if logical:
        _mbr_entry(s, 0, logical[0], logical[1], logical[2])
    if nxt:
        _mbr_entry(s, 1, 0x0F, nxt, 4096)  # second slot: next EBR (relative)
    s[510:512] = b"\x55\xaa"
    return bytes(s)


class TestExtendedMBR:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_single_logical_volume(self):
        path = _blank_image(self.tmp)
        _put_sector(path, 0, _mbr([(0x0B, 2048, 2048), (0x0F, 4096, 8192)]))
        _put_sector(path, 4096, _ebr(logical=(0x83, 63, 1024)))

        parts = list_partitions(path)
        assert [p.type for p in parts] == [0x0B, 0x0F, 0x83]
        logical = parts[-1]
        assert logical.start_lba == 4096 + 63  # EBR-relative offset
        assert logical.offset == (4096 + 63) * SECTOR_SIZE
        assert logical.size_bytes == 1024 * SECTOR_SIZE
        assert logical.name == "Logical 3"
        assert logical.type_name == "Linux"

    def test_chained_logical_volumes(self):
        path = _blank_image(self.tmp)
        _put_sector(path, 0, _mbr([(0x0F, 2048, 65536)]))
        # EBR links are relative to the extended base: 2048 -> 6144 -> 14336.
        _put_sector(path, 2048, _ebr(logical=(0x0B, 63, 2048), nxt=4096))
        _put_sector(path, 2048 + 4096, _ebr(logical=(0x07, 63, 2048), nxt=12288))
        _put_sector(path, 2048 + 12288, _ebr(logical=(0x83, 63, 1024)))

        parts = list_partitions(path)
        assert [p.start_lba for p in parts] == [2048, 2111, 6207, 14399]
        assert [p.type for p in parts] == [0x0F, 0x0B, 0x07, 0x83]
        assert [p.name for p in parts] == [
            "Partition 1",
            "Logical 2",
            "Logical 3",
            "Logical 4",
        ]

    def test_chain_stops_on_broken_ebr(self):
        path = _blank_image(self.tmp)
        _put_sector(path, 0, _mbr([(0x0B, 2048, 1024), (0x05, 4096, 8192)]))
        broken = bytearray(_ebr(logical=(0x83, 63, 512), nxt=2048))
        broken[510:512] = b"\x00\x00"  # damaged signature
        _put_sector(path, 4096, bytes(broken))

        parts = list_partitions(path)
        assert [p.type for p in parts] == [0x0B, 0x05]  # walk ended safely

    def test_chain_stops_on_dead_end(self):
        path = _blank_image(self.tmp)
        _put_sector(path, 0, _mbr([(0x0F, 4096, 8192)]))
        # Second slot empty: the chain simply ends after one logical volume.
        _put_sector(path, 4096, _ebr(logical=(0x83, 63, 512), nxt=None))
        parts = list_partitions(path)
        assert [p.type for p in parts] == [0x0F, 0x83]

    def test_plain_mbr_is_unchanged(self):
        path = _blank_image(self.tmp)
        _put_sector(path, 0, _mbr([(0x0B, 2048, 2048), (0x83, 8192, 4096)]))
        parts = list_partitions(path)
        assert [p.type for p in parts] == [0x0B, 0x83]
        assert [p.name for p in parts] == ["Partition 1", "Partition 2"]


class TestGptRobustness:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def _gpt_image(self, path, num_entries, entry_size, name="RootFS"):
        _put_sector(path, 0, _mbr([(0xEE, 1, IMG_SECTORS - 1)]))
        hdr = bytearray(SECTOR_SIZE)
        hdr[0:8] = b"EFI PART"
        struct.pack_into("<Q", hdr, 72, 2)
        struct.pack_into("<I", hdr, 80, num_entries)
        struct.pack_into("<I", hdr, 84, entry_size)
        struct.pack_into("<I", hdr, 88, 128)
        _put_sector(path, 1, bytes(hdr))
        if entry_size < 128:
            return  # unusable entry size: nothing sensible to write
        ent = bytearray(entry_size)
        ent[0:16] = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le
        struct.pack_into("<Q", ent, 32, 2048)
        struct.pack_into("<Q", ent, 40, 2048 + 99999)
        ent[56 : 56 + len(name) * 2] = name.encode("utf-16-le")
        _put_sector(path, 2, bytes(ent))

    def test_plausible_gpt(self):
        path = _blank_image(self.tmp)
        self._gpt_image(path, 128, 128)
        parts = list_partitions(path)
        assert len(parts) == 1
        assert parts[0].name == "RootFS"
        assert parts[0].start_lba == 2048

    def test_absurd_entry_count_is_clamped(self):
        path = _blank_image(self.tmp)
        self._gpt_image(path, 0xFFFFFFFF, 128)
        parts = list_partitions(path)  # must return, not spin
        assert len(parts) == 1
        assert parts[0].name == "RootFS"

    def test_bad_entry_size_is_rejected(self):
        # With an unusable GPT the protective MBR entry is not returned
        # either: this is a GPT disk, and reporting "0xEE GPT protective" as a
        # mountable volume would be useless.
        path = _blank_image(self.tmp)
        self._gpt_image(path, 128, 7)
        assert list_partitions(path) == []

    def test_zero_entry_lba_is_rejected(self):
        path = _blank_image(self.tmp)
        self._gpt_image(path, 128, 128)
        with open(path, "r+b") as f:  # entry_lba 2 -> 0
            f.seek(SECTOR_SIZE + 72)
            f.write(struct.pack("<Q", 0))
        assert list_partitions(path) == []

    def test_partition_ending_at_or_beyond_gpt_image_eof_is_rejected(self):
        path = _blank_image(self.tmp)
        self._gpt_image(path, 128, 128)
        with open(path, "r+b") as f:
            f.seek(2 * SECTOR_SIZE + 40)
            f.write(struct.pack("<Q", IMG_SECTORS))
        assert list_partitions(path) == []

    def test_rejected_gpt_extent_does_not_consume_protective_sentinel(self):
        path = _blank_image(self.tmp)
        self._gpt_image(path, 128, 128)
        sentinel = b"outside-selected-volume"
        with open(path, "r+b") as f:
            f.seek(IMG_SECTORS * SECTOR_SIZE - len(sentinel))
            f.write(sentinel)
            f.seek(2 * SECTOR_SIZE + 40)
            f.write(struct.pack("<Q", IMG_SECTORS))
        assert list_partitions(path) == []
        with open(path, "rb") as f:
            f.seek(IMG_SECTORS * SECTOR_SIZE - len(sentinel))
            assert f.read(len(sentinel)) == sentinel


if __name__ == "__main__":
    for cls in (TestExtendedMBR, TestGptRobustness):
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                inst.setup_method()
                getattr(inst, name)()
                print("PASS", name)
    print("ALL TESTS PASSED")
