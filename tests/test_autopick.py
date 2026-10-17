# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for mountfile.autopick - automatic volume selection in raw images.

The cases reproduce the real failure mode: the partition table (or a udev
file) points at a container instead of a filesystem, so The Sleuth Kit answers

    FS_Info_Con: (tsk3.cpp:328) ... FAT32: invalid bytes-per-sector
    (not a FAT BPB?)

and the built-in FAT32 engine answers ``image too small``.

Run:  python -m pytest tests/test_autopick.py -v
"""

import json
import os
import struct
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile import autopick, cli
from mountfile.autopick import (Candidate, auto_select, find_udev_files,
                                format_candidates, format_choice,
                                looks_like_udev, scan_candidates, verify_mount)

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
FAT32_BYTES = TOTAL_SECTORS * BPS  # ~34 MB, a plausible small FAT32 volume


def _boot_sector(label=b"NO NAME    "):
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
    bpb[71:82] = label[:11].ljust(11)
    bpb[510:512] = b"\x55\xaa"
    return bytes(bpb)


def _fat32_at(f, offset):
    """Write a minimal but valid FAT32 volume (BPB + FAT) at `offset`."""
    f.seek(offset)
    f.write(_boot_sector())
    fat = bytearray(SPF * BPS)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)  # media byte 0xF8
    struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
    struct.pack_into("<I", fat, 8, 0x0FFFFFFF)
    f.seek(offset + RESERVED * BPS)
    f.write(fat)


def _image(path, size, fat32_at=None):
    with open(path, "wb") as f:
        f.truncate(size)
        if fat32_at is not None:
            _fat32_at(f, fat32_at)
    return path


def _put_mbr(path, ptype, start_lba, sectors):
    sector = bytearray(BPS)
    sector[446 + 4] = ptype
    struct.pack_into("<II", sector, 446 + 8, start_lba, sectors)
    sector[510:512] = b"\x55\xaa"
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(bytes(sector))


def _write_udev(path, lines):
    text = "[PARTITIONS]\r\n" + "\r\n".join(lines) + "\r\n"
    with open(path, "wb") as f:
        f.write(text.encode("latin-1"))
    return path


class TestValidate:
    """`_validate_at` must accept real filesystems and reject look-alikes."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_valid_fat32(self):
        path = _image(os.path.join(self.tmp, "fat.img"), FAT32_BYTES, 0)
        with open(path, "rb") as f:
            info = autopick._validate_at(f, 0)
        assert info is not None
        assert info[0] == "FAT32"
        assert info[2] == FAT32_BYTES

    def test_zeroed_offset_rejected(self):
        path = _image(os.path.join(self.tmp, "fat.img"), 0x400000, 0x100000)
        with open(path, "rb") as f:
            assert autopick._validate_at(f, 0x200000) is None

    def test_misaligned_garbage_rejected(self):
        """A boot sector copied 256 bytes off its alignment is not a volume."""
        path = _image(os.path.join(self.tmp, "fat.img"), 0x400000, 0x100000)
        with open(path, "r+b") as f:
            f.seek(0x100100)
            f.write(_boot_sector())
        with open(path, "rb") as f:
            assert autopick._validate_at(f, 0x100100) is None

    def test_verify_mount_reports_backend_or_error(self):
        """verify_mount is the check that catches FS_Info_Con before the GUI."""
        good = _image(os.path.join(self.tmp, "good.img"), FAT32_BYTES, 0x100000)
        backend, entries, error = verify_mount(good, 0x100000)
        assert backend in ("tsk", "fat32")
        assert error is None

        bad = _image(os.path.join(self.tmp, "bad.img"), 0x400000, 0x100000)
        backend, _entries, error = verify_mount(bad, 0x200000)
        assert backend is None
        assert error  # the Sleuth Kit / FAT32 message is preserved


class TestScan:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_signature_candidate(self):
        path = _image(os.path.join(self.tmp, "fat.img"), FAT32_BYTES, 0x100000)
        cands = scan_candidates(path, deep=False)
        assert cands
        assert cands[0].offset == 0x100000
        assert cands[0].fstype == "FAT32"
        assert cands[0].source == "signature"

    def test_inner_search_inside_partition(self):
        """A partition whose start is not a filesystem is searched inside."""
        path = _image(os.path.join(self.tmp, "dump.bin"), 0x400000, 0x180000)
        _put_mbr(path, 0x0B, 2048, 2048)  # partition @ 0x100000, no FS there

        cands = scan_candidates(path, scan_limit=0x100000, inner_window=0x200000)
        assert any(c.offset == 0x180000 and c.source == "inner" for c in cands)
        assert any(c.source == "partition" and c.fstype == "raw" for c in cands)

        best, _ = auto_select(path, scan_limit=0x100000, inner_window=0x200000)
        assert best is not None
        assert best.offset == 0x180000

    def test_udev_offsets_are_used_and_scored(self):
        path = _image(os.path.join(self.tmp, "dump.bin"), FAT32_BYTES + 0x200000)
        with open(path, "r+b") as f:
            _fat32_at(f, 0x100000)
        udev = _write_udev(os.path.join(self.tmp, "udev.txt"), ["0x100000,0x100000,system"])
        cands = scan_candidates(path, udev=udev, deep=False)
        top = cands[0]
        assert top.offset == 0x100000
        assert top.name == "system"  # merged from the udev entry
        assert any("udev" in r for r in top.reasons)


