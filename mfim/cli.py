"""Headless command line front-end: ``python -m mfim.cli <command>``.

The CLI is the reference user of the library — every feature of the GUI is
reachable from here, which is what makes the project testable on a machine
with no display (or no Qt at all).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from typing import Optional

from . import __version__
from .partition import (
    Partition,
    PartitionTable,
    parse_partition_table,
    scan_filesystems,
)
from .rawio import ImagePatcher, RawImage
from .util import human_size

PROG = "mfim"


def _int(value, default: int = 0) -> int:
    """Accept 0x1b8, '0x1b8', '440' or 440 — users type all of them."""
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- #
# partition resolution
# --------------------------------------------------------------------------- #
def load_table(image: str, scan: bool = True) -> PartitionTable:
    """Parse the table, and by default also name each filesystem by magic."""
    return scan_filesystems(image) if scan else parse_partition_table(image)


def resolve_partition(image: str, args) -> tuple:
    """Return (offset, size, partition_or_None, table)."""
    table = load_table(image)
    size = os.path.getsize(image)

    if getattr(args, "offset", None) is not None:
        off = _int(args.offset)
        return off, size - off, table.containing(off), table

    part_no = getattr(args, "part", None)
    if part_no is not None:
        p = table.by_index(int(part_no))
        if p is None:
            _eprint(f"error: partition #{part_no} not found "
                    f"(image has {len(table.partitions)}: "
                    f"{[x.index for x in table.partitions]})")
            sys.exit(2)
        return p.offset, p.size, p, table

    if table.partitions:
        p = table.partitions[0]
        return p.offset, p.size, p, table
    return 0, size, None, table


def open_vfs(image: str, args):
    from .vfs import ImageVFS, VfsError

    offset, _size, part, table = resolve_partition(image, args)
    try:
        vfs = ImageVFS(image, offset=offset)
    except VfsError as exc:
        _eprint(f"error: {exc}")
        label = f"#{part.index} ({part.fs_name})" if part else f"0x{offset:x}"
        _eprint(f"hint: selected region {label}; TSK cannot open this filesystem. "
                "Try --offset/--part, or check `mfim parts`.")
        sys.exit(3)
    return vfs, part, table


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_info(args) -> int:
    image = args.image
    if not os.path.exists(image):
        _eprint(f"error: {image} not found")
        return 2
    size = os.path.getsize(image)
    table = load_table(image)
    print(f"image      : {image}")
    print(f"size       : {size} bytes ({human_size(size)})")
    print(f"scheme     : {table.scheme} (sector {table.sector_size})")
    print(f"partitions : {len(table.partitions)}")
    for p in table.partitions:
        print(f"  #{p.index} {p.type_name or p.name} offset=0x{p.offset:x} "
              f"size={human_size(p.size)} fs={p.fs_name or '?'}")
    for w in table.warnings:
        print(f"warning    : {w}")
    if args.json:
        print(json.dumps(dataclasses.asdict(table), default=str, ensure_ascii=False, indent=2))
    return 0


def cmd_parts(args) -> int:
    table = load_table(args.image)
    if args.json:
        print(json.dumps([p.to_dict() for p in table.partitions],
                         ensure_ascii=False, indent=2))
    else:
        print(table.describe())
    return 0


def cmd_sniff(args) -> int:
    from .partition import sniff_filesystem

    size = os.path.getsize(args.image)
    print(f"offset 0x0            : {sniff_filesystem(args.image, 0)}")
    for p in load_table(args.image).partitions:
        print(f"offset 0x{p.offset:<10x} (#{p.index}) : {sniff_filesystem(args.image, p.offset)}")
    return 0


def cmd_ls(args) -> int:
    vfs, part, _ = open_vfs(args.image, args)
    path = args.path or "/"
    if part:
        print(f"# partition #{part.index} @ 0x{part.offset:x} ({part.fs_name})")
    entries = vfs.list_dir(path, include_virtual=args.virtual)
    total = 0
    for e in entries:
        kind = "d" if e.is_dir else "-"
        print(f"{kind} {e.size:>14} {e.inode:>8} {e.name}")
        total += e.size if not e.is_dir else 0
    print(f"-- {len(entries)} entries, {human_size(total)} in files")
    return 0


def cmd_tree(args) -> int:
    from .tree_model import ImageTreeModelData

    vfs, part, _ = open_vfs(args.image, args)
    data = ImageTreeModelData(vfs)
    root = data.root
    if args.path and args.path != "/":
        node = data.find(args.path)
        if node is None:
            _eprint(f"error: {args.path} not found")
            return 4
        root = node

    count = 0

    def render(node, prefix: str = "", depth: int = 0) -> None:
        nonlocal count
        children = data.children(node)
        for i, child in enumerate(children):
            last = i == len(children) - 1
            print(f"{prefix}{'└── ' if last else '├── '}{child.name}"
                  + ("/" if child.is_dir else f"  ({human_size(child.size)})"))
            count += 1
            if child.is_dir and depth < args.depth:
                render(child, prefix + ("    " if last else "│   "), depth + 1)

    print(root.path)
    render(root)
    print(f"-- {count} nodes (depth limit {args.depth})")
    return 0


def cmd_extract(args) -> int:
    vfs, part, _ = open_vfs(args.image, args)
    if args.dest:
        dest = args.dest
    else:
        dest = os.path.join(args.out_dir or ".", os.path.basename(args.src.rstrip("/")))
    if args.all:
        return _extract_all(vfs, args)
    res = vfs.extract(args.src, dest, verify=args.verify)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["match"] else 1


def _extract_all(vfs, args) -> int:
    out_dir = args.out_dir or "extracted"
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for entry in vfs.walk("/"):
        if entry.is_dir:
            continue
        safe = entry.path.lstrip("/").replace("..", "_")
        target = os.path.join(out_dir, safe)
        os.makedirs(os.path.dirname(target) or out_dir, exist_ok=True)
        vfs.extract(entry.path, target)
        n += 1
        if args.limit and n >= args.limit:
            break
    print(f"{n} files extracted to {out_dir}")
    return 0


def cmd_hex(args) -> int:
    if args.raw:
        img = RawImage(args.image)
        off = _int(args.offset)
        print(img.hexdump(off, args.length, base=off))
        return 0
    vfs, _, _ = open_vfs(args.image, args)
    print(vfs.hexdump(args.src or "/", _int(args.offset), args.length))
    return 0


def cmd_fatwrite(args) -> int:
    from .fatwrite import FatWriteBackend, FatWriteError, mtools_available

    if not mtools_available():
        _eprint("error: mtools is not installed — FAT writing unavailable")
        return 5
    offset, _size, part, _ = resolve_partition(args.image, args)
    if args.allow_write and not args.yes:
        _eprint("refusing to write without --yes (and --backup is strongly advised)")
        return 6
    try:
        backend = FatWriteBackend(args.image, offset=offset)
    except FatWriteError as exc:
        _eprint(f"error: {exc}")
        return 7
    try:
        print(backend.describe())
        if args.command == "put":
            backend.copy_in(args.local, args.dest)
            print(f"copied {args.local} -> {args.dest}")
        elif args.command == "get":
            data = backend.read_file_bytes(args.src)
            with open(args.local, "wb") as fp:
                fp.write(data)
            print(f"copied {args.src} -> {args.local} ({len(data)} bytes)")
        elif args.command == "mkdir":
            backend.mkdir(args.path)
            print(f"created {args.path}")
        elif args.command == "rm":
            backend.remove(args.path)
            print(f"removed {args.path}")
        elif args.command == "dir":
            print(backend.listdir(args.path))
    finally:
        backend.close()
    return 0


def cmd_patch(args) -> int:
    """Low-level: patch raw bytes at an absolute offset (transactional)."""
    data = bytes.fromhex(args.hex_bytes.replace(" ", ""))
    patcher = ImagePatcher(args.image, readonly=not args.allow_write)
    patcher.plan(_int(args.offset), data)
    if not args.allow_write:
        print("readonly mode: patch planned but not written:")
        print(f"  offset=0x{_int(args.offset):x} length={len(data)}")
        print("re-run with --allow-write to commit (a .bak copy is made automatically)")
        return 0
    report = patcher.commit(backup=not args.no_backup, verify=True)
    print(report.describe())
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Mount File — file manager over raw disk/partition images",
    )
    p.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_part_args(sp, with_offset: bool = True):
        """Partition selection flags. ``with_offset=False`` for subcommands that
        already use ``--offset`` for something else (hex uses it as a file offset)."""
        sp.add_argument("--part", type=int, help="partition number (1-based)")
        if with_offset:
            sp.add_argument("--offset", help="explicit byte offset (accepts 0x...)")

    sp = sub.add_parser("info", help="summary of image + partition table")
    sp.add_argument("image")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("parts", help="print partition table")
    sp.add_argument("image")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_parts)

    sp = sub.add_parser("sniff", help="magic-based filesystem detection")
    sp.add_argument("image")
    sp.set_defaults(func=cmd_sniff)

    sp = sub.add_parser("ls", help="list a directory inside the image")
    sp.add_argument("image")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--virtual", action="store_true",
                    help="also show TSK pseudo entries ($MBR, $FAT1, $OrphanFiles, ...)")
    add_part_args(sp)
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("tree", help="recursive tree listing")
    sp.add_argument("image")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--depth", type=int, default=3)
    add_part_args(sp)
    sp.set_defaults(func=cmd_tree)

    sp = sub.add_parser("extract", help="extract file(s) out of the image")
    sp.add_argument("image")
    sp.add_argument("src", nargs="?", default="/")
    sp.add_argument("dest", nargs="?")
    sp.add_argument("--all", action="store_true", help="extract the whole tree")
    sp.add_argument("--out-dir")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--verify", action="store_true")
    add_part_args(sp)
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("hex", help="hex dump (file inside image, or --raw image bytes)")
    sp.add_argument("image")
    sp.add_argument("src", nargs="?")
    sp.add_argument("--raw", action="store_true", help="read the image itself, not a file")
    sp.add_argument("--offset", default=0, help="byte offset inside the file (or image with --raw)")
    sp.add_argument("--length", type=int, default=256)
    add_part_args(sp, with_offset=False)
    sp.set_defaults(func=cmd_hex)

    sp = sub.add_parser("fatwrite", help="write into a FAT partition via mtools (opt-in)")
    sp.add_argument("command", choices=["dir", "put", "get", "mkdir", "rm"])
    sp.add_argument("image")
    sp.add_argument("--local")
    sp.add_argument("--src")
    sp.add_argument("--dest")
    sp.add_argument("--path")
    sp.add_argument("--allow-write", action="store_true")
    sp.add_argument("--yes", action="store_true")
    add_part_args(sp)
    sp.set_defaults(func=cmd_fatwrite)

    sp = sub.add_parser("patch", help="plan/commit a raw byte patch at an absolute offset")
    sp.add_argument("image")
    sp.add_argument("offset")
    sp.add_argument("hex_bytes")
    sp.add_argument("--allow-write", action="store_true")
    sp.add_argument("--no-backup", action="store_true")
    sp.set_defaults(func=cmd_patch)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        _eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
