# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Command line interface for Mount File.

    python -m mountfile volumes dump.img
    python -m mountfile ls dump.img /
    python -m mountfile get dump.img /photos/holiday.jpg -o ./out
    python -m mountfile put dump.img ./hello.txt --dest /docs --yes
    python -m mountfile mkdir dump.img /docs/new --yes
    python -m mountfile mv  dump.img /docs/old.txt new.txt --yes
    python -m mountfile rm  dump.img /docs/old.txt --yes

Read commands use The Sleuth Kit when available and fall back to the built-in
FAT32 engine; write commands always use the built-in FAT32 engine. Commands
that modify the image (put, mkdir, mv, rm) refuse to run without --yes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

from . import __version__
from .autopick import auto_select, format_candidates, format_choice
from .dfir import detect_timestomp, grep_image, read_timeline_csv, sha256_file
from .dfir_slack import read_file_slack
from .dfir_timeline import export_timeline_csv
from .dfir_unallocated_scan import scan_unallocated
from .dfir_yara import YaraUnavailableError, scan_yara
from .live_diff import LiveDiffError, diff_images, to_markdown, write_markdown
from .search_content import SearchError, search_files, search_image
from .slack import SlackError, extract_slack
from .timeline import (
    DEFAULT_HASH_LIMIT,
    EVENT_TYPES,
    TimelineError,
    build_timeline,
    query_timeline,
)
from .timestomp import DEFAULT_SKEW, TimestompError, collect_timestomp
from .yara_scan import TARGETS, YaraScanError, scan_image
from .extract import ExtractError, extract_path
from .fat32 import Fat32, Fat32Error, Fat32Provider
from .firmware import DEFAULT_SCAN_LIMIT, detect_volumes, probe_filesystem
from .rt809 import Rt809Error, decode_dump, split_by_udev
from .vfs import TskError, TskImage

OK, ERROR, NEED_CONFIRM = 0, 1, 2

#: Subcommand names; main.py routes to this module when the executable is
#: called with one of them (works in the packaged binary too).
COMMANDS = (
    "volumes",
    "info",
    "ls",
    "autopick",
    "get",
    "put",
    "mkdir",
    "mv",
    "rm",
    "decode",
    "split",
    "grep",
    "hash",
    "timeline",
    "timeline-export",
    "timestomp",
    "yara",
    "unallocated-scan",
    "slack",
    "timeline-build",
    "timeline-query",
    "search",
    "yara-scan",
    "timestomp-scan",
    "slack-extract",
    "live-diff",

)

_READ_ERRORS = (Fat32Error, TskError, ExtractError, OSError, ValueError, Rt809Error)


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
    except ValueError as e:
        raise argparse.ArgumentTypeError("not a number: %r" % text) from e


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
        return (
            args.offset,
            probe_filesystem(image, args.offset) or "unknown",
            "offset 0x%X" % args.offset,
        )
    if getattr(args, "auto", False):
        # Automatic choice: partition table, udev offsets and filesystem
        # signatures are all tried, and the winner is confirmed by a real
        # mount, so a broken table cannot produce the TSK FS_Info_Con error.
        best, _candidates = auto_select(image, udev=getattr(args, "udev", None))
        if best is None:
            raise Fat32Error("no mountable volume found in %s (--auto)" % image)
        return best.offset, best.fstype, best.desc
    try:
        vols = detect_volumes(image, scan_limit=args.scan_limit)
    except OSError as e:
        raise Fat32Error(str(e))
    if args.volume is not None:
        if not 1 <= args.volume <= len(vols):
            raise Fat32Error("no volume %d (image has %d)" % (args.volume, len(vols)))
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
    print(
        "(!) This writes into the image: %s\n"
        "    Keep a backup. Re-run the same command with --yes to confirm." % path
    )


