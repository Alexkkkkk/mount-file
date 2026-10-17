# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for RT809 dump decoding and udev partition splitting.

Run:  python -m pytest tests/test_rt809.py -v
  or:  python tests/test_rt809.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mountfile import rt809
from mountfile.rt809 import Part, Rt809Error, decode_dump, detect, format_parts_table, parse_udev


def make_encrypted_dump(path, kind="MTK", plain_check=True):
    """Build a synthetic encrypted dump of the shape the decoder expects."""
    buf = bytearray(rt809.BLOCK * 3)
    if kind == "MTK":
        buf[0x100:0x114] = rt809.MARKER_MTK
        buf[0xFC:0xFE] = rt809.FLAG_ENCODE
        xor_addr, start, end = rt809.MTK_XOR_ADDR, rt809.MTK_DATA_START, rt809.BLOCK
        check_at, check_data = rt809.MTK_PLAIN_CHECK
        head_plain = buf[0x114:end]  # placeholder payload
    else:
        # flag "JC" at 0x200090, XOR byte at 0x200092, marker at 0x200094
        buf[rt809.MST_FLAG_ADDR : rt809.MST_FLAG_ADDR + 2] = rt809.FLAG_ENCODE
        buf[rt809.MST_XOR_ADDR + 2 : rt809.MST_XOR_ADDR + 6] = rt809.MARKER_MST
        xor_addr = rt809.MST_XOR_ADDR
        start, end = rt809.MST_DATA_START, rt809.MST_REGION_END
        check_at, check_data = rt809.MST_FLIP_CHECK
        head_plain = buf[start:end]

    buf[xor_addr] = 0x5A
    if plain_check:
        buf[check_at : check_at + len(check_data)] = check_data

    table = bytearray(os.urandom(0x400))
    src = bytes(head_plain)
    # payload = plain XOR one-byte XOR table
    payload = bytes(b ^ 0x5A ^ table[i % len(table)] for i, b in enumerate(src))
    buf[start:end] = payload

    with open(path, "wb") as f:
        f.write(buf)
    return bytes(table)


class TestDetect:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_mtk_encrypted(self):
        path = os.path.join(self.tmp, "mtk.bin")
        make_encrypted_dump(path, "MTK")
        assert detect(path) == ("MTK", True)

    def test_mtk_plain(self):
        path = os.path.join(self.tmp, "mtk.bin")
        make_encrypted_dump(path, "MTK")
        with open(path, "r+b") as f:
            f.seek(rt809.MTK_FLAG_ADDR)
            f.write(b"\x00\x00")
        assert detect(path) == ("MTK", False)

    def test_mst_encrypted(self):
        path = os.path.join(self.tmp, "mst.bin")
        make_encrypted_dump(path, "MST")
        assert detect(path) == ("MST", True)

    def test_unknown(self):
        path = os.path.join(self.tmp, "raw.bin")
        with open(path, "wb") as f:
            f.truncate(rt809.BLOCK)
        assert detect(path) == (None, False)


class TestDecode:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def test_mtk_roundtrip(self):
        path = os.path.join(self.tmp, "mtk.bin")
        table = make_encrypted_dump(path, "MTK")
        with open(os.path.join(self.tmp, "XOR_MTK_00.bin"), "wb") as f:
            f.write(table)

        out = decode_dump(path, table_dir=self.tmp)
        assert out == path + "_decode.bin"
        data = open(out, "rb").read()
        assert data[0x100:0x114] == rt809.MARKER_MTK
        assert data[rt809.MTK_FLAG_ADDR : rt809.MTK_FLAG_ADDR + 4] == b"\x00" * 4

    def test_mst_roundtrip(self):
        path = os.path.join(self.tmp, "mst.bin")
        table = make_encrypted_dump(path, "MST")
        with open(os.path.join(self.tmp, "XOR_MST_00.bin"), "wb") as f:
            f.write(table)

        out = decode_dump(path, table_dir=self.tmp)
        data = open(out, "rb").read()
        assert data[rt809.MST_FLAG_ADDR : rt809.MST_FLAG_ADDR + 4] == b"\x00" * 4
        # payload decoded back: equals the zero-filled placeholder region
        assert data[rt809.MST_DATA_START : rt809.MST_REGION_END] == b"\x00" * (
            rt809.MST_REGION_END - rt809.MST_DATA_START
        )

    def test_not_encrypted_returns_none(self):
        path = os.path.join(self.tmp, "mtk.bin")
        make_encrypted_dump(path, "MTK")
        with open(path, "r+b") as f:
            f.seek(rt809.MTK_FLAG_ADDR)
            f.write(b"\x00\x00")
        assert decode_dump(path, table_dir=self.tmp) is None

    def test_unknown_dump_raises(self):
        path = os.path.join(self.tmp, "raw.bin")
        with open(path, "wb") as f:
            f.truncate(1024)
        try:
            decode_dump(path, table_dir=self.tmp)
        except Rt809Error:
            pass
        else:
            raise AssertionError("Rt809Error expected")


UDEV_TEXT = (
    "show = 0\r\n"
    "\r\n"
    "[PARTITIONS]\r\n"
    "0x0,0x4000,preloader\r\n"
    "0x4000,0x8000,lk\r\n"
    "0x100000,0x1000000,system\r\n"
    "\r\n"
    "[SOMETHING]\r\n"
    "1,2,ignored\r\n"
)


