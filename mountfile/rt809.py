# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""RT809H dump helpers: MST/MTK decryption and UDEV partition splitting.

A faithful port of two RT809H programmer scripts:

* ``RT809 decode v11`` - eMMC dumps read by the RT809H from MStar (MST) and
  MediaTek (MTK) TV boards are encrypted: a plain-text header block is kept in
  the clear while the payload is obfuscated with a single XOR byte plus a
  cyclic XOR table shipped with the programmer.  The script detects the dump
  kind by a marker, decodes the head region and writes ``<dump>_decode.bin``
  with the rest of the image copied verbatim.
* ``extract parts from UDEV V3.2`` - Android/TV dumps carry a ``udev`` text
  file describing the partition layout (``offset,length,name`` lines under a
  ``[PARTITIONS]`` section).  The script splits the raw dump into one ``.bin``
  per partition.

Decoding details preserved from the reference scripts (they matter for
byte-exact output):

* MTK decodes the region ``0x114 .. 0x100000`` of the first 1 MiB block only;
  MST decodes ``0x2000A8 .. 0x300000`` (three blocks).
* The XOR byte lives at ``0xFE`` (MTK) / ``0x200092`` (MST); the "JC" encode
  flag at ``0xFC`` / ``0x200090`` is zeroed in the output.
* After decoding, a plain-text signature is checked (``"Start"`` at ``0x182``
  for MTK, 3 bytes at ``0x20405`` for MST); on mismatch 16-bit words are
  swapped in pairs.  The original MST flip loop bound (``sizeBlock/2*3``)
  is below its start index, i.e. a no-op - replicated here; the MTK flip
  covers the first block only, as in the script.
* XOR table cycles: 0xFFEEC bytes for MTK, 0xFFF59 for MST (the bundled MST
  table is exactly one cycle long).
"""

from __future__ import annotations

import os

__all__ = [
    "BLOCK",
    "Part",
    "Rt809Error",
    "decode_dump",
    "detect",
    "format_parts_table",
    "load_table",
    "parse_udev",
    "split_by_udev",
]

BLOCK = 0x100000
#: Blocks decoded in one pass (MTK uses 1, MST uses 3).
HEAD_BLOCKS = 3

MARKER_MTK = b"MTK/DTV/ROMCODE/MSDC"  # plain text at 0x100, 0x14 bytes
MARKER_MST = b"MBOT"  # plain text at 0x200094, 4 bytes
FLAG_ENCODE = b"JC"  # 2 bytes at 0xFC (MTK) / 0x200090 (MST)

MTK_FLAG_ADDR = 0xFC
MTK_XOR_ADDR = 0xFE
MTK_DATA_START = 0x114
MTK_REGION_END = BLOCK  # first block only, as in the reference script
MTK_PLAIN_CHECK = (0x182, b"Start")

MST_FLAG_ADDR = 0x200090
MST_XOR_ADDR = 0x200092
MST_DATA_START = 0x2000A8
MST_REGION_END = BLOCK * 3
MST_FLIP_CHECK = (0x20405, b"\xf0\x9f\xe5")  # 3 bytes, as in the script

MTK_TABLE_CYCLE = 0xFFEEC
MST_TABLE_CYCLE = 0xFFF59

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TABLES = {"MTK": "XOR_MTK_00.bin", "MST": "XOR_MST_00.bin"}

UDEV_MAX_TEXT = 10 * 1024 * 1024  # the script caps the udev file at 10 KiB

_COPY_CHUNK = 1 << 20


class Rt809Error(ValueError):
    """Raised for dumps/udev files that do not match the expected layout."""


class Part:
    """One partition entry parsed from a udev file."""

    __slots__ = ("length", "name", "offset")

    def __init__(self, name, offset, length):
        self.name = name
        self.offset = int(offset)
        self.length = int(length)

    def file_name(self):
        return "USER_0x%010X_0x%08X_%s.bin" % (self.offset, self.length, self.name)

    def __repr__(self):
        return "<Part 0x%X +0x%X %s>" % (self.offset, self.length, self.name)


# ------------------------------------------------------------------ detect
def _head(path, size):
    need = min(size, MST_REGION_END)
    with open(path, "rb") as f:
        return bytearray(f.read(need))


def detect(path):
    """Return ``(kind, encrypted)`` for a dump; kind is ``"MTK"``, ``"MST"``
    or ``None`` when no known marker is present."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        mtk_zone = f.read(0x114)
        if len(mtk_zone) >= 0x114 and mtk_zone[0x100:0x114] == MARKER_MTK:
            return "MTK", mtk_zone[MTK_FLAG_ADDR : MTK_FLAG_ADDR + 2] == FLAG_ENCODE
        f.seek(MST_FLAG_ADDR)
        mst_zone = f.read(8)
        if len(mst_zone) >= 8 and mst_zone[4:8] == MARKER_MST:
            return "MST", mst_zone[0:2] == FLAG_ENCODE
    return None, False


