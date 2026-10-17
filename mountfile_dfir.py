#!/usr/bin/env python3
"""
mountfile_dfir.py — Stage 1 DFIR toolkit for raw disk images (EPIC #87).

Tasks implemented:
  1.1 timeline      — unified timestamp database (SQLite + CSV)
  1.2 grep          — content search (plain / UTF-16LE / regex)
  1.3 yara          — YARA malware scanning
  1.4 timestomp     — timestamp manipulation detector
  1.5 unallocated   — unallocated block extraction
  1.6 slack         — file slack space extraction

Requirements:
  pip install pytsk3
  pip install yara-python    # optional, for task 1.3

Examples:
  python mountfile_dfir.py timeline  disk.img -o tl.db --csv tl.csv
  python mountfile_dfir.py grep      disk.img "password=" --unicode
  python mountfile_dfir.py grep      disk.img "passw(or)?d" --regex
  python mountfile_dfir.py yara      disk.img rules.yar
  python mountfile_dfir.py timestomp disk.img
  python mountfile_dfir.py unallocated disk.img -o unalloc.bin -v
  python mountfile_dfir.py slack     disk.img -o ./slack_out/
"""

import argparse
import csv
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

try:
    import pytsk3
except ImportError:
    print("Error: pytsk3 required (pip install pytsk3)", file=sys.stderr)
    sys.exit(1)

try:
    import yara as _yara
    HAS_YARA = True
except ImportError:
    _yara = None
    HAS_YARA = False


# ============================================================================
# Common helpers
# ============================================================================

FS_TYPE_MAP = {
    pytsk3.TSK_FS_TYPE_NTFS: "NTFS",
    pytsk3.TSK_FS_TYPE_EXT2: "ext2",
    pytsk3.TSK_FS_TYPE_EXT3: "ext3",
    pytsk3.TSK_FS_TYPE_EXT4: "ext4",
    pytsk3.TSK_FS_TYPE_FAT12: "FAT12",
    pytsk3.TSK_FS_TYPE_FAT16: "FAT16",
    pytsk3.TSK_FS_TYPE_FAT32: "FAT32",
    pytsk3.TSK_FS_TYPE_ISO9660: "ISO9660",
    pytsk3.TSK_FS_TYPE_HFS: "HFS",
}


def to_iso(ts: Optional[int]) -> Optional[str]:
    """Convert unix timestamp to ISO-8601 UTC (None if unset)."""
    if ts is None or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def open_image(path: Path):
    """Open image and mount filesystem."""
    img = pytsk3.Img_Info(str(path))
    fs = pytsk3.FS_Info(img)
    fs_name = FS_TYPE_MAP.get(fs.info.ftype, f"unknown({fs.info.ftype})")
    return img, fs, fs_name


def walk_fs(fs, path: str = "/") -> Iterator[Tuple[pytsk3.File, str]]:
    """Recursively yield (entry, full_path) for every fs object."""
    try:
        directory = fs.open_dir(path)
    except Exception:
        return
    for entry in directory:
        if not hasattr(entry, "info") or entry.info.name is None:
            continue
        try:
            name = entry.info.name.name.decode("utf-8", errors="replace")
        except Exception:
            continue
        if name in (".", ".."):
            continue
        full_path = f"{path.rstrip('/')}/{name}"
        yield entry, full_path
        if entry.info.meta and entry.info.meta.type == pytsk3.TSK_FS_META_TYPE_DIR:
            yield from walk_fs(fs, full_path)


# ============================================================================
# Task 1.1 — Timeline
# ============================================================================