class TestUdevHelpers:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_looks_like_udev(self):
        udev = _write_udev(
            os.path.join(self.tmp, "udev.txt"), ["0x0,0x4000,preloader"]
        )
        assert looks_like_udev(udev)

    def test_binary_dump_is_not_udev(self):
        dump = _image(os.path.join(self.tmp, "dump.bin"), 0x200000, 0x100000)
        assert not looks_like_udev(dump)

    def test_find_udev_files_next_to_dump(self):
        dump = _image(os.path.join(self.tmp, "dump.bin"), 0x200000, 0x100000)
        udev = _write_udev(os.path.join(self.tmp, "udev.txt"), ["0x0,0x4000,lk"])
        found = find_udev_files(dump)
        assert found and found[0] == udev

    def test_find_udev_files_by_content(self):
        dump = _image(os.path.join(self.tmp, "dump.bin"), 0x200000, 0x100000)
        other = _write_udev(os.path.join(self.tmp, "layout.txt"), ["0x0,0x4000,lk"])
        assert other in find_udev_files(dump)


class TestAutoSelect:
    """The scenario from the screenshots: udev offsets that mount nothing."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.dump = _image(os.path.join(self.tmp, "dump.bin"), 0x3400000)
        with open(self.dump, "r+b") as f:
            _fat32_at(f, 0x180000)  # the only real filesystem
        self.udev = _write_udev(
            os.path.join(self.tmp, "udev.txt"),
            [
                "0x00100000,0x00100000,preloader",
                "0x00a5a000,0x00100000,system",
                "0x198f0000,0x00100000,user",
                "0x00e5a000,0x00100000,data",
            ],
        )

    def test_picks_the_real_filesystem(self):
        best, cands = auto_select(self.dump, udev=self.udev, verify=True)
        assert best is not None
        assert best.offset == 0x180000
        assert best.fstype == "FAT32"
        assert best.backend in ("tsk", "fat32")
        # every udev offset is present but none of them is a mountable volume
        raw = [c.offset for c in cands if c.fstype == "raw"]
        assert 0x00A5A000 in raw and 0x00E5A000 in raw
        assert 0x00100000 in raw

    def test_reported_offsets_do_not_mount(self):
        for offset in (0x00A5A000, 0x00E5A000, 0x00100000):
            backend, _entries, error = verify_mount(self.dump, offset)
            assert backend is None
            assert error

    def test_hint_matches_partition_name(self):
        with open(self.dump, "r+b") as f:
            _fat32_at(f, 0x100000)
        udev = _write_udev(
            os.path.join(self.tmp, "udev2.txt"), ["0x100000,0x00100000,system"]
        )
        best, _ = auto_select(self.dump, udev=udev, hint="system", verify=True)
        assert best.offset == 0x100000
        assert any("подсказк" in r for r in best.reasons)

    def test_no_filesystem_returns_none(self):
        empty = _image(os.path.join(self.tmp, "empty.bin"), 0x200000)
        best, cands = auto_select(empty, verify=True)
        assert best is None

    def test_deep_scan_can_be_disabled(self):
        best, cands = auto_select(self.dump, deep=False, verify=False)
        assert any(c.offset == 0x180000 for c in cands)
        assert best is not None


class TestFormat:
    def test_candidates_table(self):
        cand = Candidate(0x100000, "FAT32", source="signature", size=FAT32_BYTES)
        cand.add(30, "mount ok")
        text = format_candidates([cand])
        assert "0x00100000" in text
        assert "FAT32" in text
        assert "Тип" in text

    def test_empty_candidates(self):
        assert "не найдены" in format_candidates([])

    def test_choice_explains_itself(self):
        cand = Candidate(0x180000, "FAT32", source="inner")
        text = format_choice(cand)
        assert "Выбран том" in text
        assert "0x180000" in text.lower()

    def test_no_choice(self):
        assert "не найден" in format_choice(None)


class TestCli:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.image = _image(os.path.join(self.tmp, "fat.img"), FAT32_BYTES, 0x100000)

    def _args(self, **kw):
        base = dict(
            image=self.image,
            offset=None,
            volume=None,
            udev=None,
            hint=None,
            scan_limit=autopick.DEFAULT_SCAN_LIMIT,
            no_verify=False,
            json=False,
            extract=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_text_output(self, capsys):
        assert cli.cmd_autopick(self._args()) == 0
        out = capsys.readouterr().out
        assert "0x00100000" in out
        assert "Выбран том" in out

    def test_json_output(self, capsys):
        assert cli.cmd_autopick(self._args(json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["best"]["offset"] == 0x100000
        assert payload["best"]["fstype"] == "FAT32"
        assert payload["best"]["offset_hex"] == "0x00100000"
        assert payload["candidates"]

    def test_extract_after_pick(self, capsys):
        out_dir = os.path.join(self.tmp, "out")
        assert cli.cmd_autopick(self._args(extract=out_dir)) == 0
        assert "Extracted" in capsys.readouterr().out
        assert os.path.isdir(out_dir)

    def test_parser_accepts_autopick(self):
        parser = cli.build_parser()
        args = parser.parse_args(["autopick", self.image, "--json", "--hint", "system"])
        assert args.command == "autopick"
        assert args.json is True and args.hint == "system"
        assert "autopick" in cli.COMMANDS

    def test_parser_accepts_auto_flag(self):
        parser = cli.build_parser()
        args = parser.parse_args(["ls", self.image, "/", "--auto"])
        assert args.auto is True

    def test_select_uses_auto_flag(self):
        args = self._args(auto=True)
        offset, fstype, _desc = cli._select(self.image, args)
        assert offset == 0x100000
        assert fstype == "FAT32"


if __name__ == "__main__":
    print("run with: python -m pytest tests/test_autopick.py -v")
