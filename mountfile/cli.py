# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Command line interface for Mount File.

    python -m mountfile volumes dump.img
    python -m mountfile ls dump.img /
    python -m mountfile get dump.img /photos/holiday.jpg -o ./out
    python -m mountfile put dump.img ./hello.txt --dest /docs --yes
    python -m mountfile mkdir dump.img /docs/new
    python -m mountfile mv  dump.img /docs/old.txt new.txt
    python -m mountfile rm  dump.img /docs/old.txt --yes

Read commands use The Sleuth Kit when available and fall back to the built-in
FAT32 engine; write commands always use the built-in FAT32 engine. Commands
that destroy data (put, rm) refuse to run without --yes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__
from .extract import ExtractError, extract_path
from .fat32 import Fat32, Fat32Error, Fat32Provider
from .firmware import DEFAULT_SCAN_LIMIT, detect_volumes, probe_filesystem
from .vfs import TskError, TskImage

OK, ERROR, NEED_CONFIRM = 0, 1, 2

#: Subcommand names; main.py routes to this module when the executable is
#: called with one of them (works in the packaged binary too).
COMMANDS = ("volumes", "info", "ls", "get", "put", "mkdir", "mv", "rm")

_READ_ERRORS = (Fat32Error, TskError, ExtractError, OSError, ValueError)


# --------------------------------------------------------------- helpers
def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f GB" % n


def _parse_offset(text):
    try:
        return int(str(text), 0)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %r" % text)


def _abs_path(path):
    return "/" + str(path or "/").strip().strip("/")


def _split_dir(path):
    path = _abs_path(path)
    if path == "/":
        raise Fat32Error("that is the volume root")
    parent, _, name = path.rpartition("/")
    return (parent or "/"), name


def _select(image, args):
    """Resolve --offset / --volume / automatic choice to one volume."""
    if args.offset is not None:
        return args.offset, probe_filesystem(image, args.offset) or "unknown", \
            "offset 0x%X" % args.offset
    try:
        vols = detect_volumes(image, scan_limit=args.scan_limit)
    except OSError as e:
        raise Fat32Error(str(e))
    if args.volume is not None:
        if not 1 <= args.volume <= len(vols):
            raise Fat32Error("no volume %d (image has %d)"
                             % (args.volume, len(vols)))
        v = vols[args.volume - 1]
        return v.offset, v.fstype, v.desc
    for v in vols:
        if v.fstype != "raw":
            return v.offset, v.fstype, v.desc
    if vols:
        return vols[0].offset, vols[0].fstype, vols[0].desc
    return 0, probe_filesystem(image, 0) or "unknown", "whole image"


def _provider(image, offset):
    """Read-only provider: The Sleuth Kit if installed, else the FAT32 engine."""
    try:
        return TskImage(image, offset=offset), "tsk"
    except TskError:
        return Fat32Provider(image, offset=offset), "fat32"


def _open_write(image, offset, backup=False):
    """Open the image for writing and return an open (handle, Fat32) pair."""
    if backup:
        bak = image + ".bak"
        shutil.copy2(image, bak)
        print("Backup written: %s" % bak)
    fh = open(image, "r+b")
    try:
        fs = Fat32(fh, offset)
    except Exception:
        fh.close()
        raise
    return fh, fs


def _warn(path):
    print("(!) This writes into the image: %s\n"
          "    Keep a backup. Re-run the same command with --yes to confirm."
          % path)