def cmd_timeline(args):
    img, fs, fs_name = open_image(args.image)
    print(f"[*] FS: {fs_name}", file=sys.stderr)

    conn = sqlite3.connect(args.output)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            source TEXT,
            path TEXT,
            event TEXT,
            sha256 TEXT,
            details TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ts ON timeline(timestamp)")
    conn.commit()

    csv_file = open(args.csv, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["timestamp", "source", "path", "event", "sha256", "details"])

    count = 0
    for entry, full_path in walk_fs(fs):
        meta = entry.info.meta
        if not meta:
            continue

        sha256 = None
        if meta.type == pytsk3.TSK_FS_META_TYPE_REG and meta.size and meta.size < 100 * 1024 * 1024:
            try:
                data = entry.read_random(0, meta.size)
                if data:
                    sha256 = hashlib.sha256(data).hexdigest()
            except Exception:
                pass

        ts_map = {
            "mtime": meta.mtime,
            "atime": meta.atime,
            "ctime": meta.ctime,
            "crtime": getattr(meta, "crtime", None),
        }
        if hasattr(meta, "fn"):
            ts_map.update({
                "fn_mtime": meta.fn.mtime,
                "fn_atime": meta.fn.atime,
                "fn_ctime": meta.fn.ctime,
                "fn_crtime": meta.fn.crtime,
            })

        for event, ts in ts_map.items():
            iso = to_iso(ts)
            if not iso:
                continue
            details = f"type={meta.type}, size={meta.size}"
            cur.execute(
                "INSERT INTO timeline (timestamp, source, path, event, sha256, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (iso, fs_name, full_path, event, sha256, details),
            )
            writer.writerow([iso, fs_name, full_path, event, sha256 or "", details])
            count += 1

    conn.commit()
    conn.close()
    csv_file.close()
    print(f"[*] timeline events: {count}")
    print(f"[*] SQLite: {args.output}")
    print(f"[*] CSV:    {args.csv}")


# ============================================================================
# Task 1.2 — Content search (grep)
# ============================================================================

CHUNK = 8 * 1024 * 1024
OVERLAP = 4096


def _grep_chunks(file_path: Path) -> Iterator[Tuple[int, bytes]]:
    with open(file_path, "rb") as f:
        offset = 0
        prev_tail = b""
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            data = prev_tail + chunk
            yield offset - len(prev_tail), data
            prev_tail = chunk[-OVERLAP:]
            offset += len(chunk)


def cmd_grep(args):
    if args.unicode:
        flags = 0 if args.regex else re.IGNORECASE
        pat = args.pattern if args.regex else re.escape(args.pattern)
        compiled = re.compile(pat, flags | re.UNICODE)
    else:
        flags = 0 if args.regex else re.IGNORECASE
        pat_bytes = args.pattern.encode("utf-8")
        pat = pat_bytes if args.regex else re.escape(pat_bytes)
        compiled = re.compile(pat, flags)

    count = 0
    for base_offset, data in _grep_chunks(args.image):
        if args.unicode:
            text = data.decode("utf-16-le", errors="ignore")
            for m in compiled.finditer(text):
                byte_off = base_offset + m.start() * 2
                print(f"\n--- match #{count + 1} @ 0x{byte_off:X} ---")
                print(f"match: {m.group()!r}")
                count += 1
                if args.max_results and count >= args.max_results:
                    break
        else:
            for m in compiled.finditer(data):
                byte_off = base_offset + m.start()
                print(f"\n--- match #{count + 1} @ 0x{byte_off:X} ---")
                print(f"match: {m.group()!r}")
                count += 1
                if args.max_results and count >= args.max_results:
                    break
        if args.max_results and count >= args.max_results:
            break

    print(f"\n[*] total matches: {count}", file=sys.stderr)


# ============================================================================
# Task 1.3 — YARA scan
# ============================================================================

