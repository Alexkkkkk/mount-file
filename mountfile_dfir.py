#!/usr/bin/env python3
"""
mountfile_dfir.py — single-file DFIR toolkit (issue #87).

Покрывает:
  - Task 1.1 Timeline (SQLite)
  - Task 1.2 Grep по raw-образу
  - Task 1.3 YARA (yara-python если есть, иначе сигнатуры)
  - Task 1.4 Timestomp detection
  - Task 2.1 Universal RW (базовый слой)
  - Task 2.2 Live diff (snapshot + diff)
  - Task 2.3 Crypto containers (LUKS/BitLocker detect + wrappers)
  - Task 4.1 Plugin system (+ example plugins)
  - Task 4.2 REST API (basic auth)

Зависимости: stdlib.
Опционально: yara-python, fastapi, uvicorn.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import mmap
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


# =========================================================================
# 1. Timeline (Task 1.1)
# =========================================================================

@dataclass
class TimelineRecord:
    path: str
    size: int | None = None
    mtime: float | None = None
    atime: float | None = None
    ctime: float | None = None
    birth: float | None = None
    sha256: str | None = None


def _opt_float(v: Any) -> float | None:
    return float(v) if v not in (None, "") else None


def _opt_int(v: Any) -> int | None:
    return int(v) if v not in (None, "") else None


def read_timeline_csv(path: str | Path) -> Iterator[TimelineRecord]:
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yield TimelineRecord(
                path=row.get("path") or row.get("Path") or "",
                size=_opt_int(row.get("size")),
                mtime=_opt_float(row.get("mtime")),
                atime=_opt_float(row.get("atime")),
                ctime=_opt_float(row.get("ctime")),
                birth=_opt_float(row.get("birth")),
                sha256=row.get("sha256") or None,
            )


class TimelineDB:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL, size INTEGER,
                mtime REAL, atime REAL, ctime REAL, birth REAL,
                sha256 TEXT
            )""")
        self.conn.commit()

    def add(self, records: Iterable[TimelineRecord]) -> int:
        rows = [(r.path, r.size, r.mtime, r.atime, r.ctime, r.birth, r.sha256)
                for r in records]
        self.conn.executemany(
            "INSERT INTO timeline(path,size,mtime,atime,ctime,birth,sha256) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def query(self, *, limit: int = 1000,
              path_like: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM timeline", []
        if path_like:
            sql += " WHERE path LIKE ?"
            params.append(f"%{path_like}%")
        sql += " ORDER BY mtime IS NULL, mtime LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]


# =========================================================================
# 2. Grep по raw-образу (Task 1.2)
# =========================================================================

def _ctx(mm: mmap.mmap, s: int, e: int, c: int) -> str:
    return mm[max(0, s - c):min(len(mm), e + c)].decode("utf-8", errors="replace")


def grep_image(image_path, pattern: str, *, regex=False, encoding="utf-8",
               context=64, max_hits=1000) -> Iterator[dict[str, Any]]:
    enc = "latin-1" if encoding == "raw" else encoding
    needle = pattern.encode(enc)
    with open(image_path, "rb") as f, mmap.mmap(f.fileno(), 0,
                                                access=mmap.ACCESS_READ) as mm:
        if regex:
            for i, m in enumerate(re.finditer(needle, mm)):
                if i >= max_hits:
                    return
                s, e = m.span()
                yield {"offset": s, "length": e - s,
                       "context": _ctx(mm, s, e, context)}
        else:
            pos, hits = 0, 0
            while hits < max_hits:
                idx = mm.find(needle, pos)
                if idx < 0:
                    return
                yield {"offset": idx, "length": len(needle),
                       "context": _ctx(mm, idx, idx + len(needle), context)}
                hits += 1
                pos = idx + 1


# =========================================================================
# 3. YARA (Task 1.3) — yara-python если есть, иначе сигнатуры
# =========================================================================

BUILTIN_SIGNATURES: dict[str, bytes] = {
    "PE_MZ": b"MZ",
    "ELF": b"\x7fELF",
    "ZIP": b"PK\x03\x04",
    "PDF": b"%PDF-",
    "EICAR": b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR",
    "PowerShell_EncodedCommand": b"-EncodedCommand",
    "Mimikatz_marker": b"sekurlsa::logonpasswords",
}


def yara_scan(image_path, rules_path: str | None = None,
              max_hits: int = 1000) -> list[dict[str, Any]]:
    """Если установлен yara-python и задан rules_path — использует YARA.
    Иначе — встроенные сигнатуры через mmap."""
    try:
        import yara  # type: ignore
    except ImportError:
        yara = None  # fallback

    results: list[dict[str, Any]] = []
    if yara and rules_path:
        rules = yara.compile(filepath=rules_path)
        for m in rules.match(str(image_path), timeout=60):
            for s in m.strings:
                for off, data in s.instances:
                    results.append({
                        "rule": m.rule, "tag": getattr(s, "identifier", ""),
                        "offset": off, "data": data[:64].decode("latin-1", "replace"),
                    })
                    if len(results) >= max_hits:
                        return results
        return results

    # fallback: встроенные сигнатуры
    with open(image_path, "rb") as f, mmap.mmap(f.fileno(), 0,
                                                access=mmap.ACCESS_READ) as mm:
        for name, needle in BUILTIN_SIGNATURES.items():
            pos = 0
            while len(results) < max_hits:
                idx = mm.find(needle, pos)
                if idx < 0:
                    break
                results.append({"rule": name, "offset": idx,
                                "data": needle.decode("latin-1", "replace")})
                pos = idx + 1
    return results


# =========================================================================
# 4. Timestomp detection (Task 1.4)
# =========================================================================

def detect_timestomp(records: Iterable[TimelineRecord], skew: float = 2.0
                     ) -> Iterator[tuple[TimelineRecord, list[str]]]:
    for r in records:
        reasons: list[str] = []
        if r.mtime and r.ctime and r.mtime + skew < r.ctime:
            reasons.append("mtime < ctime")
        if r.atime and r.mtime and r.atime + skew < r.mtime:
            reasons.append("atime < mtime")
        if r.birth and r.mtime and r.mtime + skew < r.birth:
            reasons.append("mtime < birth")
        if r.mtime and r.mtime > time.time() + 86400:
            reasons.append("mtime in future")
        if reasons:
            yield r, reasons


# =========================================================================
# 5. Universal RW layer (Task 2.1) — базовый слой с backup + journal
# =========================================================================

class UniversalRW:
    """Универсальный слой чтения/записи. Работает с обычными файлами и
    с монтированными образами. Автобэкап + hash-chain journal."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.journal_path = self.root / ".mountfile_journal.jsonl"
        self._last_hash = "0" * 64

    def _abs(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError("path escapes root")
        return p

    def read(self, rel: str) -> bytes:
        return self._abs(rel).read_bytes()

    def write(self, rel: str, data: bytes, *, autobackup=True) -> None:
        target = self._abs(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if autobackup and target.exists():
            bak = target.with_suffix(target.suffix + f".bak.{int(time.time())}")
            shutil.copy2(target, bak)
        target.write_bytes(data)
        self._journal("write", rel, hashlib.sha256(data).hexdigest())

    def _journal(self, op: str, rel: str, digest: str) -> None:
        entry = {
            "ts": time.time(), "op": op, "path": rel,
            "sha256": digest, "prev": self._last_hash,
        }
        entry["chain"] = hashlib.sha256(
            json.dumps(entry, sort_keys=True).encode()).hexdigest()
        self._last_hash = entry["chain"]
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


# =========================================================================
# 6. Live diff (Task 2.2)
# =========================================================================

def snapshot(root: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(root)
    snap: dict[str, dict[str, Any]] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        st = p.stat()
        snap[rel] = {"sha256": h.hexdigest(), "size": st.st_size,
                     "mtime": st.st_mtime}
    return snap


def live_diff(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(k for k in (set(old) & set(new))
                      if old[k]["sha256"] != new[k]["sha256"])
    meta_changed = sorted(k for k in (set(old) & set(new))
                          if old[k]["mtime"] != new[k]["mtime"]
                          and old[k]["sha256"] == new[k]["sha256"])
    return {"added": added, "removed": removed,
            "modified": modified, "meta_changed": meta_changed}


# =========================================================================
# 7. Crypto containers (Task 2.3) — detect + wrappers
# =========================================================================

LUKS_MAGIC = b"LUKS\xba\xbe"
BITLOCKER_MAGIC = b"\xeb\x52\x90" + b"-FVE-FS-"


def detect_container(path: str | Path) -> str:
    with open(path, "rb") as f:
        head = f.read(512)
    if head.startswith(LUKS_MAGIC):
        return "luks"
    if BITLOCKER_MAGIC in head[:64]:
        return "bitlocker"
    return "unknown"


def unlock_luks(image: str, name: str, *, key_file: str | None = None,
                password: str | None = None) -> str:
    if not shutil.which("cryptsetup"):
        raise RuntimeError("cryptsetup не установлен")
    cmd = ["cryptsetup", "luksOpen", image, name]
    if key_file:
        cmd += ["--key-file", key_file]
    if password:
        env = dict(os.environ, CRYPTSETUP_PASSPHRASE=password)
        subprocess.run(cmd, check=True, env=env)
    else:
        subprocess.run(cmd, check=True)
    return f"/dev/mapper/{name}"


def unlock_bitlocker(image: str, mountpoint: str, *,
                     recovery_password: str | None = None) -> str:
    if not shutil.which("dislocker"):
        raise RuntimeError("dislocker не установлен")
    cmd = ["dislocker", "-V", image, "--", mountpoint]
    if recovery_password:
        cmd.insert(1, f"-p{recovery_password}")
    subprocess.run(cmd, check=True)
    return mountpoint


# =========================================================================
# 8. Plugin system (Task 4.1) + example plugins
# =========================================================================

class PluginRegistry:
    HOOKS = ("on_open", "on_file", "on_close")

    def __init__(self) -> None:
        self.hooks: dict[str, list[Callable]] = {h: [] for h in self.HOOKS}

    def register(self, plugin: Any) -> None:
        for h in self.HOOKS:
            fn = getattr(plugin, h, None)
            if callable(fn):
                self.hooks[h].append(fn)

    def emit(self, hook: str, *a: Any, **kw: Any) -> list[Any]:
        if hook not in self.hooks:
            raise ValueError(f"unknown hook: {hook}")
        return [fn(*a, **kw) for fn in self.hooks[hook]]

    def load_dir(self, path: str | Path) -> int:
        """Загружает .py-плагины из директории. Каждый плагин должен
        определить класс Plugin с методами-хуками."""
        import importlib.util
        count = 0
        for py in Path(path).glob("*.py"):
            spec = importlib.util.spec_from_file_location(py.stem, py)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, "Plugin", None)
            if cls:
                self.register(cls())
                count += 1
        return count


class ExampleHashPlugin:
    """Плагин: считает SHA256 и пишет в on_file."""
    def on_file(self, path: str, data: bytes) -> dict[str, str]:
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}

    def on_open(self, image: str) -> None:
        print(f"[plugin] opened image: {image}", file=sys.stderr)

    def on_close(self) -> None:
        print("[plugin] closed", file=sys.stderr)


class ExampleIOCPlugin:
    """Плагин: ищет IOC-паттерны в тексте."""
    PATTERNS = re.compile(rb"(?i)(powershell|cmd\.exe|/etc/passwd|SELECT \* FROM)")

    def on_file(self, path: str, data: bytes) -> dict[str, Any] | None:
        m = self.PATTERNS.search(data)
        if m:
            return {"path": path, "ioc": m.group(0).decode("latin-1")}
        return None


# =========================================================================
# 9. REST API (Task 4.2) с basic-auth
# =========================================================================

def serve(db_path: str, host: str, port: int,
          user: str | None, password: str | None) -> None:
    try:
        from fastapi import Depends, FastAPI, HTTPException, status
        from fastapi.security import HTTPBasic, HTTPBasicCredentials
        import uvicorn
    except ImportError:
        sys.exit("Установи: pip install fastapi uvicorn")

    app = FastAPI(title="mountfile-dfir")
    security = HTTPBasic()
    db = TimelineDB(db_path)

    def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
        if user is None and password is None:
            return "anonymous"
        ok = (creds.username == user and creds.password == password)
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                headers={"WWW-Authenticate": "Basic"})
        return creds.username

    @app.get("/timeline")
    def timeline(limit: int = 100, path: str | None = None,
                 _: str = Depends(auth)) -> list[dict[str, Any]]:
        return db.query(limit=limit, path_like=path)

    @app.get("/hash")
    def hash_path(p: str, _: str = Depends(auth)) -> dict[str, str]:
        if not Path(p).is_file():
            raise HTTPException(404, "not found")
        return {"sha256": sha256_file(p)}

    @app.get("/diff")
    def diff(old: str, new: str, _: str = Depends(auth)) -> dict[str, list]:
        return live_diff(snapshot(old), snapshot(new))

    uvicorn.run(app, host=host, port=port)


# =========================================================================
# Helpers
# =========================================================================

def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(chunk), b""):
            h.update(c)
    return h.hexdigest()


# =========================================================================
# CLI
# =========================================================================

def cmd_grep(a: argparse.Namespace) -> None:
    for hit in grep_image(a.image, a.pattern, regex=a.regex,
                          encoding=a.encoding, context=a.context,
                          max_hits=a.max_hits):
        print(json.dumps(hit, ensure_ascii=False) if a.json
              else f"0x{hit['offset']:x}: {hit['context']!r}")


def cmd_timeline(a: argparse.Namespace) -> None:
    db = TimelineDB(a.db)
    if a.query:
        print(json.dumps(db.query(limit=a.limit, path_like=a.path),
                         ensure_ascii=False, indent=2))
    else:
        print(f"added {db.add(read_timeline_csv(a.csv))} records")


def cmd_timestomp(a: argparse.Namespace) -> None:
    out = [{"record": asdict(r), "reasons": reasons}
           for r, reasons in detect_timestomp(read_timeline_csv(a.csv), a.skew)]
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for item in out:
            print(f"{item['record']['path']}: {', '.join(item['reasons'])}")


def cmd_yara(a: argparse.Namespace) -> None:
    for hit in yara_scan(a.image, a.rules, a.max_hits):
        print(json.dumps(hit, ensure_ascii=False))


def cmd_hash(a: argparse.Namespace) -> None:
    print(sha256_file(a.file))


def cmd_diff(a: argparse.Namespace) -> None:
    print(json.dumps(live_diff(snapshot(a.old), snapshot(a.new)),
                     indent=2, ensure_ascii=False))


def cmd_crypto(a: argparse.Namespace) -> None:
    kind = detect_container(a.image)
    print(f"container: {kind}")
    if a.unlock:
        if kind == "luks":
            print(f"mapped at {unlock_luks(a.image, a.name, key_file=a.key_file)}")
        elif kind == "bitlocker":
            print(f"unlocked at {unlock_bitlocker(a.image, a.name, recovery_password=a.password)}")
        else:
            sys.exit("unknown container type")


def cmd_plugins(a: argparse.Namespace) -> None:
    reg = PluginRegistry()
    reg.register(ExampleHashPlugin())
    reg.register(ExampleIOCPlugin())
    if a.dir:
        print(f"loaded {reg.load_dir(a.dir)} external plugins", file=sys.stderr)
    data = Path(a.file).read_bytes()
    reg.emit("on_open", a.file)
    print(json.dumps(reg.emit("on_file", a.file, data), default=str,
                     ensure_ascii=False, indent=2))
    reg.emit("on_close")


def cmd_serve(a: argparse.Namespace) -> None:
    serve(a.db, a.host, a.port, a.user, a.password)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="mount-file DFIR toolkit (#87)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grep", help="grep по raw-образу (1.2)")
    g.add_argument("image"); g.add_argument("pattern")
    g.add_argument("--regex", action="store_true")
    g.add_argument("--encoding", default="utf-8",
                   choices=["utf-8", "utf-16le", "latin-1", "raw"])
    g.add_argument("--context", type=int, default=64)
    g.add_argument("--max-hits", type=int, default=1000)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_grep)

    t = sub.add_parser("timeline", help="timeline CSV -> SQLite (1.1)")
    t.add_argument("csv"); t.add_argument("--db", default="timeline.db")
    t.add_argument("--query", action="store_true")
    t.add_argument("--limit", type=int, default=100)
    t.add_argument("--path", default=None)
    t.set_defaults(func=cmd_timeline)

    ts = sub.add_parser("timestomp", help="timestomp detection (1.4)")
    ts.add_argument("csv"); ts.add_argument("--skew", type=float, default=2.0)
    ts.add_argument("--json", action="store_true")
    ts.set_defaults(func=cmd_timestomp)

    y = sub.add_parser("yara", help="YARA / сигнатуры (1.3)")
    y.add_argument("image"); y.add_argument("--rules", default=None)
    y.add_argument("--max-hits", type=int, default=1000)
    y.set_defaults(func=cmd_yara)

    h = sub.add_parser("hash", help="SHA256 файла/образа")
    h.add_argument("file"); h.set_defaults(func=cmd_hash)

    d = sub.add_parser("diff", help="live diff двух директорий (2.2)")
    d.add_argument("old"); d.add_argument("new")
    d.set_defaults(func=cmd_diff)

    c = sub.add_parser("crypto", help="LUKS / BitLocker (2.3)")
    c.add_argument("image"); c.add_argument("--unlock", action="store_true")
    c.add_argument("--name", default="mnt")
    c.add_argument("--key-file", default=None)
    c.add_argument("--password", default=None)
    c.set_defaults(func=cmd_crypto)

    pl = sub.add_parser("plugins", help="plugin system (4.1)")
    pl.add_argument("file"); pl.add_argument("--dir", default=None)
    pl.set_defaults(func=cmd_plugins)

    s = sub.add_parser("serve", help="REST API (4.2)")
    s.add_argument("--db", default="timeline.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--user", default=None)
    s.add_argument("--password", default=None)
    s.set_defaults(func=cmd_serve)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