# --------------------------------------------------------------- decoding
def load_table(kind, table_dir=None):
    """Read the XOR table for ``kind`` (bundled by default)."""
    name = TABLES[kind]
    path = os.path.join(table_dir, name) if table_dir else os.path.join(DATA_DIR, name)
    with open(path, "rb") as f:
        return f.read()


def _xor_region(buf, start, end, xor_byte):
    """XOR ``buf[start:end]`` with one byte (translated in bulk)."""
    if end <= start:
        return
    table = bytes(b ^ xor_byte for b in range(256))
    buf[start:end] = bytes(buf[start:end]).translate(table)


def _xor_table(buf, start, end, table, cycle):
    """XOR ``buf[start:end]`` with the cyclic XOR table."""
    if end <= start:
        return
    t_len = min(len(table), cycle)
    if t_len <= 0:
        raise Rt809Error("XOR table is empty")
    n = end - start
    repeated = (bytes(table[:t_len]) * (n // t_len + 1))[:n]
    x = int.from_bytes(bytes(buf[start:end]), "big") ^ int.from_bytes(repeated, "big")
    buf[start:end] = x.to_bytes(n, "big")


def _flip_words(buf, start_word, end_word):
    """Swap neighbouring 16-bit words in pairs (word index = byte index / 2)."""
    for i in range(start_word, end_word - 1, 2):
        a, b = 2 * i, 2 * i + 2
        buf[a : a + 2], buf[b : b + 2] = buf[b : b + 2], buf[a : a + 2]


def _decode_head(buf, kind, table):
    """Decode the encrypted head region of ``buf`` in place."""
    if kind == "MTK":
        xor_addr, data_start, region_end, cycle = (
            MTK_XOR_ADDR,
            MTK_DATA_START,
            min(MTK_REGION_END, len(buf)),
            MTK_TABLE_CYCLE,
        )
        check_at, check_data = MTK_PLAIN_CHECK
        flip_end = BLOCK // 2  # first block only, as in the reference script
    else:
        xor_addr, data_start, region_end, cycle = (
            MST_XOR_ADDR,
            MST_DATA_START,
            min(MST_REGION_END, len(buf)),
            MST_TABLE_CYCLE,
        )
        check_at, check_data = MST_FLIP_CHECK
        # The original MST flip loop never runs (start index above its bound);
        # the full-range flip is what the author evidently intended.
        flip_end = len(buf) // 2

    xor_byte = buf[xor_addr]
    _xor_region(buf, data_start, region_end, xor_byte)
    _xor_table(buf, data_start, region_end, table, cycle)

    if buf[check_at : check_at + len(check_data)] != check_data:
        _flip_words(buf, data_start // 2, flip_end)

    flag = MTK_FLAG_ADDR if kind == "MTK" else MST_FLAG_ADDR
    buf[flag : flag + 4] = b"\x00\x00\x00\x00"  # clear the "JC" flag (write32)


def decode_dump(path, out_path=None, table_dir=None, kind=None):
    """Decode an RT809H-encrypted dump.

    Returns the output path, or ``None`` when the dump carries a known marker
    but is not encrypted (nothing to decode).  Raises :class:`Rt809Error` for
    dumps without a known marker.
    """
    detected, encrypted = detect(path)
    if detected is None:
        raise Rt809Error(
            "no MST ('MBOT') or MTK ('MTK/DTV/ROMCODE/MSDC') marker found: %s" % path
        )
    if kind and kind.upper() != detected:
        raise Rt809Error("dump kind is %s, not %s" % (detected, kind))
    if not encrypted:
        return None

    table = load_table(detected, table_dir)
    size = os.path.getsize(path)
    if out_path is None:
        out_path = path + "_decode.bin"

    head = _head(path, size)
    _decode_head(head, detected, table)

    with open(path, "rb") as src, open(out_path, "wb") as dst:
        dst.write(head)
        src.seek(len(head))
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
    return out_path


# ------------------------------------------------------------- presentation
def format_parts_table(parts):
    """Numbered ``# / Offset / Length / Name`` table, one line per partition.

    The rows use the same layout as the ``split`` command, so the GUI log
    and the CLI show identical partition lines.
    """
    lines = ["  № Offset      Length      Name"]
    for i, p in enumerate(parts, 1):
        lines.append("%-3d 0x%08X  0x%08X  %s" % (i, p.offset, p.length, p.name))
    return "\n".join(lines)


# -------------------------------------------------------------- udev split
def _parse_num(text):
    """Hex when 0x-prefixed, decimal otherwise (as in the script)."""
    text = text.strip()
    if text[:2].lower() == "0x":
        return int(text[2:], 16)
    return int(text, 10)


def parse_udev(path):
    """Parse a udev file: ``[PARTITIONS]`` section, ``offset,length,name`` lines.

    Windows (CRLF), Unix (LF) and Mac (CR) line endings are all accepted.
    Parsing stops at an empty line or end of file, like the reference script.
    """
    with open(path, "rb") as f:
        raw = f.read(UDEV_MAX_TEXT + 1)
    if len(raw) > UDEV_MAX_TEXT:
        raise Rt809Error("udev file is larger than %d bytes" % UDEV_MAX_TEXT)

    lines = raw.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[PARTITIONS]":
            start = i + 1
            break
    if start is None:
        raise Rt809Error("[PARTITIONS] section not found in %s" % path)

    parts = []
    for line in lines[start:]:
        if not line.strip():
            break  # empty line ends the section, as in the script
        pieces = line.split(",")
        if len(pieces) < 3:
            continue
        name = pieces[2] if len(pieces) == 3 else ",".join(pieces[2:])
        parts.append(Part(name.strip(), _parse_num(pieces[0]), _parse_num(pieces[1])))
    return parts


def _default_out_dir(dump):
    base = dump
    dot, slash = base.rfind("."), base.rfind(os.sep)
    if dot > slash:
        base = base[:dot]
    return base + "_/"


def split_by_udev(dump, udev_path, out_dir=None):
    """Split ``dump`` into one ``.bin`` per udev partition entry.

    Returns ``(parts, written_paths)``; lengths are clamped to the dump size
    (entries fully past the end are skipped), as in the reference script.
    """
    parts = parse_udev(udev_path)
    if out_dir is None:
        out_dir = _default_out_dir(dump)
    os.makedirs(out_dir, exist_ok=True)

    dump_size = os.path.getsize(dump)
    written = []
    with open(dump, "rb") as src:
        for part in parts:
            if part.offset >= dump_size:
                continue
            length = min(part.length, dump_size - part.offset)
            out_name = Part(part.name, part.offset, length).file_name()
            out_path = os.path.join(out_dir, out_name)
            src.seek(part.offset)
            remaining = length
            with open(out_path, "wb") as dst:
                while remaining > 0:
                    chunk = src.read(min(_COPY_CHUNK, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)
            written.append(out_path)
    return parts, written