def cmd_yara(args):
    if not HAS_YARA:
        print("Error: yara-python required (pip install yara-python)", file=sys.stderr)
        return
    if not args.rules.exists():
        print(f"Error: rules file {args.rules} not found", file=sys.stderr)
        return

    try:
        rules = _yara.compile(filepath=str(args.rules))
    except _yara.YaraSyntaxError as e:
        print(f"YARA syntax error: {e}", file=sys.stderr)
        return

    img, fs, fs_name = open_image(args.image)
    print(f"[*] FS: {fs_name}, rules: {args.rules}", file=sys.stderr)

    total, scanned = 0, 0
    for entry, full_path in walk_fs(fs):
        meta = entry.info.meta
        if not meta or meta.type != pytsk3.TSK_FS_META_TYPE_REG:
            continue
        size = meta.size
        if not size or size <= 0:
            continue
        if args.max_size and size > args.max_size:
            continue
        try:
            data = entry.read_random(0, size)
            if not data:
                continue
            matches = rules.match(data=data)
            scanned += 1
            if matches:
                total += len(matches)
                for m in matches:
                    print(f"\n[!] {full_path}")
                    print(f"    rule: {m.rule}")
                    for tag in m.tags:
                        print(f"    tag: {tag}")
        except Exception:
            continue

    print(f"\n[*] scanned files: {scanned}, matched rules: {total}")


# ============================================================================
# Task 1.4 — Timestomp detector
# ============================================================================

def cmd_timestomp(args):
    img, fs, fs_name = open_image(args.image)
    print(f"[*] FS: {fs_name}", file=sys.stderr)
    now = datetime.now(timezone.utc)
    suspicious = 0

    for entry, full_path in walk_fs(fs):
        meta = entry.info.meta
        if not meta:
            continue
        reasons = []

        # rule 1: timestamps in the future
        for name, ts in [("mtime", meta.mtime), ("atime", meta.atime),
                         ("ctime", meta.ctime), ("crtime", getattr(meta, "crtime", None))]:
            iso = to_iso(ts)
            if iso:
                try:
                    if datetime.fromisoformat(iso) > now:
                        reasons.append(f"{name} in future: {iso}")
                except Exception:
                    pass

        # rule 2: mtime == atime == ctime
        vals = [meta.mtime, meta.atime, meta.ctime]
        if all(vals) and len(set(vals)) == 1:
            reasons.append("mtime == atime == ctime")

        # rule 3: SI.mtime < FN.mtime (NTFS-specific)
        if hasattr(meta, "fn") and meta.mtime and meta.fn.mtime:
            if meta.mtime < meta.fn.mtime:
                reasons.append(
                    f"SI.mtime ({to_iso(meta.mtime)}) < FN.mtime ({to_iso(meta.fn.mtime)})"
                )

        if reasons:
            suspicious += 1
            print(f"\n[!] {full_path}")
            for r in reasons:
                print(f"    - {r}")

    print(f"\n[*] suspicious files: {suspicious}")


# ============================================================================
# Task 1.5 — Unallocated space
# ============================================================================

def cmd_unallocated(args):
    img, fs, fs_name = open_image(args.image)
    print(f"[*] FS: {fs_name}", file=sys.stderr)

    block_size = fs.info.block_size or 4096
    last_block = getattr(fs.info, "last_block", None) or getattr(fs.info, "block_count", None)
    if last_block is None:
        print("Error: cannot determine last_block", file=sys.stderr)
        return

    out = open(args.output, "wb") if args.output else None
    total_unalloc, total_bytes = 0, 0

    block_get = getattr(fs, "block_get", None)
    if block_get is None:
        print("[!] pytsk3 lacks block_get — dumping ALL blocks as raw fallback",
              file=sys.stderr)
        for block_idx in range(last_block + 1):
            try:
                data = img.read(block_idx * block_size, block_size)
            except Exception:
                continue
            if data:
                total_bytes += len(data)
                if out:
                    out.write(data)
        print(f"[*] raw blocks written: {total_bytes} bytes")
        if out:
            out.close()
        return

    for block_idx in range(last_block + 1):
        try:
            block = block_get(block_idx)
        except Exception:
            continue
        if block is None or getattr(block, "alloc", True):
            continue
        buf = getattr(block, "buf", None)
        if buf is None:
            continue
        data = bytes(buf)
        if data.strip(b"\x00") == b"":
            continue
        total_unalloc += 1
        total_bytes += len(data)
        if out:
            out.write(data)
        if args.verbose:
            print(f"  block {block_idx} @ 0x{block_idx * block_size:X} ({len(data)} bytes)")

    if out:
        out.close()
        print(f"[*] output: {args.output}")
    print(f"[*] unallocated blocks: {total_unalloc}")
    print(f"[*] total bytes:        {total_bytes}")