# -------------------------------------------------------------- commands
def cmd_volumes(args):
    vols = detect_volumes(args.image, scan_limit=args.scan_limit)
    if not vols:
        print(
            "No volumes detected: no partition table and no known "
            "filesystem signature in the first %d MB."
            % (args.scan_limit // (1024 * 1024))
        )
        return ERROR
    print("%-3s %-12s %-10s %s" % ("#", "offset", "type", "source"))
    for i, v in enumerate(vols, 1):
        print("%-3d 0x%08X   %-10s %s" % (i, v.offset, v.fstype, v.source))
    return OK


def cmd_info(args):
    size = os.path.getsize(args.image)
    print("Image:  %s" % args.image)
    print("Size:   %s (%d bytes)" % (_fmt_size(size), size))
    print("At 0:   %s" % (probe_filesystem(args.image, 0) or "no filesystem signature"))
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
        items = provider.list_dir(_abs_path(args.path))
    except (Fat32Error, TskError) as e:
        # A missing directory is an expected, user-level error: report it as a
        # one-line message (exit code 1) instead of a raw TSK backtrace.
        print(
            "error: cannot list '%s': %s" % (_abs_path(args.path), e), file=sys.stderr
        )
        return ERROR
    finally:
        if hasattr(provider, "close"):
            provider.close()
    print("# %s  [%s, %s]" % (desc, fstype, backend))
    for item in items:
        if item["is_dir"]:
            print("  %-10s  %s/" % ("<DIR>", item["name"]))
        else:
            print("  %-10s  %s" % (_fmt_size(item["size"]), item["name"]))
    return OK


def cmd_get(args):
    offset, fstype, desc = _select(args.image, args)
    provider, backend = _provider(args.image, offset)
    out = args.output or "."
    try:
        src = _abs_path(args.path)
        count = extract_path(provider, src if args.path else "/", out)
        print("Extracted %d file(s) from %s [%s] to %s" % (count, src, fstype, out))
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
            print(
                "Wrote %s (%d bytes) -> %s%s"
                % (src, len(data), dest if dest != "/" else "/", name)
            )
    finally:
        fh.close()
    return OK


def cmd_mkdir(args):
    if not args.yes:
        _warn(args.image)
        return NEED_CONFIRM
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
    if not args.yes:
        _warn(args.image)
        return NEED_CONFIRM
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
        print(
            "Deleted %s (%s, %d bytes)%s"
            % (
                _abs_path(args.path),
                "dir" if entry.is_dir else "file",
                entry.size,
                " with its contents" if args.recursive and entry.is_dir else "",
            )
        )
    finally:
        fh.close()
    return OK


def cmd_decode(args):
    """Decode an RT809H dump; writes <dump>_decode.bin next to it."""
    if not os.path.isfile(args.dump):
        raise Rt809Error("no such file: %s" % args.dump)
    out = decode_dump(args.dump, table_dir=args.table_dir)
    if out is None:
        print(
            "Dump carries an MST/MTK marker but is not encrypted - nothing to decode."
        )
        return OK
    print("Decoded dump written: %s" % out)
    return OK


def cmd_split(args):
    if not os.path.isfile(args.dump):
        raise Rt809Error("no such file: %s" % args.dump)
    parts, written = split_by_udev(args.dump, args.udev, args.output)
    if not parts:
        print("No partitions parsed from %s" % args.udev)
        return ERROR
    print("%-3s %-10s %-10s %s" % ("#", "offset", "length", "name"))
    for i, p in enumerate(parts, 1):
        print("%-3d 0x%08X  0x%08X  %s" % (i, p.offset, p.length, p.name))
    print(
        "\nWritten %d partition file(s) to %s"
        % (len(written), args.output or "<dump>_/")
    )
    return OK


def cmd_autopick(args):
    """`autopick`: choose the working volume of an image automatically.

    Prints every candidate with its score, the reason the winner was chosen,
    and optionally extracts the whole volume. Exit code 1 when no mountable
    filesystem was found.
    """
    if not os.path.isfile(args.image):
        raise Fat32Error("no such file: %s" % args.image)
    if args.udev and not os.path.isfile(args.udev):
        raise Rt809Error("no such udev file: %s" % args.udev)

    best, candidates = auto_select(
        args.image,
        udev=args.udev,
        hint=args.hint,
        scan_limit=args.scan_limit,
        verify=not args.no_verify,
    )

    if args.json:
        payload = {
            "image": args.image,
            "udev": args.udev,
            "best": (
                None
                if best is None
                else {
                    "offset": best.offset,
                    "offset_hex": "0x%08X" % best.offset,
                    "fstype": best.fstype,
                    "source": best.source,
                    "score": best.score,
                    "size": best.size,
                    "label": best.label,
                    "name": best.name,
                    "backend": best.backend,
                    "reasons": list(best.reasons),
                }
            ),
            "candidates": [
                {
                    "offset": c.offset,
                    "offset_hex": "0x%08X" % c.offset,
                    "fstype": c.fstype,
                    "source": c.source,
                    "score": c.score,
                    "backend": c.backend,
                    "error": c.error,
                    "name": c.name,
                    "label": c.label,
                }
                for c in candidates
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("Image: %s (%s)" % (args.image, _fmt_size(os.path.getsize(args.image))))
        if args.udev:
            print("udev:  %s" % args.udev)
        print(format_candidates(candidates))
        print()
        print(format_choice(best))

    if best is None:
        return ERROR

    if args.extract:
        provider, backend = _provider(args.image, best.offset)
        try:
            count = extract_path(provider, "/", args.extract)
        finally:
            if hasattr(provider, "close"):
                provider.close()
        print(
            "\nExtracted %d file(s) from 0x%08X [%s] to %s"
            % (count, best.offset, backend, args.extract)
        )
    return OK


def cmd_grep(args):
    """Search raw image bytes without extracting or modifying the image."""
    count = 0
    for hit in grep_image(
        args.image,
        args.pattern,
        regex=args.regex,
        encoding=args.encoding,
        context=args.context,
        max_hits=args.max_results,
    ):
        count += 1
        print("0x%X (+%d): %s" % (hit["offset"], hit["length"], hit["context"]))
    print("Matches: %d" % count)
    return OK if count else ERROR


def cmd_hash(args):
    print("SHA-256: %s  %s" % (sha256_file(args.image), args.image))
    return OK


def _timeline_records(args):
    if not args.csv:
        raise ValueError("--csv is required: filesystem-aware collection is not yet enabled")
    return list(read_timeline_csv(args.csv))


def cmd_timeline(args):
    records = _timeline_records(args)
    for record in records[: args.limit]:
        print(
            "%s\t%s\t%s\t%s\t%s"
            % (record.path, record.mtime or "", record.atime or "", record.ctime or "", record.sha256 or "")
        )
    print("Records: %d" % len(records))
    return OK


def cmd_timestomp(args):
    records = _timeline_records(args)
    findings = list(detect_timestomp(records, skew=args.skew))
    for record, reasons in findings[: args.limit]:
        print("%s\t%s" % (record.path, ", ".join(reasons)))
    print("Findings: %d" % len(findings))
    return OK if findings else ERROR


def cmd_timeline_export(args):
    records = _timeline_records(args)
    count = export_timeline_csv(records, args.output)
    print("Wrote %d normalized events to %s" % (count, args.output))
    return OK


def cmd_yara(args):
    try:
        hits = scan_yara(args.image, args.rules)
    except YaraUnavailableError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    except (OSError, TypeError, ValueError) as exc:
        print("error: YARA scan failed: %s" % exc, file=sys.stderr)
        return ERROR
    for hit in hits:
        print(json.dumps(hit, ensure_ascii=False))
    print("Matches: %d" % len(hits))
    return OK if hits else ERROR


def cmd_unallocated_scan(args):
    with open(args.ranges, encoding="utf-8") as stream:
        ranges = json.load(stream)
    hits = scan_unallocated(
        args.image,
        ranges,
        chunk_size=args.chunk_size,
        max_hits=args.max_results,
    )
    count = 0
    for hit in hits:
        print(json.dumps(hit.as_dict(), ensure_ascii=False))
        count += 1
    print("Matches: %d" % count)
    return OK if count else ERROR


def cmd_slack(args):
    """Extract only a caller-resolved final-cluster slack region."""
    result = read_file_slack(
        args.image,
        args.last_cluster_offset,
        args.file_size,
        args.cluster_size,
    )
    summary = {
        "offset": result.offset,
        "length": len(result.bytes),
        "has_slack": result.has_slack,
        "created": False,
    }
    if args.output is not None and result.has_slack:
        # Exclusive creation prevents accidental replacement of existing data.
        with open(args.output, "xb") as stream:
            stream.write(result.bytes)
        summary["created"] = True
        summary["output"] = args.output
    print(json.dumps(summary, ensure_ascii=False))
    return OK


# ----------------------------------------------------------------- parser
def _add_target(sp):
    sp.add_argument("image", help="raw disk image (.img/.bin/.dump/...)")
    sp.add_argument(
        "-V",
        "--volume",
        type=int,
        default=None,
        help="volume number as printed by `volumes` (1-based)",
    )
    sp.add_argument(
        "-O",
        "--offset",
        type=_parse_offset,
        default=None,
        help="raw byte offset of the filesystem (e.g. 0x400000)",
    )
    sp.add_argument(
        "--scan-limit",
        type=_parse_offset,
        default=DEFAULT_SCAN_LIMIT,
        help="bytes scanned for filesystem signatures",
    )
    sp.add_argument(
        "--auto",
        action="store_true",
        help="pick the working volume automatically (same as the `autopick` command)",
    )
    sp.add_argument(
        "--backup",
        action="store_true",
        help="copy the image to <image>.bak before writing",
    )



# ---------------------------------------------------------- issue #87 commands
def cmd_timeline_build(args):
    """Collect a filesystem-aware timeline into SQLite and/or CSV."""
    if not args.output and not args.csv:
        print("error: pass -o/--output and/or --csv", file=sys.stderr)
        return ERROR
    try:
        count, events = build_timeline(
            args.image,
            db_path=args.output,
            csv_path=args.csv,
            hash_limit=args.hash_limit,
        )
    except (TimelineError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    print("Collected %d events from %s" % (count, args.image))
    if args.output:
        print("SQLite: %s" % args.output)
    if args.csv:
        print("CSV:    %s" % args.csv)
    return OK if count else ERROR


def cmd_timeline_query(args):
    """Answer "what happened between X and Y" against a timeline database."""
    try:
        rows = query_timeline(
            args.db, start=args.start, end=args.end, event=args.event, limit=args.limit
        )
    except sqlite3.Error as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(
                "%s\t%s\t%s\t%s"
                % (row["timestamp"], row["event"], row["path"], row["sha256"] or "-")
            )
    print("Events: %d" % len(rows))
    return OK if rows else ERROR


def cmd_search(args):
    """Search file content (--files) or raw image bytes, read-only."""
    try:
        if args.files:
            hits = search_files(
                args.image,
                args.pattern,
                regex=args.regex,
                encoding=args.encoding,
                context=args.context,
                max_hits=args.max_results,
            )
        else:
            hits = search_image(
                args.image,
                args.pattern,
                regex=args.regex,
                encoding=args.encoding,
                context=args.context,
                max_hits=args.max_results,
            )
    except (SearchError, TimelineError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    if args.json:
        print(json.dumps([hit.as_dict() for hit in hits], ensure_ascii=False, indent=2))
    else:
        for hit in hits:
            label = hit.path or "0x%X" % hit.offset
            print("%s (+%d): %s" % (label, hit.length, hit.context))
    print("Matches: %d" % len(hits))
    return OK if hits else ERROR


def cmd_yara_scan(args):
    """Scan image files and/or unallocated space with YARA rules."""
    try:
        hits = scan_image(
            args.image,
            args.rules,
            targets=tuple(args.targets),
            max_file_size=args.max_file_size,
        )
    except (YaraScanError, YaraUnavailableError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    except (OSError, TypeError, ValueError) as exc:
        print("error: YARA scan failed: %s" % exc, file=sys.stderr)
        return ERROR
    for hit in hits:
        if args.json:
            print(json.dumps(hit.as_dict(), ensure_ascii=False))
        else:
            offset = "-" if hit.offset is None else "0x%X" % hit.offset
            print("%s\t%s\t%s\t%s" % (hit.rule, hit.path or "-", offset, hit.target))
    print("Matches: %d" % len(hits))
    return OK if hits else ERROR


def cmd_timestomp_scan(args):
    """Flag timestamp anomalies directly from an image (no CSV round-trip)."""
    try:
        findings = collect_timestomp(args.image, skew=args.skew)
    except (TimestompError, TimelineError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    if args.json:
        print(json.dumps([item.as_dict() for item in findings], ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print("%s\t%s" % (item.path, ", ".join(item.reasons)))
    print("Findings: %d" % len(findings))
    return OK if findings else ERROR


def cmd_slack_extract(args):
    """Extract the slack tail of every file into a directory."""
    try:
        regions = extract_slack(args.image, args.output, min_slack=args.min_slack)
    except (SlackError, TimelineError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    for region in regions[:20]:
        print("%s\toffset=0x%X\tlen=%d" % (region.path, region.offset, region.length))
    if len(regions) > 20:
        print("... and %d more" % (len(regions) - 20))
    print("Slack regions: %d -> %s" % (len(regions), args.output))
    return OK if regions else ERROR


def cmd_live_diff(args):
    """Diff two images (or one image before/after a write) as Markdown."""
    try:
        entries = diff_images(args.before, args.after)
    except (LiveDiffError, TimelineError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return ERROR
    if args.output:
        write_markdown(entries, args.output)
        print("Wrote %d diff entries to %s" % (len(entries), args.output))
    else:
        print(to_markdown(entries), end="")
    print("Diff entries: %d" % len(entries))
    return OK


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m mountfile",
        description="Mount File - browse and edit raw disk images.",
    )
    p.add_argument("--version", action="version", version="Mount File %s" % __version__)
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
    sp.add_argument("--yes", action="store_true", help="confirm the write")
    sp.set_defaults(func=cmd_mkdir)

    sp = sub.add_parser("mv", help="rename an entry (FAT32)")
    _add_target(sp)
    sp.add_argument("path", help="path of the file or directory to rename")
    sp.add_argument("new_name", help="new name inside the same directory")
    sp.add_argument("--yes", action="store_true", help="confirm the write")
    sp.set_defaults(func=cmd_mv)

    sp = sub.add_parser("rm", help="delete a file or directory (FAT32)")
    _add_target(sp)
    sp.add_argument("path")
    sp.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="delete a non-empty directory with its contents",
    )
    sp.add_argument("--yes", action="store_true", help="confirm the delete")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser(
        "decode",
        help="decode an RT809H-encrypted MST/MTK eMMC dump",
    )
    sp.add_argument("dump", help="encrypted dump read by the RT809H programmer")
    sp.add_argument(
        "--table-dir",
        default=None,
        help="directory with XOR_MST_00.bin / XOR_MTK_00.bin (bundled by default)",
    )
    sp.set_defaults(func=cmd_decode)

    sp = sub.add_parser(
        "split",
        help="split a dump into partitions by a udev file",
    )
    sp.add_argument("dump", help="raw dump to split")
    sp.add_argument("udev", help="udev file with a [PARTITIONS] section")
    sp.add_argument(
        "-o",
        "--output",
        default=None,
        help="output directory (default: <dump name>_/ next to the dump)",
    )
    sp.set_defaults(func=cmd_split)

    sp = sub.add_parser(
        "autopick",
        help="pick the working volume (offset + filesystem) automatically",
    )
    _add_target(sp)
    sp.add_argument(
        "-u",
        "--udev",
        default=None,
        help="udev file with a [PARTITIONS] section, if one is available",
    )
    sp.add_argument(
        "--hint",
        default=None,
        help="prefer the volume whose udev/partition name or label matches this",
    )
    sp.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the real mount check (faster, but a broken offset is possible)",
    )
    sp.add_argument("--json", action="store_true", help="print the result as JSON")
    sp.add_argument(
        "--extract",
        default=None,
        help="extract the whole chosen volume into this host directory",
    )
    sp.set_defaults(func=cmd_autopick)

    sp = sub.add_parser("grep", help="search raw image bytes read-only")
    sp.add_argument("image")
    sp.add_argument("pattern")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--encoding", choices=("utf-8", "utf-16le", "latin-1", "raw"), default="utf-8")
    sp.add_argument("--context", type=int, default=64)
    sp.add_argument("--max-results", type=int, default=1000)
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("hash", help="calculate a read-only SHA-256 identity")
    sp.add_argument("image")
    sp.set_defaults(func=cmd_hash)

    sp = sub.add_parser("timeline", help="print records from a timeline CSV")
    sp.add_argument("image", help="image label retained for a stable CLI shape")
    sp.add_argument("--csv", required=True, help="CSV with path and timestamp columns")
    sp.add_argument("--limit", type=int, default=1000)
    sp.set_defaults(func=cmd_timeline)

    sp = sub.add_parser("timestomp", help="flag conservative timestamp anomalies from CSV")
    sp.add_argument("image", help="image label retained for a stable CLI shape")
    sp.add_argument("--csv", required=True, help="CSV with path and timestamp columns")
    sp.add_argument("--skew", type=float, default=2.0)
    sp.add_argument("--limit", type=int, default=1000)
    sp.set_defaults(func=cmd_timestomp)

    sp = sub.add_parser(
        "timeline-export", help="normalize caller-supplied timeline CSV timestamps to UTC"
    )
    sp.add_argument("--csv", required=True, help="CSV with path and timestamp columns")
    sp.add_argument("--output", required=True, help="normalized CSV output path")
    sp.set_defaults(func=cmd_timeline_export)

    sp = sub.add_parser("yara", help="scan a file/image read-only with a YARA rule file")
    sp.add_argument("image")
    sp.add_argument("--rules", required=True, help="user-supplied YARA source file")
    sp.set_defaults(func=cmd_yara)

    sp = sub.add_parser(
        "unallocated-scan",
        help="scan explicitly supplied byte ranges for common file signatures",
    )
    sp.add_argument("image")
    sp.add_argument(
        "--ranges",
        required=True,
        help="JSON list of [offset, length] pairs or objects; allocation map is not discovered",
    )
    sp.add_argument("--chunk-size", type=int, default=1024 * 1024)
    sp.add_argument("--max-results", type=int, default=1000)
    sp.set_defaults(func=cmd_unallocated_scan)
    sp = sub.add_parser(
        "timeline-build", help="collect a filesystem-aware timeline into SQLite/CSV"
    )
    sp.add_argument("image")
    sp.add_argument("-o", "--output", default=None, help="SQLite database path")
    sp.add_argument("--csv", default=None, help="CSV output path")
    sp.add_argument(
        "--hash-limit",
        type=int,
        default=DEFAULT_HASH_LIMIT,
        help="skip SHA-256 for files above this size; 0 disables hashing",
    )
    sp.set_defaults(func=cmd_timeline_build)

    sp = sub.add_parser(
        "timeline-query", help="query a timeline database by time window and/or event type"
    )
    sp.add_argument("--db", required=True, help="SQLite timeline database")
    sp.add_argument("--start", default=None, help="inclusive lower bound (ISO-8601)")
    sp.add_argument("--end", default=None, help="inclusive upper bound (ISO-8601)")
    sp.add_argument("--event", choices=EVENT_TYPES, default=None)
    sp.add_argument("--limit", type=int, default=1000)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_timeline_query)

    sp = sub.add_parser(
        "search", help="search file content or raw image bytes read-only"
    )
    sp.add_argument("image")
    sp.add_argument("pattern")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--encoding", default="utf-8", help="utf-8 (default) or utf-16le")
    sp.add_argument("--context", type=int, default=32)
    sp.add_argument("--max-results", type=int, default=1000)
    sp.add_argument(
        "--files",
        action="store_true",
        help="search file contents through the filesystem instead of raw bytes",
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser(
        "yara-scan", help="scan files and/or unallocated space with YARA rules"
    )
    sp.add_argument("image")
    sp.add_argument("--rules", required=True, help="rule file or directory of rules")
    sp.add_argument("--targets", nargs="+", choices=TARGETS, default=list(TARGETS))
    sp.add_argument("--max-file-size", type=int, default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_yara_scan)

    sp = sub.add_parser(
        "timestomp-scan", help="flag timestamp anomalies directly from an image"
    )
    sp.add_argument("image")
    sp.add_argument("--skew", type=float, default=DEFAULT_SKEW)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_timestomp_scan)

    sp = sub.add_parser("slack-extract", help="extract file slack space into a directory")
    sp.add_argument("image")
    sp.add_argument("-o", "--output", required=True, help="output directory")
    sp.add_argument("--min-slack", type=int, default=1)
    sp.set_defaults(func=cmd_slack_extract)

    sp = sub.add_parser(
        "slack",
        help="extract final-cluster slack from an explicitly resolved file extent",
    )
    sp.add_argument("image", help="raw disk image (opened read-only)")
    sp.add_argument(
        "--slack-only",
        action="store_true",
        required=True,
        help="explicitly select slack-only extraction; no filesystem path resolution is performed",
    )
    sp.add_argument("--last-cluster-offset", required=True, type=_parse_offset)
    sp.add_argument("--file-size", required=True, type=_parse_offset)
    sp.add_argument("--cluster-size", required=True, type=_parse_offset)
    sp.add_argument(
        "-o",
        "--output",
        help="create a new file for extracted slack (never overwrite)",
    )
    sp.set_defaults(func=cmd_slack)

    sp = sub.add_parser(
        "live-diff", help="diff two images (or one image before/after) into Markdown"
    )
    sp.add_argument("before")
    sp.add_argument("after")
    sp.add_argument("-o", "--output", default=None, help="Markdown path (stdout when omitted)")
    sp.set_defaults(func=cmd_live_diff)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _READ_ERRORS as e:
        print("error: %s" % e, file=sys.stderr)
        return ERROR
    except KeyboardInterrupt:
        # Ctrl+C is a normal way to abort a long scan: exit cleanly instead of
        # dumping a multi-frame traceback from deep inside pytsk3.
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