# -------------------------------------------------------------- commands
def cmd_volumes(args):
    vols = detect_volumes(args.image, scan_limit=args.scan_limit)
    if not vols:
        print("No volumes detected: no partition table and no known "
              "filesystem signature in the first %d MB."
              % (args.scan_limit // (1024 * 1024)))
        return ERROR
    print("%-3s %-12s %-10s %s" % ("#", "offset", "type", "source"))
    for i, v in enumerate(vols, 1):
        print("%-3d 0x%08X   %-10s %s" % (i, v.offset, v.fstype, v.source))
    return OK


def cmd_info(args):
    size = os.path.getsize(args.image)
    print("Image:  %s" % args.image)
    print("Size:   %s (%d bytes)" % (_fmt_size(size), size))
    print("At 0:   %s" % (probe_filesystem(args.image, 0)
                          or "no filesystem signature"))
    try:
        vols = detect_volumes(args.image, scan_limit=args.scan_limit)
    except OSError as e:
        print("Volumes: cannot scan (%s)" % e)
        return ERROR
    print("Volumes: %d" % len(vols))
    for i, v in enumerate(vols, 1):
        print("  %d. 0x%08X  %-9s  %s" % (i, v.offset, v.fstype, v.desc))
    return OK


def cmd_ls(args):
    offset, fstype, desc = _select(args.image, args)
    provider, backend = _provider(args.image, offset)
    try:
        print("# %s  [%s, %s]" % (desc, fstype, backend))
        for item in provider.list_dir(_abs_path(args.path)):
            if item["is_dir"]:
                print("  %-10s  %s/" % ("<DIR>", item["name"]))
            else:
                print("  %-10s  %s" % (_fmt_size(item["size"]), item["name"]))
    finally:
        if hasattr(provider, "close"):
            provider.close()
    return OK


def cmd_get(args):
    offset, fstype, desc = _select(args.image, args)
    provider, backend = _provider(args.image, offset)
    out = args.output or "."
    try:
        src = _abs_path(args.path)
        count = extract_path(provider, src if args.path else "/", out)
        print("Extracted %d file(s) from %s [%s] to %s"
              % (count, src, fstype, out))
    finally:
        if hasattr(provider, "close"):
            provider.close()
    return OK


def cmd_put(args):
    if not args.yes:
        _warn(args.image)
        return NEED_CONFIRM
    offset, fstype, _ = _select(args.image, args)
    dest = _abs_path(args.dest)
    for src in args.local:
        if not os.path.isfile(src):
            raise Fat32Error("no such local file: %s" % src)
    fh, fs = _open_write(args.image, offset, backup=args.backup)
    try:
        for src in args.local:
            with open(src, "rb") as f:
                data = f.read()
            name = os.path.basename(src)
            fs.write_file(dest, name, data)
            print("Wrote %s (%d bytes) -> %s%s"
                  % (src, len(data), dest if dest != "/" else "/", name))
    finally:
        fh.close()
    return OK


def cmd_mkdir(args):
    offset, _, _ = _select(args.image, args)
    parent, name = _split_dir(args.path)
    fh, fs = _open_write(args.image, offset, backup=args.backup)
    try:
        fs.mkdir(parent, name)
        print("Created directory %s" % _abs_path(args.path))
    finally:
        fh.close()
    return OK


def cmd_mv(args):
    offset, _, _ = _select(args.image, args)
    fh, fs = _open_write(args.image, offset, backup=args.backup)
    try:
        fs.rename(_abs_path(args.path), args.new_name)
        print("Renamed %s -> %s" % (_abs_path(args.path), args.new_name))
    finally:
        fh.close()
    return OK


def cmd_rm(args):
    if not args.yes:
        _warn(args.image)
        return NEED_CONFIRM
    offset, _, _ = _select(args.image, args)
    fh, fs = _open_write(args.image, offset, backup=args.backup)
    try:
        entry = fs.delete(_abs_path(args.path), recursive=args.recursive)
        print("Deleted %s (%s, %d bytes)%s"
              % (_abs_path(args.path), "dir" if entry.is_dir else "file",
                 entry.size,
                 " with its contents" if args.recursive and entry.is_dir
                 else ""))
    finally:
        fh.close()
    return OK


# ----------------------------------------------------------------- parser
def _add_target(sp):
    sp.add_argument("image", help="raw disk image (.img/.bin/.dump/...)")
    sp.add_argument("-V", "--volume", type=int, default=None,
                    help="volume number as printed by `volumes` (1-based)")
    sp.add_argument("-O", "--offset", type=_parse_offset, default=None,
                    help="raw byte offset of the filesystem (e.g. 0x400000)")
    sp.add_argument("--scan-limit", type=_parse_offset,
                    default=DEFAULT_SCAN_LIMIT,
                    help="bytes scanned for filesystem signatures")
    sp.add_argument("--backup", action="store_true",
                    help="copy the image to <image>.bak before writing")


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m mountfile",
        description="Mount File - browse and edit raw disk images.")
    p.add_argument("--version", action="version",
                   version="Mount File %s" % __version__)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("volumes", help="list detected volumes")
    _add_target(sp)
    sp.set_defaults(func=cmd_volumes)

    sp = sub.add_parser("info", help="image summary")
    _add_target(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("ls", help="list a directory inside the image")
    _add_target(sp)
    sp.add_argument("path", nargs="?", default="/")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("get", help="extract a file or a directory")
    _add_target(sp)
    sp.add_argument("path", nargs="?", default="/")
    sp.add_argument("-o", "--output", default=".", help="host output directory")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("put", help="copy local files into the image (FAT32)")
    _add_target(sp)
    sp.add_argument("local", nargs="+", help="local file(s) to write")
    sp.add_argument("--dest", default="/", help="directory inside the image")
    sp.add_argument("--yes", action="store_true", help="confirm the write")
    sp.set_defaults(func=cmd_put)

    sp = sub.add_parser("mkdir", help="create a directory (FAT32)")
    _add_target(sp)
    sp.add_argument("path", help="directory to create, e.g. /docs/new")
    sp.set_defaults(func=cmd_mkdir)

    sp = sub.add_parser("mv", help="rename an entry (FAT32)")
    _add_target(sp)
    sp.add_argument("path", help="path of the file or directory to rename")
    sp.add_argument("new_name", help="new name inside the same directory")
    sp.set_defaults(func=cmd_mv)

    sp = sub.add_parser("rm", help="delete a file or directory (FAT32)")
    _add_target(sp)
    sp.add_argument("path")
    sp.add_argument("-r", "--recursive", action="store_true",
                    help="delete a non-empty directory with its contents")
    sp.add_argument("--yes", action="store_true", help="confirm the delete")
    sp.set_defaults(func=cmd_rm)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _READ_ERRORS as e:
        print("error: %s" % e, file=sys.stderr)
        return ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