# ============================================================================
# Task 1.6 — Slack space
# ============================================================================

def cmd_slack(args):
    img, fs, fs_name = open_image(args.image)
    print(f"[*] FS: {fs_name}", file=sys.stderr)

    block_size = fs.info.block_size or 4096
    out_dir = args.output_dir
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    count, total_slack = 0, 0
    for entry, full_path in walk_fs(fs):
        meta = entry.info.meta
        if not meta or meta.type != pytsk3.TSK_FS_META_TYPE_REG:
            continue
        size = meta.size
        if not size or size <= 0:
            continue

        alloc_size = ((size + block_size - 1) // block_size) * block_size
        if alloc_size <= size:
            continue

        try:
            data = entry.read_random(0, alloc_size)
        except Exception:
            continue
        if not data or len(data) <= size:
            continue

        slack = data[size:]
        if slack.strip(b"\x00") == b"":
            continue

        count += 1
        total_slack += len(slack)
        if out_dir:
            safe = full_path.strip("/").replace("/", "_").replace("\\", "_") or f"file_{count}"
            (out_dir / f"{safe}.slack").write_bytes(slack)

        if args.verbose or count <= 20:
            preview = slack[:64].hex()
            print(f"[+] {full_path} (size={size}, slack={len(slack)}): {preview}...")

    print(f"\n[*] files with non-empty slack: {count}")
    print(f"[*] total slack bytes:         {total_slack}")
    if out_dir:
        print(f"[*] saved to: {out_dir}")


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    p = argparse.ArgumentParser(
        description="mountfile DFIR toolkit — Stage 1 (EPIC #87)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("timeline", help="Task 1.1: unified timeline")
    t.add_argument("image", type=Path)
    t.add_argument("-o", "--output", type=Path, default=Path("timeline.db"))
    t.add_argument("--csv", type=Path, default=Path("timeline.csv"))
    t.set_defaults(func=cmd_timeline)

    g = sub.add_parser("grep", help="Task 1.2: content search")
    g.add_argument("image", type=Path)
    g.add_argument("pattern")
    g.add_argument("--unicode", action="store_true", help="UTF-16LE mode (Windows)")
    g.add_argument("--regex", action="store_true", help="regex pattern")
    g.add_argument("--max-results", type=int, default=0)
    g.set_defaults(func=cmd_grep)

    y = sub.add_parser("yara", help="Task 1.3: YARA scan")
    y.add_argument("image", type=Path)
    y.add_argument("rules", type=Path, help="YARA rule file")
    y.add_argument("--max-size", type=int, default=0, help="max file size in bytes")
    y.set_defaults(func=cmd_yara)

    ts = sub.add_parser("timestomp", help="Task 1.4: timestamp anomaly detection")
    ts.add_argument("image", type=Path)
    ts.set_defaults(func=cmd_timestomp)

    u = sub.add_parser("unallocated", help="Task 1.5: unallocated block extraction")
    u.add_argument("image", type=Path)
    u.add_argument("-o", "--output", type=Path, help="output raw file")
    u.add_argument("-v", "--verbose", action="store_true")
    u.set_defaults(func=cmd_unallocated)

    s = sub.add_parser("slack", help="Task 1.6: file slack extraction")
    s.add_argument("image", type=Path)
    s.add_argument("-o", "--output-dir", type=Path, help="directory for slack files")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_slack)

    args = p.parse_args()
    if not args.image.exists():
        print(f"Error: {args.image} not found", file=sys.stderr)
        return 1
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
