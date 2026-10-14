"""Classic ``xxd``-style hex dump, used by both the CLI and the GUI.

Kept dependency-free and pure so it can be unit-tested without Qt.
"""

from __future__ import annotations

from typing import Iterable, List

PRINTABLE = set(range(0x20, 0x7F))


def hexdump(data: bytes, base: int = 0, width: int = 16,
            ascii_col: bool = True) -> str:
    """Render ``data`` as hex + ASCII columns with absolute offsets.

    >>> hexdump(b"AB", base=0x10)
    '00000010  41 42                                            AB'
    """
    lines: List[str] = []
    for i in range(0, len(data), width):
        row = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in row)
        hex_part = hex_part.ljust(width * 3 - 1)
        line = f"{base + i:08x}  {hex_part}"
        if ascii_col:
            text = "".join(chr(b) if b in PRINTABLE else "." for b in row)
            line += f"  {text}"
        lines.append(line.rstrip())
    return "\n".join(lines)


def hexdump_range(fp, offset: int, length: int, width: int = 16) -> str:
    fp.seek(offset)
    return hexdump(fp.read(length), base=offset, width=width)


def iter_hexdump(data: bytes, base: int = 0, width: int = 16) -> Iterable[str]:
    for i in range(0, len(data), width):
        yield from hexdump(data[i:i + width], base=base + i, width=width).splitlines()
