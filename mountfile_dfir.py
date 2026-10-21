#!/usr/bin/env python3
"""
mountfile_dfir.py — single-file DFIR prototype.

Возможности:
  - grep по raw-образу без извлечения на диск
  - timeline CSV -> SQLite
  - простой timestomp-детектор
  - plugin registry с хуками on_open/on_file/on_close
  - опциональный REST API (/timeline) через FastAPI

Зависимости: только stdlib.
Для REST: pip install fastapi uvicorn
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mmap
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Timeline model
# ---------------------------------------------------------------------------


@dataclass
class TimelineRecord:
    path: str
    size: int | None = None
    mtime: float | None = None
    atime: float | None = None
    ctime: float | None = None
    birth: float | None = None
    sha256: str | None = None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def read_timeline_csv(path: str | Path) -> Iterator[TimelineRecord]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield TimelineRecord(
                path=row.get("path") or row.get("Path") or "",
                size=_opt_int(row.get("size")),
                mtime=_opt_float(row.get("mtime")),
                atime=_opt_float(row.get("atime")),
                ctime=_opt_float(row.get("ctime")),
                birth=_opt_float(row.get("birth")),
                sha256=row.get("sha256") or None,
            )


# ---------------------------------------------------------------------------
# Grep по raw-образу
# ---------------------------------------------------------------------------


def _context(mm: mmap.mmap, start: int, end: int, context: int) -> str:
    left = max(0, start - context)
    right = min(len(mm), end + context)
    return mm[left:right].decode("utf-8", errors="replace")


def grep_image(
    image_path: str | Path,
    pattern: str,
    *,
    regex: bool = False,
    encoding: str = "utf-8",
    context: int = 64,
    max_hits: int = 1000,
) -> Iterator[dict[str, Any]]:
    """
    Ищет pattern в raw-образе через mmap.
    encoding: utf-8, utf-16le, latin-1, raw.
    """
    if encoding == "raw":
        needle = pattern.encode("latin-1")
    else:
        needle = pattern.encode(encoding)

    with open(image_path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if regex:
                rx = re.compile(needle)
                for i, match in enumerate(rx.finditer(mm)):
                    if i >= max_hits:
                        break
                    start, end = match.span()
                    yield {
                        "offset": start,
                        "length": end - start,
                        "context": _context(mm, start, end, context),
                    }
            else:
                pos = 0
                hits = 0
                while hits < max_hits:
                    idx = mm.find(needle, pos)
                    if idx < 0:
                        break
                    yield {
                        "offset": idx,
                        "length": len(needle),
                        "context": _context(mm, idx, idx + len(needle), context),
                    }
                    hits += 1
                    pos = idx + 1


# ---------------------------------------------------------------------------
# SQLite timeline
# ---------------------------------------------------------------------------


class TimelineDB:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                size INTEGER,
                mtime REAL,
                atime REAL,
                ctime REAL,
                birth REAL,
                sha256 TEXT
            )
            """
        )
        self.conn.commit()

    def add(self, records: Iterable[TimelineRecord]) -> int:
        rows = [
            (r.path, r.size, r.mtime, r.atime, r.ctime, r.birth, r.sha256)
            for r in records
        ]
        self.conn.executemany(
            """
            INSERT INTO timeline(path, size, mtime, atime, ctime, birth, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def query(
        self, *, limit: int = 1000, path_like: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM timeline"
        params: list[Any] = []
        if path_like:
            sql += " WHERE path LIKE ?"
            params.append(f"%{path_like}%")
        sql += " ORDER BY mtime IS NULL, mtime LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# Timestomp detection
# ---------------------------------------------------------------------------


def detect_timestomp(
    records: Iterable[TimelineRecord],
    *,
    skew: float = 2.0,
) -> Iterator[tuple[TimelineRecord, list[str]]]:
    """
    Простые эвристики:
      - mtime заметно раньше ctime
      - atime заметно раньше mtime
      - mtime раньше birth
    """
    for r in records:
        reasons: list[str] = []

        if r.mtime is not None and r.ctime is not None and r.mtime + skew < r.ctime:
            reasons.append("mtime < ctime")
        if r.atime is not None and r.mtime is not None and r.atime + skew < r.mtime:
            reasons.append("atime < mtime")
        if r.birth is not None and r.mtime is not None and r.mtime + skew < r.birth:
            reasons.append("mtime < birth")

        if reasons:
            yield r, reasons


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------


class PluginRegistry:
    HOOKS = ("on_open", "on_file", "on_close")

    def __init__(self) -> None:
        self.hooks: dict[str, list[Any]] = {name: [] for name in self.HOOKS}

    def register(self, plugin: Any) -> None:
        for hook in self.HOOKS:
            fn = getattr(plugin, hook, None)
            if callable(fn):
                self.hooks[hook].append(fn)

    def emit(self, hook: str, *args: Any, **kwargs: Any) -> list[Any]:
        if hook not in self.hooks:
            raise ValueError(f"unknown hook: {hook}")
        return [fn(*args, **kwargs) for fn in self.hooks[hook]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_grep(args: argparse.Namespace) -> None:
    for hit in grep_image(
        args.image,
        args.pattern,
        regex=args.regex,
        encoding=args.encoding,
        context=args.context,
        max_hits=args.max_hits,
    ):
        if args.json:
            print(json.dumps(hit, ensure_ascii=False))
        else:
            print(f"0x{hit['offset']:x}: {hit['context']!r}")


def cmd_timeline(args: argparse.Namespace) -> None:
    records = list(read_timeline_csv(args.csv))
    db = TimelineDB(args.db)

    if args.query:
        result = db.query(limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        count = db.add(records)
        print(f"added {count} records to {args.db}")


def cmd_timestomp(args: argparse.Namespace) -> None:
    records = read_timeline_csv(args.csv)
    findings = []
    for record, reasons in detect_timestomp(records, skew=args.skew):
        findings.append({"record": asdict(record), "reasons": reasons})

    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"{item['record']['path']}: {', '.join(item['reasons'])}")


def cmd_hash(args: argparse.Namespace) -> None:
    print(sha256_file(args.file))


def cmd_serve(args: argparse.Namespace) -> None:
    try:
        import uvicorn
        from fastapi import FastAPI
    except ImportError:
        sys.exit("Установи зависимости: pip install fastapi uvicorn")

    app = FastAPI(title="mountfile-dfir")
    db = TimelineDB(args.db)

    @app.get("/timeline")
    def timeline(limit: int = 100, path: str | None = None) -> list[dict[str, Any]]:
        return db.query(limit=limit, path_like=path)

    uvicorn.run(app, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-file DFIR prototype for mount-file issue #87"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_grep = sub.add_parser("grep", help="grep по raw-образу")
    p_grep.add_argument("image")
    p_grep.add_argument("pattern")
    p_grep.add_argument("--regex", action="store_true")
    p_grep.add_argument(
        "--encoding", default="utf-8", choices=["utf-8", "utf-16le", "latin-1", "raw"]
    )
    p_grep.add_argument("--context", type=int, default=64)
    p_grep.add_argument("--max-hits", type=int, default=1000)
    p_grep.add_argument("--json", action="store_true")
    p_grep.set_defaults(func=cmd_grep)

    p_tl = sub.add_parser("timeline", help="timeline CSV -> SQLite")
    p_tl.add_argument("csv")
    p_tl.add_argument("--db", default="timeline.db")
    p_tl.add_argument("--query", action="store_true")
    p_tl.add_argument("--limit", type=int, default=100)
    p_tl.set_defaults(func=cmd_timeline)

    p_ts = sub.add_parser("timestomp", help="поиск признаков timestomping")
    p_ts.add_argument("csv")
    p_ts.add_argument("--skew", type=float, default=2.0)
    p_ts.add_argument("--json", action="store_true")
    p_ts.set_defaults(func=cmd_timestomp)

    p_hash = sub.add_parser("hash", help="SHA256 файла/образа")
    p_hash.add_argument("file")
    p_hash.set_defaults(func=cmd_hash)

    p_serve = sub.add_parser("serve", help="REST API /timeline")
    p_serve.add_argument("--db", default="timeline.db")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