class TestUdev:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def _udev(self, text=UDEV_TEXT, name="udev.txt"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(text.encode("latin-1"))
        return path

    def test_parse_windows_eol(self):
        parts = parse_udev(self._udev())
        assert len(parts) == 3
        assert (parts[0].offset, parts[0].length, parts[0].name) == (
            0,
            0x4000,
            "preloader",
        )
        assert parts[2].name == "system"

    def test_parse_unix_eol(self):
        parts = parse_udev(self._udev(UDEV_TEXT.replace("\r\n", "\n")))
        assert len(parts) == 3

    def test_parse_mac_eol(self):
        parts = parse_udev(self._udev(UDEV_TEXT.replace("\r\n", "\r")))
        assert len(parts) == 3

    def test_decimal_values(self):
        text = UDEV_TEXT.replace("0x0,0x4000", "0,16384")
        parts = parse_udev(self._udev(text))
        assert parts[0].offset == 0 and parts[0].length == 16384

    def test_no_section_raises(self):
        try:
            parse_udev(self._udev("no sections here\n"))
        except Rt809Error:
            pass
        else:
            raise AssertionError("Rt809Error expected")

    def test_split(self):
        dump = os.path.join(self.tmp, "dump.bin")
        with open(dump, "wb") as f:
            f.write(os.urandom(0x200000))
        out_dir = os.path.join(self.tmp, "out")
        parts, written = rt809.split_by_udev(dump, self._udev(), out_dir)
        assert len(written) == 3
        sizes = [os.path.getsize(p) for p in written]
        assert sizes == [0x4000, 0x8000, 0x100000]  # last clamped to EOF
        base = os.path.basename(written[2])
        assert base == "USER_0x0000100000_0x00100000_system.bin"

    def test_part_filename(self):
        assert Part("lk", 0x4000, 0x8000).file_name() == (
            "USER_0x0000004000_0x00008000_lk.bin"
        )


class TestFormatPartsTable:
    """The table the GUI prints before asking for confirmation."""

    def setup_method(self):
        self.tmp = tempfile.mkdtemp(prefix="rt809_table_")

    def test_header_and_rows(self):
        parts = [Part("preloader", 0, 0x4000), Part("lk", 0x4000, 0x8000)]
        lines = format_parts_table(parts).splitlines()
        assert len(lines) == 3
        assert "Offset" in lines[0] and "Length" in lines[0] and "Name" in lines[0]
        # header and rows share the same column layout
        assert lines[0].index("Offset") == lines[1].index("0x")
        assert lines[1].split()[:3] == ["1", "0x00000000", "0x00004000"]
        assert lines[1].endswith("preloader")
        assert lines[2].split()[:3] == ["2", "0x00004000", "0x00008000"]
        assert lines[2].endswith("lk")

    def test_empty(self):
        assert len(format_parts_table([]).splitlines()) == 1  # header only

    def test_numbering_starts_at_one(self):
        lines = format_parts_table([Part("boot", 0, 0x1000)]).splitlines()
        assert lines[1].startswith("1 ")

    def test_cli_rows_match(self, capsys):
        """The `split` command prints the same rows as the GUI table."""
        from types import SimpleNamespace

        from mountfile import cli

        dump = os.path.join(self.tmp, "dump.bin")
        with open(dump, "wb") as f:
            f.write(bytes(0x200000))
        udev = os.path.join(self.tmp, "udev.txt")
        with open(udev, "wb") as f:
            # CRLF built from byte values: byte-identical file on Linux and Windows.
            # A text-mode write of os.linesep produced CR CR LF on Windows, the
            # udev parser then stopped at the empty line and found no partitions
            # (CI: "No partitions parsed from ...udev.txt" on windows-latest).
            crlf = bytes([13, 10])
            f.write(
                b"[PARTITIONS]"
                + crlf
                + b"0x00000000,0x00004000,preloader"
                + crlf
                + b"0x00010000,0x00100000,system"
                + crlf
            )
        args = SimpleNamespace(
            dump=dump, udev=udev, output=os.path.join(self.tmp, "out")
        )
        assert cli.cmd_split(args) == getattr(cli, "OK", 0)
        printed = capsys.readouterr().out
        for row in format_parts_table(parse_udev(udev)).splitlines()[1:]:
            assert row in printed


if __name__ == "__main__":
    tc = TestDetect()
    for meth in sorted(n for n in dir(tc) if n.startswith("test_")):
        tc.setup_method()
        getattr(tc, meth)()
    tc = TestDecode()
    for meth in sorted(n for n in dir(tc) if n.startswith("test_")):
        tc.setup_method()
        getattr(tc, meth)()
    tc = TestUdev()
    for meth in sorted(n for n in dir(tc) if n.startswith("test_")):
        tc.setup_method()
        getattr(tc, meth)()
    tc = TestFormatPartsTable()
    for meth in sorted(n for n in dir(tc) if n.startswith("test_")):
        if meth == "test_cli_rows_match":
            continue  # needs the pytest capsys fixture
        tc.setup_method()
        getattr(tc, meth)()
    print("all rt809 tests passed")
