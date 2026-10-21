#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MOUNTFILE_FIX — auto-patched + OLMA full integration
"""
Единый патч-скрипт: внедряет OLMA-фреймворк в Mount File.

Создаёт пакет mountfile/olma/ (7 модулей) и патчит main.py + модули,
подключая трассировку, анализ и автоисправление.

Запуск:
  python mountfile_fix.py                # применить
  python mountfile_fix.py --dry-run      # план
  python mountfile_fix.py --revert       # откат из .bak
  python mountfile_fix.py --check        # синтаксис
  python mountfile_fix.py --smoke        # применить + smoke
"""

from __future__ import annotations

import argparse
import ast
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "# MOUNTFILE_FIX"
BAK = ".bak"


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(10):
        if (cur / "main.py").exists() and (cur / "mountfile").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write(p: Path, t: str, *, dry: bool) -> None:
    if dry:
        print(f"  [dry] {p}")
        return
    p.write_text(t, encoding="utf-8")


def backup(p: Path, *, dry: bool) -> None:
    if not p.exists():
        return
    b = Path(str(p) + BAK)
    if b.exists():
        return
    if dry:
        print(f"  [dry] backup {b.name}")
        return
    shutil.copy2(p, b)


def insert_header(t: str, h: str) -> str:
    lines = t.splitlines(keepends=True)
    at = 0
    for i, ln in enumerate(lines[:3]):
        if ln.startswith("#!") or "coding" in ln:
            at = i + 1
    lines.insert(at, h)
    return "".join(lines)


def insert_after_imports(t: str, block: str) -> str:
    lines = t.splitlines(keepends=True)
    n = len(lines)
    i = 0
    # шебанг, coding-строка, начальные комментарии и пустые строки
    while i < n and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        i += 1
    # докстринг модуля, если он есть
    if i < n:
        s = lines[i].lstrip()
        for q in ('"""', "'''"):
            if s.startswith(q):
                if s.count(q) >= 2 and s.strip() != q:
                    i += 1  # закрыт в той же строке
                else:
                    i += 1
                    while i < n and q not in lines[i]:
                        i += 1
                    i += 1
                break
    # импорты (включая from __future__), комментарии и пустые строки
    last = max(i - 1, 0)
    while i < n:
        s = lines[i].strip()
        if s.startswith(("import ", "from ")) or s.startswith("#") or not s:
            last = i
            i += 1
        else:
            break
    lines.insert(last + 1, block)
    return "".join(lines)


def patch_file(path, *, dry, force=False, transform=None) -> bool:
    if not path.exists():
        print(f"  [skip] {path.name}")
        return False
    orig = read(path)
    if MARKER in orig and not force:
        print(f"  [ok] {path.name}")
        return False
    if transform is None:
        return False
    try:
        new = transform(orig)
    except Exception as e:
        print(f"  [err] {path.name}: {e}")
        return False
    if not new or new == orig:
        print(f"  [skip] {path.name}: no change")
        return False
    new = insert_header(new, f"{MARKER} — auto-patched + OLMA\n")
    backup(path, dry=dry)
    write(path, new, dry=dry)
    print(f"  [fixed] {path.name}")
    return True


def syntax_ok(p: Path) -> bool:
    try:
        ast.parse(read(p), filename=str(p))
        return True
    except SyntaxError as e:
        print(f"  [syntax] {p}: {e}")
        return False


def compile_file(p: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory() as td:
            py_compile.compile(str(p), cfile=os.path.join(td, "o.pyc"), doraise=True)
        return True
    except py_compile.PyCompileError as e:
        print(f"  [compile] {p}: {e}")
        return False


# =========================================================================== #
# ШАБЛОНЫ ФАЙЛОВ OLMA
# =========================================================================== #

# --- mountfile/olma/__init__.py -------------------------------------------- #
OLMA_INIT = '''\
# MOUNTFILE_FIX — OLMA package
"""OLMA — Object-Level Memory Analysis & Access.

Единая точка входа:
    from mountfile.olma import olma
    olma.install()                # включить трассировку
    olma.analyze()                # статический анализ проекта
    olma.fix(dry_run=True)        # автоисправление
    olma.report(fmt="html")       # отчёт
    olma.trace_stats()            # статистика runtime-трассировки
"""

from __future__ import annotations

from .core import (
    OlmaError, OlmaBoundsError, OlmaIntegrityError, OlmaStateError,
    OlmaHandle, OlmaView, OlmaBuffer,
    OlmaCluster, OlmaChain,
    OlmaPartition, OlmaPartitionTable,
    OlmaTransaction, OlmaXorDecoder,
    safe_download, verify_sha256,
    SECTOR, FAT32_MAX_CLUSTER, FAT32_EOC_MIN, FAT32_BAD,
)
from .runtime import OlmaRuntime, RuntimeEvent
from .analyzer import OlmaAnalyzer, Issue
from .rules import RULES, Rule, Severity
from .fixer import OlmaFixer, FixResult
from .reporter import OlmaReporter
from .cli import olma_main


class Olma:
    """Фасад для всех операций OLMA."""

    def __init__(self) -> None:
        self._runtime: OlmaRuntime | None = None
        self._analyzer: OlmaAnalyzer | None = None

    # --- runtime ---
    def install(self, *, trace_files: bool = True, trace_memory: bool = False) -> None:
        if self._runtime is not None:
            return
        self._runtime = OlmaRuntime(
            trace_files=trace_files, trace_memory=trace_memory,
        )
        self._runtime.install()

    def uninstall(self) -> None:
        if self._runtime:
            self._runtime.uninstall()
            self._runtime = None

    def trace_stats(self) -> dict:
        return self._runtime.stats() if self._runtime else {}

    def trace_events(self, limit: int = 100) -> list:
        return self._runtime.events(limit) if self._runtime else []

    # --- analysis ---
    def analyzer(self, root: str | None = None) -> OlmaAnalyzer:
        if self._analyzer is None:
            from pathlib import Path
            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._analyzer = OlmaAnalyzer(base)
        return self._analyzer

    def analyze(self, root: str | None = None) -> list:
        return self.analyzer(root).scan()

    # --- fix ---
    def fix(self, *, dry_run: bool = True, root: str | None = None) -> list:
        from .fixer import OlmaFixer
        issues = self.analyze(root)
        fixer = OlmaFixer(self.analyzer(root).root, dry_run=dry_run)
        return fixer.apply(issues)

    # --- report ---
    def report(self, *, fmt: str = "html", out: str | None = None,
               root: str | None = None) -> str:
        from .reporter import OlmaReporter
        issues = self.analyze(root)
        rep = OlmaReporter(issues, self.trace_stats())
        return rep.render(fmt=fmt, out=out)


olma = Olma()

__all__ = [
    "olma", "Olma",
    "OlmaError", "OlmaBoundsError", "OlmaIntegrityError", "OlmaStateError",
    "OlmaHandle", "OlmaView", "OlmaBuffer",
    "OlmaCluster", "OlmaChain",
    "OlmaPartition", "OlmaPartitionTable",
    "OlmaTransaction", "OlmaXorDecoder",
    "OlmaRuntime", "OlmaAnalyzer", "OlmaFixer", "OlmaReporter",
    "Issue", "Rule", "Severity", "RULES", "FixResult",
    "safe_download", "verify_sha256", "olma_main",
    "SECTOR", "FAT32_MAX_CLUSTER", "FAT32_EOC_MIN", "FAT32_BAD",
]
'''


# --- mountfile/olma/core.py ------------------------------------------------ #
OLMA_CORE = '''\
# MOUNTFILE_FIX — OLMA core
"""Безопасные обёртки памяти и файлов."""

from __future__ import annotations
import hashlib, os, tempfile
from dataclasses import dataclass
from typing import BinaryIO, Optional


class OlmaError(Exception): ...
class OlmaBoundsError(OlmaError): ...
class OlmaIntegrityError(OlmaError): ...
class OlmaStateError(OlmaError): ...


SECTOR = 512
MAX_PARTITIONS = 128
FAT32_MAX_CLUSTER = 0x0FFFFFF5
FAT32_EOC_MIN = 0x0FFFFFF8
FAT32_BAD = 0x0FFFFFF7
DEFAULT_CHUNK = 1 << 20
DEFAULT_MAX_DOWNLOAD = 512 * 1024 * 1024


class OlmaHandle:
    """RAII над файлом."""

    __slots__ = ("_fh", "_path", "_size", "_closed", "_tracer")

    def __init__(self, path, mode="rb", *, tracer=None) -> None:
        self._path = os.fspath(path)
        self._tracer = tracer
        self._fh: BinaryIO = open(self._path, mode)
        self._closed = False
        try:
            self._fh.seek(0, os.SEEK_END); self._size = self._fh.tell(); self._fh.seek(0)
        except OSError:
            self._size = 0
        if tracer:
            tracer.on_open(self._path, mode)

    @property
    def path(self) -> str: return self._path
    @property
    def size(self) -> int: return self._size
    @property
    def closed(self) -> bool: return self._closed

    def read_at(self, offset: int, size: int) -> bytes:
        if self._closed: raise OlmaStateError("handle closed")
        if size < 0 or offset < 0: raise OlmaBoundsError("negative offset/size")
        if offset + size > self._size:
            raise OlmaBoundsError(f"{offset}+{size} > {self._size}")
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise OlmaIntegrityError(f"short read: {len(data)} != {size}")
        if self._tracer:
            self._tracer.on_read(self._path, offset, size)
        return data

    def read_upto(self, offset: int, size: int) -> bytes:
        if self._closed: raise OlmaStateError("handle closed")
        if size <= 0 or offset >= self._size: return b""
        offset = max(0, offset)
        self._fh.seek(offset)
        n = min(size, self._size - offset)
        data = self._fh.read(n)
        if self._tracer:
            self._tracer.on_read(self._path, offset, len(data))
        return data

    def write_at(self, offset: int, data: bytes) -> None:
        if self._closed: raise OlmaStateError("handle closed")
        if "r" in self._fh.mode and "+" not in self._fh.mode:
            raise OlmaStateError("read-only")
        if offset < 0: raise OlmaBoundsError("negative offset")
        self._fh.seek(offset); self._fh.write(data); self._fh.flush()
        if self._tracer:
            self._tracer.on_write(self._path, offset, len(data))

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()
            try: os.fsync(self._fh.fileno())
            except OSError: pass

    def close(self) -> None:
        if not self._closed:
            try: self._fh.close()
            finally:
                self._closed = True
                if self._tracer:
                    self._tracer.on_close(self._path)

    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def __del__(self):
        try: self.close()
        except Exception: pass


@dataclass(frozen=True)
class OlmaView:
    handle: OlmaHandle
    base_offset: int
    length: int

    def __post_init__(self) -> None:
        if self.base_offset < 0 or self.length < 0:
            raise OlmaBoundsError("negative")
        if self.base_offset + self.length > self.handle.size:
            raise OlmaBoundsError("view out of file")

    @property
    def size(self) -> int: return self.length

    def read(self, off: int, size: int) -> bytes:
        if off < 0 or size < 0 or off + size > self.length:
            raise OlmaBoundsError("out of view")
        return self.handle.read_at(self.base_offset + off, size)

    def read_upto(self, off: int, size: int) -> bytes:
        if off < 0 or off >= self.length or size <= 0: return b""
        return self.handle.read_at(self.base_offset + off, min(size, self.length - off))

    def subview(self, off: int, length: int) -> "OlmaView":
        if off < 0 or length < 0 or off + length > self.length:
            raise OlmaBoundsError("subview")
        return OlmaView(self.handle, self.base_offset + off, length)


class OlmaBuffer:
    __slots__ = ("_data",)

    def __init__(self, data) -> None:
        self._data = bytes(data)

    def __len__(self): return len(self._data)

    def __getitem__(self, k):
        if isinstance(k, slice): return self._data[k]
        if not isinstance(k, int): raise TypeError(type(k).__name__)
        if k < 0 or k >= len(self._data):
            raise OlmaBoundsError(f"index {k} out of [0,{len(self._data)})")
        return self._data[k]

    def slice(self, start: int, size: int) -> bytes:
        if start < 0 or size < 0 or start + size > len(self._data):
            raise OlmaBoundsError(f"{start}+{size}")
        return self._data[start:start + size]

    def u8(self, o): return self.slice(o, 1)[0]
    def u16le(self, o): return int.from_bytes(self.slice(o, 2), "little")
    def u32le(self, o): return int.from_bytes(self.slice(o, 4), "little")
    def u64le(self, o): return int.from_bytes(self.slice(o, 8), "little")
    def raw(self): return self._data
    def sha256(self): return hashlib.sha256(self._data).hexdigest()


@dataclass(frozen=True)
class OlmaCluster:
    value: int
    max_cluster: int = FAT32_MAX_CLUSTER

    def __post_init__(self):
        if not (2 <= self.value < self.max_cluster):
            raise OlmaBoundsError(f"cluster {self.value:#x}")

    @property
    def is_eoc(self): return self.value >= FAT32_EOC_MIN
    @property
    def is_bad(self): return self.value == FAT32_BAD

    def next_from(self, fat_get):
        n = fat_get(self.value)
        if n == 0 or n >= FAT32_EOC_MIN or n == FAT32_BAD:
            return None
        try: return OlmaCluster(n, self.max_cluster)
        except OlmaBoundsError: return None


class OlmaChain:
    def __init__(self, start, fat_get, max_cluster=FAT32_MAX_CLUSTER, budget=1_000_000):
        self._start = OlmaCluster(start, max_cluster)
        self._fat_get = fat_get
        self._budget = budget

    def __iter__(self):
        seen, cur, left = set(), self._start, self._budget
        while cur is not None and left > 0:
            if cur.value in seen:
                raise OlmaIntegrityError(f"cycle at {cur.value:#x}")
            seen.add(cur.value)
            yield cur
            left -= 1
            cur = cur.next_from(self._fat_get)
        if left <= 0:
            raise OlmaIntegrityError("chain budget exceeded")


@dataclass(frozen=True)
class OlmaPartition:
    index: int
    start_sector: int
    size_sectors: int
    type_id: int = 0
    label: str = ""

    def __post_init__(self):
        if self.index < 0 or self.start_sector < 0 or self.size_sectors < 0:
            raise OlmaBoundsError("negative")

    def clamp(self, total):
        s = max(0, self.start_sector)
        if s >= total:
            return OlmaPartition(self.index, 0, 0, self.type_id, self.label)
        return OlmaPartition(self.index, s, min(self.size_sectors, total - s),
                             self.type_id, self.label)

    def view(self, h, total):
        p = self.clamp(total)
        return OlmaView(h, p.start_sector * SECTOR, p.size_sectors * SECTOR)


class OlmaPartitionTable:
    def __init__(self, buf: OlmaBuffer) -> None:
        self._buf = buf
        self._kind = "unknown"
        self._parts: list[OlmaPartition] = []
        self._parse()

    @property
    def kind(self): return self._kind
    @property
    def partitions(self): return list(self._parts)

    def _parse(self):
        if len(self._buf) < SECTOR: raise OlmaIntegrityError("too small")
        if self._buf.slice(510, 2) == b"\\x55\\xAA":
            if len(self._buf) >= 1024 and self._buf.slice(512, 8) == b"EFI PART":
                self._kind = "gpt"; self._parse_gpt()
            else:
                self._kind = "mbr"; self._parse_mbr()
        else:
            raise OlmaIntegrityError("no 0x55AA")

    def _parse_mbr(self):
        for i in range(4):
            e = self._buf.slice(446 + i * 16, 16)
            s = int.from_bytes(e[8:12], "little"); sz = int.from_bytes(e[12:16], "little")
            if sz == 0: continue
            self._parts.append(OlmaPartition(i, s, sz, e[4], ""))
            if len(self._parts) > MAX_PARTITIONS:
                raise OlmaIntegrityError("too many")

    def _parse_gpt(self):
        h = self._buf.slice(512, 92)
        lba = int.from_bytes(h[72:80], "little")
        num = min(int.from_bytes(h[80:84], "little"), MAX_PARTITIONS)
        esz = int.from_bytes(h[84:88], "little")
        if esz == 0 or esz > 512: raise OlmaIntegrityError(f"esz={esz}")
        base = lba * SECTOR
        for i in range(num):
            off = base + i * esz
            if off + 16 > len(self._buf): break
            if self._buf.slice(off, 16) == b"\\x00" * 16: continue
            f = int.from_bytes(self._buf.slice(off + 32, 8), "little")
            l = int.from_bytes(self._buf.slice(off + 40, 8), "little")
            if l >= f:
                self._parts.append(OlmaPartition(i, f, l - f + 1, 0, ""))


class OlmaTransaction:
    def __init__(self, target) -> None:
        self._target = os.fspath(target)
        d = os.path.dirname(os.path.abspath(self._target)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".olma-", suffix=".tmp")
        os.close(fd)
        self._tmp = tmp
        self._committed = False
        self._h = OlmaHandle(tmp, "wb")

    @property
    def handle(self): return self._h

    def write_all(self, data): self._h.write_at(0, data)

    def commit(self):
        if self._committed: raise OlmaStateError("already committed")
        self._h.flush(); self._h.close(); os.replace(self._tmp, self._target)
        self._committed = True

    def rollback(self):
        if self._committed: return
        try: self._h.close()
        except Exception: pass
        try: os.unlink(self._tmp)
        except OSError: pass

    def __enter__(self): return self
    def __exit__(self, t, e, tb):
        self.commit() if t is None else self.rollback()


class OlmaXorDecoder:
    def __init__(self, table) -> None:
        self._t = bytes(table)
        if not self._t: raise OlmaIntegrityError("empty table")

    @property
    def table_size(self): return len(self._t)

    def decode(self, data):
        n = len(self._t)
        return bytes(b ^ self._t[i % n] for i, b in enumerate(data))

    def decode_file(self, src, dst, chunk=DEFAULT_CHUNK):
        total, off, written = src.size, 0, 0
        while off < total:
            n = min(chunk, total - off)
            dst.write_at(off, self.decode(src.read_at(off, n)))
            off += n; written += n
        return written


def safe_download(url, *, timeout=30.0, max_size=DEFAULT_MAX_DOWNLOAD) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "MountFile/OLMA"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        ln = resp.headers.get("Content-Length")
        if ln and int(ln) > max_size: raise OlmaIntegrityError(f"too big: {ln}")
        data = bytearray()
        while True:
            c = resp.read(1 << 16)
            if not c: break
            data.extend(c)
            if len(data) > max_size: raise OlmaIntegrityError("size exceeded")
    return bytes(data)


def verify_sha256(data, expected_hex) -> bool:
    if not expected_hex: return True
    return hashlib.sha256(data).hexdigest().lower() == expected_hex.strip().lower()
'''


# --- mountfile/olma/runtime.py --------------------------------------------- #
OLMA_RUNTIME = '''\
# MOUNTFILE_FIX — OLMA runtime tracing
"""Трассировка файловых и memory-операций во время работы программы."""

from __future__ import annotations
import builtins, io, os, threading, time, traceback
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RuntimeEvent:
    kind: str            # open/read/write/seek/close/error
    path: str
    offset: int = 0
    size: int = 0
    duration_ms: float = 0.0
    mode: str = ""
    error: str = ""
    stack: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class OlmaRuntime:
    """Патчит builtins.open + io.IOBase, собирает события."""

    MAX_EVENTS = 20_000

    def __init__(self, *, trace_files=True, trace_memory=False) -> None:
        self._trace_files = trace_files
        self._trace_memory = trace_memory
        self._events: deque[RuntimeEvent] = deque(maxlen=self.MAX_EVENTS)
        self._lock = threading.Lock()
        self._installed = False
        self._orig_open = None
        self._orig_io_read = None
        self._orig_io_write = None
        self._orig_io_close = None

    # --- install/uninstall ---
    def install(self) -> None:
        if self._installed: return
        self._orig_open = builtins.open
        builtins.open = self._patched_open
        if self._trace_memory:
            self._orig_io_read = io.IOBase.read
            self._orig_io_write = io.IOBase.write
            self._orig_io_close = io.IOBase.close
            io.IOBase.read = self._patched_read
            io.IOBase.write = self._patched_write
            io.IOBase.close = self._patched_close
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed: return
        if self._orig_open: builtins.open = self._orig_open
        if self._orig_io_read: io.IOBase.read = self._orig_io_read
        if self._orig_io_write: io.IOBase.write = self._orig_io_write
        if self._orig_io_close: io.IOBase.close = self._orig_io_close
        self._installed = False

    # --- hooks ---
    def on_open(self, path, mode):
        self._push(RuntimeEvent("open", str(path), mode=mode, stack=self._stack()))

    def on_read(self, path, off, size):
        self._push(RuntimeEvent("read", str(path), offset=off, size=size))

    def on_write(self, path, off, size):
        self._push(RuntimeEvent("write", str(path), offset=off, size=size))

    def on_close(self, path):
        self._push(RuntimeEvent("close", str(path)))

    def on_error(self, path, err):
        self._push(RuntimeEvent("error", str(path), error=repr(err), stack=self._stack()))

    # --- patched functions ---
    def _patched_open(self, file, mode="r", *a, **kw):
        t0 = time.perf_counter()
        try:
            fh = self._orig_open(file, mode, *a, **kw)
            dt = (time.perf_counter() - t0) * 1000
            self._push(RuntimeEvent("open", str(file), mode=mode, duration_ms=dt,
                                    stack=self._stack()))
            return fh
        except Exception as e:
            self.on_error(file, e)
            raise

    def _patched_read(self, self_, *a, **kw):
        t0 = time.perf_counter()
        try:
            r = self._orig_io_read(self_, *a, **kw)
            dt = (time.perf_counter() - t0) * 1000
            n = len(r) if isinstance(r, (bytes, bytearray, str)) else 0
            p = getattr(self_, "name", "?")
            self._push(RuntimeEvent("read", str(p), size=n, duration_ms=dt))
            return r
        except Exception as e:
            self.on_error(getattr(self_, "name", "?"), e)
            raise

    def _patched_write(self, self_, *a, **kw):
        t0 = time.perf_counter()
        try:
            r = self._orig_io_write(self_, *a, **kw)
            dt = (time.perf_counter() - t0) * 1000
            n = len(a[0]) if a and isinstance(a[0], (bytes, bytearray, str)) else 0
            p = getattr(self_, "name", "?")
            self._push(RuntimeEvent("write", str(p), size=n, duration_ms=dt))
            return r
        except Exception as e:
            self.on_error(getattr(self_, "name", "?"), e)
            raise

    def _patched_close(self, self_):
        try:
            return self._orig_io_close(self_)
        finally:
            self._push(RuntimeEvent("close", str(getattr(self_, "name", "?"))))

    # --- helpers ---
    def _push(self, ev: RuntimeEvent) -> None:
        with self._lock:
            self._events.append(ev)

    def _stack(self, depth: int = 6) -> str:
        frames = traceback.extract_stack()[:-2][-depth:]
        return " | ".join(f"{f.filename.split(chr(47))[-1]}:{f.lineno}:{f.name}"
                          for f in frames)

    # --- queries ---
    def events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in list(self._events)[-limit:]]

    def stats(self) -> dict:
        with self._lock:
            evs = list(self._events)
        s = {"total": len(evs), "open": 0, "read": 0, "write": 0,
             "close": 0, "error": 0, "bytes_read": 0, "bytes_written": 0,
             "slow_ops": [], "files_touched": set(), "errors": []}
        for e in evs:
            s[e.kind] = s.get(e.kind, 0) + 1
            if e.size:
                if e.kind == "read":  s["bytes_read"] += e.size
                if e.kind == "write": s["bytes_written"] += e.size
            if e.duration_ms > 100:
                s["slow_ops"].append({"path": e.path, "ms": round(e.duration_ms, 1),
                                      "kind": e.kind})
            if e.path: s["files_touched"].add(e.path)
            if e.kind == "error":
                s["errors"].append({"path": e.path, "error": e.error, "stack": e.stack})
        s["files_touched"] = sorted(s["files_touched"])
        s["slow_ops"] = s["slow_ops"][:50]
        s["errors"] = s["errors"][:50]
        return s

    def detect_anomalies(self) -> list[dict]:
        """Ищет утечки, повторные close, ошибки."""
        with self._lock:
            evs = list(self._events)
        anomalies = []
        opens: dict[str, int] = {}
        closes: dict[str, int] = {}
        for e in evs:
            if e.kind == "open":   opens[e.path] = opens.get(e.path, 0) + 1
            if e.kind == "close":  closes[e.path] = closes.get(e.path, 0) + 1
            if e.kind == "error":
                anomalies.append({"type": "runtime_error",
                                  "path": e.path, "detail": e.error})
        for p, n in opens.items():
            c = closes.get(p, 0)
            if n > c:
                anomalies.append({"type": "possible_leak",
                                  "path": p, "open": n, "close": c})
            elif c > n:
                anomalies.append({"type": "double_close",
                                  "path": p, "open": n, "close": c})
        return anomalies
'''


# --- mountfile/olma/rules.py ----------------------------------------------- #
OLMA_RULES = '''\
# MOUNTFILE_FIX — OLMA rules
"""База правил для статического анализа."""

from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    INFO = 1
    WARN = 2
    ERROR = 3
    CRITICAL = 4

    def label(self) -> str:
        return {1: "INFO", 2: "WARN", 3: "ERROR", 4: "CRITICAL"}[int(self)]


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: Severity
    pattern: str       # "open_no_with", "read_no_size", ...
    fixable: bool
    hint: str


RULES: dict[str, Rule] = {
    "OL001": Rule("OL001", "open() без with", Severity.WARN, "open_no_with", True,
                  "Оберните open() в with для гарантированного закрытия"),
    "OL002": Rule("OL002", "read() без размера", Severity.WARN, "read_no_size", True,
                  "Ограничьте size, чтобы не читать весь файл в память"),
    "OL003": Rule("OL003", "seek() без проверки", Severity.INFO, "seek_unchecked", True,
                  "Проверьте границы перед seek()"),
    "OL004": Rule("OL004", "f.read() в цикле без break", Severity.WARN, "read_no_break", False,
                  "Добавьте условие выхода, чтобы избежать бесконечного цикла"),
    "OL005": Rule("OL005", "запись в файл без flush/close", Severity.WARN, "write_no_close", True,
                  "Используйте with или явный flush()"),
    "OL006": Rule("OL006", "захардкоженный путь", Severity.INFO, "hardcoded_path", False,
                  "Вынесите путь в константу или аргумент"),
    "OL007": Rule("OL007", "IO без обработки исключений", Severity.WARN, "io_no_try", True,
                  "Оберните в try/except OSError"),
    "OL008": Rule("OL008", "двойной close", Severity.INFO, "double_close", False,
                  "Проверьте, не закрывается ли handle дважды"),
    "OL009": Rule("OL009", "чтение всего файла в память", Severity.WARN, "read_all", True,
                  "Используйте потоковое чтение chunk'ами"),
    "OL010": Rule("OL010", "проверка границ без инварианта", Severity.ERROR, "manual_bounds", True,
                  "Используйте OlmaBuffer/OlmaView для проверки границ"),
    "OL011": Rule("OL011", "FAT-цепочка без бюджета", Severity.CRITICAL, "chain_no_budget", True,
                  "Используйте OlmaChain с бюджетом итераций"),
    "OL012": Rule("OL012", "XOR без проверки таблицы", Severity.ERROR, "xor_no_check", True,
                  "Проверьте, что таблица не пуста, через OlmaXorDecoder"),
    "OL013": Rule("OL013", "запись неатомарна", Severity.WARN, "write_nonatomic", True,
                  "Используйте OlmaTransaction для atomic write"),
    "OL014": Rule("OL014", "int.from_bytes без проверки длины", Severity.WARN, "from_bytes_unsafe", False,
                  "Проверьте длину среза перед int.from_bytes"),
    "OL015": Rule("OL015", "os.replace без fsync", Severity.INFO, "replace_no_fsync", False,
                  "Добавьте fsync перед replace"),
    "OL016": Rule("OL016", "молчаливый except", Severity.WARN, "silent_except", False,
                  "Логируйте или перебрасывайте исключение"),
    "OL017": Rule("OL017", "print вместо logging", Severity.INFO, "print_instead_logging", False,
                  "Используйте logging для диагностики"),
    "OL018": Rule("OL018", "магическое число в смещении", Severity.INFO, "magic_offset", False,
                  "Вынесите offset в именованную константу"),
    "OL019": Rule("OL019", "close без try/finally", Severity.INFO, "close_no_finally", True,
                  "Оберните close в try/finally или используйте with"),
    "OL020": Rule("OL020", "open в режиме w без backup", Severity.CRITICAL, "open_w_no_backup", True,
                  "Используйте OlmaTransaction, чтобы не потерять данные"),
}
'''


# --- mountfile/olma/analyzer.py -------------------------------------------- #
OLMA_ANALYZER = '''\
# MOUNTFILE_FIX — OLMA analyzer
"""Статический AST-анализ проекта на 20 правил."""

from __future__ import annotations
import ast, os, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .rules import RULES, Rule, Severity


@dataclass
class Issue:
    rule_id: str
    title: str
    severity: int
    file: str
    line: int
    col: int
    snippet: str
    hint: str
    fixable: bool

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id, "title": self.title,
            "severity": self.severity,
            "severity_label": Severity(self.severity).label(),
            "file": self.file, "line": self.line, "col": self.col,
            "snippet": self.snippet, "hint": self.hint, "fixable": self.fixable,
        }


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, src_lines: list[str]) -> None:
        self.path = path
        self.lines = src_lines
        self.issues: list[Issue] = []

    def _emit(self, rule_id: str, node: ast.AST, snippet: str = "") -> None:
        r = RULES[rule_id]
        ln = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0)
        snip = snippet or (self.lines[ln - 1].strip() if 0 < ln <= len(self.lines) else "")
        self.issues.append(Issue(
            rule_id=r.id, title=r.title, severity=int(r.severity),
            file=self.path, line=ln, col=col,
            snippet=snip[:160], hint=r.hint, fixable=r.fixable,
        ))

    # --- visitors ---
    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node)

        # OL001: open без with
        if name == "open":
            parent = getattr(node, "_olma_parent", None)
            if not isinstance(parent, (ast.With, ast.AsyncWith)):
                self._emit("OL001", node)

        # OL002 / OL009: .read() без размера
        if name.endswith(".read"):
            if not node.args and not node.keywords:
                self._emit("OL002", node)
                # .read() без size в выражении, присвоенном переменной → read_all
                self._emit("OL009", node)

        # OL003: seek() без проверки
        if name.endswith(".seek"):
            self._emit("OL003", node)

        # OL013 / OL020: open(..., 'w') без backup
        if name == "open":
            mode = self._mode_arg(node)
            if mode and "w" in mode and "x" not in mode:
                self._emit("OL020", node)
            if "r+" in (mode or "") or "w" in (mode or ""):
                self._emit("OL013", node)

        # OL014: int.from_bytes без slice
        if name == "int.from_bytes":
            src = node.args[0] if node.args else None
            if isinstance(src, ast.Subscript) and isinstance(src.slice, ast.Constant):
                pass
            elif isinstance(src, ast.Name):
                # не можем проверить длину — предупреждаем
                self._emit("OL014", node)

        # OL015: os.replace
        if name == "os.replace":
            self._emit("OL015", node)

        # OL017: print(...)
        if name == "print":
            self._emit("OL017", node)

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # OL016: silent except
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self._emit("OL016", node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        # OL004: while True без break внутри
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(n, ast.Break) for n in ast.walk(node)):
                self._emit("OL004", node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # OL006 / OL018: захардкоженные пути и магические offset
        if isinstance(node.value, str):
            s = node.value
            if re.match(r"^[A-Za-z]:[\\\\/]|^/[/\\\\w]", s) and len(s) < 120:
                # исключаем строки, содержащие пробелы — вероятно текст
                if " " not in s:
                    self._emit("OL006", node, snippet=repr(s))
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            if 0x1000 <= node.value <= 0xFFFFFF and node.value not in (1000, 10000, 100000):
                # вероятный offset
                self._emit("OL018", node, snippet=str(node.value))
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        # OL011: ручной обход FAT-цепочки
        src = ast.unparse(node.iter) if hasattr(ast, "unparse") else ""
        if "fat" in src.lower() and "while" not in src.lower():
            self._emit("OL011", node)
        self.generic_visit(node)

    # --- helpers ---
    def _call_name(self, node: ast.Call) -> str:
        f = node.func
        if isinstance(f, ast.Name): return f.id
        if isinstance(f, ast.Attribute):
            parts, cur = [], f
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr); cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            return ".".join(reversed(parts))
        return ""

    def _mode_arg(self, node: ast.Call):
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return kw.value.value
        return None


def _mark_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._olma_parent = node  # type: ignore[attr-defined]


class OlmaAnalyzer:
    """Сканирует проект и возвращает список Issue."""

    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                 "dist", "build", ".pytest_cache"}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def iter_files(self) -> Iterator[Path]:
        for p in self.root.rglob("*.py"):
            if any(s in p.parts for s in self.SKIP_DIRS):
                continue
            if p.name == "mountfile_fix.py":
                continue
            if "olma" in p.parts and p.name in {
                "core.py", "runtime.py", "rules.py", "analyzer.py",
                "fixer.py", "reporter.py", "cli.py", "__init__.py"
            }:
                continue  # не анализируем сам OLMA
            yield p

    def scan_file(self, path: Path) -> list[Issue]:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(path))
        except (SyntaxError, OSError):
            return []
        _mark_parents(tree)
        v = _Visitor(str(path.relative_to(self.root)), src.splitlines())
        v.visit(tree)
        return v.issues

    def scan(self) -> list[Issue]:
        all_issues: list[Issue] = []
        for p in self.iter_files():
            all_issues.extend(self.scan_file(p))
        all_issues.sort(key=lambda i: (-i.severity, i.file, i.line))
        return all_issues

    def summary(self, issues: list[Issue]) -> dict:
        by_sev = {1: 0, 2: 0, 3: 0, 4: 0}
        by_rule: dict[str, int] = {}
        by_file: dict[str, int] = {}
        for i in issues:
            by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
            by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
            by_file[i.file] = by_file.get(i.file, 0) + 1
        return {
            "total": len(issues),
            "by_severity": {Severity(k).label(): v for k, v in by_sev.items()},
            "by_rule": by_rule,
            "by_file": by_file,
            "fixable": sum(1 for i in issues if i.fixable),
        }
'''


# --- mountfile/olma/fixer.py ----------------------------------------------- #
OLMA_FIXER = '''\
# MOUNTFILE_FIX — OLMA fixer
"""Автоисправление кода: безопасные AST-трансформации."""

from __future__ import annotations
import ast, shutil, textwrap
from dataclasses import dataclass
from pathlib import Path

from .analyzer import Issue


@dataclass
class FixResult:
    file: str
    line: int
    rule_id: str
    applied: bool
    detail: str = ""


class OlmaFixer:
    """Применяет безопасные исправления к исходникам."""

    def __init__(self, root: str | Path, *, dry_run: bool = True) -> None:
        self.root = Path(root).resolve()
        self.dry_run = dry_run

    # --- публичное API ---
    def apply(self, issues: list[Issue]) -> list[FixResult]:
        by_file: dict[str, list[Issue]] = {}
        for i in issues:
            if i.fixable:
                by_file.setdefault(i.file, []).append(i)

        results: list[FixResult] = []
        for rel, file_issues in by_file.items():
            p = self.root / rel
            if not p.exists():
                continue
            results.extend(self._fix_file(p, rel, file_issues))
        return results

    # --- внутренняя логика ---
    def _fix_file(self, p: Path, rel: str, issues: list[Issue]) -> list[FixResult]:
        src = p.read_text(encoding="utf-8", errors="replace")
        lines = src.splitlines(keepends=True)
        results: list[FixResult] = []

        # сортируем по убыванию строки, чтобы смещения не сбивались
        for issue in sorted(issues, key=lambda i: -i.line):
            handler = getattr(self, f"_fix_{issue.rule_id.lower()}", None)
            if handler is None:
                results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                         "нет обработчика"))
                continue
            try:
                new_lines, detail = handler(lines, issue)
                if new_lines is not None and new_lines != lines:
                    lines = new_lines
                    results.append(FixResult(rel, issue.line, issue.rule_id, True, detail))
                else:
                    results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                             detail or "без изменений"))
            except Exception as e:
                results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                         f"ошибка: {e}"))

        new_src = "".join(lines)
        if new_src != src and not self.dry_run:
            bak = Path(str(p) + ".olma.bak")
            if not bak.exists():
                shutil.copy2(p, bak)
            p.write_text(new_src, encoding="utf-8")

        return results

    # --- конкретные fix-хендлеры ---
    def _fix_ol001(self, lines, issue):
        """open() без with → with open() as _f: ... (оборачиваем одну строку)."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        # Простейший случай: var = open(...)
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        if "=" in stripped and "open(" in stripped:
            var, expr = stripped.split("=", 1)
            var = var.strip()
            new = f"{indent}with {expr.strip()} as {var}:\\n"
            # добавляем TODO — надо перенести тело внутрь with
            new += f"{indent}    pass  # OLMA: перенесите тело сюда\\n"
            new += f"{indent}# OLMA-TODO: закрыть with-блок для {var}\\n"
            lines[idx] = new
            return lines, "обёрнуто в with (нужно вручную перенести тело)"
        return None, "не поддержано"

    def _fix_ol002(self, lines, issue):
        """read() → read(size=1<<20)."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        if ".read()" in line:
            lines[idx] = line.replace(".read()", ".read(1 << 20)", 1)
            return lines, "добавлен size=1MiB"
        return None, ""

    def _fix_ol005(self, lines, issue):
        return self._fix_ol001(lines, issue)

    def _fix_ol007(self, lines, issue):
        """Оборачивает строку в try/except OSError."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        indent = line[:len(line) - len(line.lstrip())]
        body = line[len(indent):]
        new = (f"{indent}try:\\n"
               f"{indent}    {body}"
               f"{indent}except OSError as _e:\\n"
               f"{indent}    raise  # OLMA: обработайте ошибку\\n")
        lines[idx] = new
        return lines, "добавлен try/except OSError"

    def _fix_ol010(self, lines, issue):
        """Рекомендация — ручная замена на OlmaBuffer."""
        return None, "требует ручной замены на OlmaBuffer/OlmaView"

    def _fix_ol011(self, lines, issue):
        """Подсказка — ручная замена на OlmaChain."""
        return None, "требует ручной замены на OlmaChain"

    def _fix_ol012(self, lines, issue):
        return None, "требует ручной замены на OlmaXorDecoder"

    def _fix_ol013(self, lines, issue):
        return None, "требует ручной замены на OlmaTransaction"

    def _fix_ol019(self, lines, issue):
        """close() → try/finally."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        indent = line[:len(line) - len(line.lstrip())]
        body = line[len(indent):]
        new = (f"{indent}try:\\n"
               f"{indent}    {body}"
               f"{indent}finally:\\n"
               f"{indent}    pass  # OLMA: убедитесь, что ресурс закрыт\\n")
        lines[idx] = new
        return lines, "добавлен try/finally"

    def _fix_ol020(self, lines, issue):
        """open(..., 'w') → OlmaTransaction (рекомендация)."""
        return None, "требует ручной замены на OlmaTransaction"
'''


# --- mountfile/olma/reporter.py -------------------------------------------- #
OLMA_REPORTER = '''\
# MOUNTFILE_FIX — OLMA reporter
"""Отчёты: json / markdown / html / text."""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

from .analyzer import Issue
from .rules import Severity


class OlmaReporter:
    def __init__(self, issues: list[Issue], trace_stats: dict | None = None) -> None:
        self.issues = issues
        self.trace = trace_stats or {}
        self.ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def render(self, *, fmt: str = "text", out: str | None = None) -> str:
        fn = getattr(self, f"_render_{fmt}", None)
        if fn is None:
            raise ValueError(f"unsupported format: {fmt}")
        text = fn()
        if out:
            Path(out).write_text(text, encoding="utf-8")
        return text

    # --- formatters ---
    def _summary(self) -> dict:
        by_sev = {"INFO": 0, "WARN": 0, "ERROR": 0, "CRITICAL": 0}
        by_rule: dict[str, int] = {}
        for i in self.issues:
            by_sev[Severity(i.severity).label()] += 1
            by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
        return {"total": len(self.issues), "by_severity": by_sev, "by_rule": by_rule}

    def _render_text(self) -> str:
        s = self._summary()
        lines = [
            f"OLMA report — {self.ts}",
            f"Всего проблем: {s['total']}",
            "  " + " ".join(f"{k}={v}" for k, v in s["by_severity"].items()),
            "",
            "По правилам:",
        ]
        for r, n in sorted(s["by_rule"].items(), key=lambda x: -x[1]):
            lines.append(f"  {r}: {n}")
        lines.append("")
        for i in self.issues[:100]:
            tag = Severity(i.severity).label()
            lines.append(f"[{tag}] {i.rule_id} {i.file}:{i.line} — {i.title}")
            if i.snippet:
                lines.append(f"      > {i.snippet}")
            lines.append(f"      fix: {i.hint}")
        if len(self.issues) > 100:
            lines.append(f"... и ещё {len(self.issues) - 100}")
        if self.trace:
            lines.append("")
            lines.append("Runtime trace:")
            lines.append(f"  events={self.trace.get('total', 0)} "
                         f"read={self.trace.get('read', 0)} "
                         f"write={self.trace.get('write', 0)} "
                         f"errors={self.trace.get('error', 0)}")
        return "\\n".join(lines)

    def _render_json(self) -> str:
        return json.dumps({
            "ts": self.ts,
            "summary": self._summary(),
            "issues": [i.to_dict() for i in self.issues],
            "trace": self.trace,
        }, indent=2, ensure_ascii=False)

    def _render_markdown(self) -> str:
        s = self._summary()
        out = [f"# OLMA report ({self.ts})", "",
               f"**Всего:** {s['total']}  "]
        for k, v in s["by_severity"].items():
            out.append(f"**{k}:** {v}  ")
        out += ["", "## По правилам", ""]
        for r, n in sorted(s["by_rule"].items(), key=lambda x: -x[1]):
            out.append(f"- `{r}` — {n}")
        out += ["", "## Проблемы", "",
                "| Sev | Rule | File | Line | Title |",
                "|-----|------|------|------|-------|"]
        for i in self.issues[:200]:
            out.append(f"| {Severity(i.severity).label()} | `{i.rule_id}` | "
                       f"`{i.file}` | {i.line} | {i.title} |")
        return "\\n".join(out)

    def _render_html(self) -> str:
        s = self._summary()
        rows = []
        for i in self.issues[:500]:
            sev = Severity(i.severity).label()
            cls = {"INFO": "i", "WARN": "w", "ERROR": "e", "CRITICAL": "c"}[sev]
            rows.append(
                f"<tr class='{cls}'><td>{sev}</td><td>{i.rule_id}</td>"
                f"<td>{i.file}</td><td>{i.line}</td>"
                f"<td>{_html(i.title)}</td><td>{_html(i.snippet)}</td>"
                f"<td>{_html(i.hint)}</td></tr>"
            )
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>OLMA Report</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f7f8fa}}
h1{{color:#222}} .sum{{display:flex;gap:1rem;margin:1rem 0}}
.badge{{padding:.4rem .8rem;border-radius:6px;color:#fff}}
.badge.INFO{{background:#3498db}} .badge.WARN{{background:#f39c12}}
.badge.ERROR{{background:#e74c3c}} .badge.CRITICAL{{background:#8e44ad}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{padding:.5rem .7rem;text-align:left;border-bottom:1px solid #eee;font-size:.9rem}}
th{{background:#2c3e50;color:#fff}} tr.c{{background:#f4e6ff}}
tr.e{{background:#ffe6e6}} tr.w{{background:#fff8e1}} tr.i{{background:#eef6ff}}
code{{background:#f0f0f0;padding:.1rem .3rem;border-radius:3px}}
</style></head><body>
<h1>OLMA Report — {self.ts}</h1>
<div class="sum">
<div class="badge INFO">INFO {s['by_severity']['INFO']}</div>
<div class="badge WARN">WARN {s['by_severity']['WARN']}</div>
<div class="badge ERROR">ERROR {s['by_severity']['ERROR']}</div>
<div class="badge CRITICAL">CRITICAL {s['by_severity']['CRITICAL']}</div>
</div>
<h2>Всего проблем: {s['total']}</h2>
<table><thead><tr>
<th>Sev</th><th>Rule</th><th>File</th><th>Line</th>
<th>Title</th><th>Snippet</th><th>Hint</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""


def _html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
'''


# --- mountfile/olma/cli.py ------------------------------------------------- #
OLMA_CLI = '''\
# MOUNTFILE_FIX — OLMA CLI
"""python main.py --olma <cmd>"""

from __future__ import annotations
import argparse, sys
from pathlib import Path


def olma_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olma", description="OLMA analysis & fix")
    ap.add_argument("cmd", choices=["analyze", "fix", "report", "trace",
                                    "watch", "rules", "install", "uninstall"],
                    help="команда")
    ap.add_argument("--root", default=None, help="корень проекта")
    ap.add_argument("--format", default="text", choices=["text", "json", "md", "html"])
    ap.add_argument("--out", default=None, help="файл отчёта")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true", help="применить исправления")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-severity", default="INFO",
                    choices=["INFO", "WARN", "ERROR", "CRITICAL"])
    args = ap.parse_args(argv)

    from . import olma
    from .rules import RULES, Severity

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]

    if args.cmd == "rules":
        for rid, r in sorted(RULES.items()):
            print(f"{rid}  [{r.severity.label():8}] {r.title}"
                  f"{'  (fixable)' if r.fixable else ''}")
            print(f"       {r.hint}")
        return 0

    if args.cmd == "install":
        olma.install(trace_files=True, trace_memory=True)
        print("OLMA runtime installed")
        return 0

    if args.cmd == "uninstall":
        olma.uninstall()
        print("OLMA runtime uninstalled")
        return 0

    if args.cmd == "analyze":
        issues = olma.analyze(str(root))
        min_sev = {"INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}[args.min_severity]
        issues = [i for i in issues if i.severity >= min_sev]
        from .reporter import OlmaReporter
        rep = OlmaReporter(issues[:args.limit])
        print(rep.render(fmt=args.format, out=args.out))
        return 0

    if args.cmd == "fix":
        results = olma.fix(dry_run=not args.apply, root=str(root))
        ok = sum(1 for r in results if r.applied)
        print(f"Fix results: applied={ok}, total={len(results)}")
        for r in results[:args.limit]:
            mark = "OK " if r.applied else "SKIP"
            print(f"  [{mark}] {r.file}:{r.line} {r.rule_id} — {r.detail}")
        if not args.apply:
            print("\\nЭто dry-run. Запустите с --apply, чтобы применить.")
        return 0

    if args.cmd == "report":
        text = olma.report(fmt=args.format, out=args.out, root=str(root))
        if not args.out:
            print(text)
        else:
            print(f"Report written: {args.out}")
        return 0

    if args.cmd == "trace":
        olma.install(trace_files=True, trace_memory=True)
        st = olma.trace_stats()
        print(f"OLMA trace installed. Stats: {st}")
        return 0

    if args.cmd == "watch":
        olma.install(trace_files=True, trace_memory=True)
        print("OLMA watching. Нажмите Ctrl+C для отчёта.")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        st = olma.trace_stats()
        print(f"\\nИТОГО: events={st.get('total', 0)} "
              f"read={st.get('read', 0)} write={st.get('write', 0)} "
              f"errors={st.get('error', 0)}")
        anomalies = olma._runtime.detect_anomalies() if olma._runtime else []
        for a in anomalies[:20]:
            print(f"  ! {a}")
        return 0

    return 1
'''


# =========================================================================== #
# ФАЙЛЫ ДЛЯ СОЗДАНИЯ
# =========================================================================== #

OLMA_FILES = {
    "mountfile/olma/__init__.py": OLMA_INIT,
    "mountfile/olma/core.py": OLMA_CORE,
    "mountfile/olma/runtime.py": OLMA_RUNTIME,
    "mountfile/olma/rules.py": OLMA_RULES,
    "mountfile/olma/analyzer.py": OLMA_ANALYZER,
    "mountfile/olma/fixer.py": OLMA_FIXER,
    "mountfile/olma/reporter.py": OLMA_REPORTER,
    "mountfile/olma/cli.py": OLMA_CLI,
}


def create_olma_package(root: Path, *, dry: bool) -> int:
    created = 0
    pkg = root / "mountfile" / "olma"
    if dry:
        print(f"  [dry] создам пакет {pkg} ({len(OLMA_FILES)} файлов)")
        return len(OLMA_FILES)
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, content in OLMA_FILES.items():
        p = root / rel
        if p.exists():
            backup(p, dry=False)
        p.write_text(content, encoding="utf-8")
        print(f"  [created] {rel} ({len(content.splitlines())} строк)")
        created += 1
    return created


# =========================================================================== #
# ПАТЧИ СУЩЕСТВУЮЩИХ МОДУЛЕЙ
# =========================================================================== #

OLMA_INTEGRATION_BLOCK = """
# --- MOUNTFILE_FIX: OLMA integration ---------------------------------------
try:
    from mountfile.olma import olma as _olma
    _olma.install(trace_files=True, trace_memory=False)
except Exception as _olma_err:  # noqa: BLE001
    _olma = None
    import sys as _sys
    print(f"[OLMA] init warning: {_olma_err}", file=_sys.stderr)
# --- /MOUNTFILE_FIX --------------------------------------------------------
"""


def patch_main(text: str) -> str:
    """Добавляет --olma <cmd>, --diag и автоинициализацию OLMA."""
    if "_olma_main" in text:
        return text

    helper = """

# --- MOUNTFILE_FIX: OLMA CLI -----------------------------------------------

def _olma_main(argv=None) -> int:
    from mountfile.olma.cli import olma_main
    return olma_main(argv)


def _mf_diag() -> int:
    import platform, sys as _sys
    try:
        from mountfile import __version__ as _v
    except Exception:
        _v = "unknown"
    print("MountFile diag (OLMA)")
    print(f"  python:   {_sys.version.split()[0]}")
    print(f"  platform: {platform.platform()}")
    print(f"  version:  {_v}")
    try:
        from mountfile import qt_compat
        print(f"  qt:       {qt_compat.QT_VERSION}")
    except Exception as e:
        print(f"  qt:       NOT AVAILABLE ({e!r})")
    try:
        from mountfile.olma import olma
        print(f"  olma:     OK")
    except Exception as e:
        print(f"  olma:     FAIL ({e!r})")
    try:
        import pytsk3
        print(f"  pytsk3:   {getattr(pytsk3, '__version__', 'ok')}")
    except Exception:
        print("  pytsk3:   NOT AVAILABLE")
    return 0

# --- /MOUNTFILE_FIX --------------------------------------------------------
"""
    text = insert_after_imports(text, helper)

    # Автоинициализация OLMA после импортов
    text = insert_after_imports(text, OLMA_INTEGRATION_BLOCK)

    # Обработка --olma и --diag в main
    if 'if __name__ == "__main__":' in text and "--olma" not in text:
        text = text.replace(
            'if __name__ == "__main__":',
            'if __name__ == "__main__":\n'
            "    import sys as _sys\n"
            '    if "--diag" in _sys.argv:\n'
            "        raise SystemExit(_mf_diag())\n"
            '    if "--olma" in _sys.argv:\n'
            '        idx = _sys.argv.index("--olma")\n'
            "        raise SystemExit(_olma_main(_sys.argv[idx + 1:]))",
            1,
        )
    return text


def patch_qt_compat(text: str) -> str:
    if "MOUNTFILE_FIX" in text:
        return text
    content = '''\
# MOUNTFILE_FIX — auto-patched + OLMA
"""qt_compat.py — унифицированный слой PyQt5/PyQt6."""

from __future__ import annotations
IS_QT6 = False
QT_VERSION = "none"
_IMPORT_ERROR = None

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QKeySequence, QAction, QIcon
    IS_QT6 = True
    QT_VERSION = "PyQt6"
except Exception as e6:
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot
        from PyQt5.QtGui import QKeySequence, QIcon
        from PyQt5.QtWidgets import QAction
        IS_QT6 = False
        QT_VERSION = "PyQt5"
    except Exception as e5:
        _IMPORT_ERROR = f"PyQt6: {e6!r}; PyQt5: {e5!r}"


def is_qt_available() -> bool:
    return _IMPORT_ERROR is None


def require_qt() -> None:
    if not is_qt_available():
        raise RuntimeError(f"Qt not found. {_IMPORT_ERROR}")


__all__ = ["QtCore", "QtGui", "QtWidgets", "Qt", "pyqtSignal", "pyqtSlot",
           "QKeySequence", "QAction", "QIcon",
           "IS_QT6", "QT_VERSION", "is_qt_available", "require_qt"]
'''
    return content


def patch_fat32(text: str) -> str:
    if "OlmaChain" in text:
        return text
    block = """
# --- MOUNTFILE_FIX: OLMA FAT32 helpers -------------------------------------
try:
    from mountfile.olma.core import (
        OlmaChain, OlmaCluster, FAT32_MAX_CLUSTER, FAT32_EOC_MIN, FAT32_BAD,
        OlmaBoundsError, OlmaIntegrityError,
    )
except Exception:
    OlmaChain = None

def _check_cluster_range(c, mc=FAT32_MAX_CLUSTER):
    try:
        OlmaCluster(c, mc); return True
    except Exception:
        return False

def _is_eoc(v): return v >= FAT32_EOC_MIN
def _is_bad(v): return v == FAT32_BAD

def _iter_chain(start, fat_get, mc=FAT32_MAX_CLUSTER):
    if OlmaChain is None:
        cur = start
        while _check_cluster_range(cur, mc):
            yield cur
            cur = fat_get(cur)
            if _is_eoc(cur) or _is_bad(cur) or cur == 0:
                break
        return
    for c in OlmaChain(start, fat_get, mc):
        yield c.value

def _safe_free_chain(fat_get, fat_set, start, mc=FAT32_MAX_CLUSTER):
    freed = 0
    try:
        for v in _iter_chain(start, fat_get, mc):
            fat_set(v, 0); freed += 1
    except Exception:
        pass
    return freed
# --- /MOUNTFILE_FIX --------------------------------------------------------
"""
    return insert_after_imports(text, block)


def patch_partition(text: str) -> str:
    if "OlmaPartitionTable" in text:
        return text
    block = """
# --- MOUNTFILE_FIX: OLMA partition helpers ---------------------------------
try:
    from mountfile.olma.core import (
        OlmaPartitionTable, OlmaBuffer, OlmaPartition, OlmaError,
    )
except Exception:
    OlmaPartitionTable = None

def _validate_mbr(buf):
    if OlmaPartitionTable is None:
        return len(buf) >= 512 and buf[510:512] == b"\\x55\\xAA"
    try:
        return OlmaPartitionTable(OlmaBuffer(buf)).kind in ("mbr", "gpt")
    except OlmaError:
        return False

def _validate_gpt(buf):
    if OlmaPartitionTable is None:
        return len(buf) >= 1024 and buf[512:520] == b"EFI PART"
    try:
        return OlmaPartitionTable(OlmaBuffer(buf)).kind == "gpt"
    except OlmaError:
        return False

def parse_partitions_safe(buf):
    if OlmaPartitionTable is None:
        return []
    try:
        return OlmaPartitionTable(OlmaBuffer(buf)).partitions
    except OlmaError:
        return []

def _clamp_partition(start, size, total):
    p = OlmaPartition(0, start, size).clamp(total)
    return p.start_sector, p.size_sectors
# --- /MOUNTFILE_FIX --------------------------------------------------------
"""
    return insert_after_imports(text, block)


def patch_vfs(text: str) -> str:
    if "safe_read" in text and "OlmaHandle" in text:
        return text
    block = """
# --- MOUNTFILE_FIX: OLMA safe read -----------------------------------------
try:
    from mountfile.olma.core import OlmaHandle, OlmaError
except Exception:
    OlmaHandle = None

def safe_read(fh, offset, size, *, chunk=1 << 20):
    if OlmaHandle is not None and isinstance(fh, OlmaHandle):
        return fh.read_upto(offset, size)
    if size <= 0 or offset < 0:
        return b""
    out = bytearray()
    fh.seek(offset)
    remaining = size
    while remaining > 0:
        part = fh.read(min(chunk, remaining))
        if not part: break
        out.extend(part); remaining -= len(part)
    return bytes(out)

def safe_read_exact(fh, offset, size):
    if OlmaHandle is not None and isinstance(fh, OlmaHandle):
        try: return fh.read_at(offset, size)
        except OlmaError: return None
    d = safe_read(fh, offset, size)
    return d if len(d) == size else None
# --- /MOUNTFILE_FIX --------------------------------------------------------
"""
    return insert_after_imports(text, block)


def patch_updater(text: str) -> str:
    if "_safe_download" in text:
        return text
    block = """
# --- MOUNTFILE_FIX: OLMA safe updater --------------------------------------
try:
    from mountfile.olma.core import (
        safe_download as _olma_dl, verify_sha256 as _olma_sha,
        OlmaTransaction as _OlmaTx,
    )
except Exception:
    _olma_dl = _olma_sha = _OlmaTx = None

def _safe_download(url, *, timeout=30.0, max_size=512 * 1024 * 1024):
    if _olma_dl is None:
        import urllib.request
        return urllib.request.urlopen(url, timeout=timeout).read()
    return _olma_dl(url, timeout=timeout, max_size=max_size)

def _verify_sha256(data, expected):
    if _olma_sha is None: return True
    return _olma_sha(data, expected)

def _atomic_write(path, data):
    if _OlmaTx is None:
        with open(path, "wb") as f: f.write(data)
        return
    with _OlmaTx(path) as tx: tx.write_all(data)
# --- /MOUNTFILE_FIX --------------------------------------------------------
"""
    return insert_after_imports(text, block)


def patch_requirements(root: Path, *, dry: bool) -> bool:
    p = root / "requirements.txt"
    if not p.exists():
        return False
    t = read(p)
    if MARKER in t:
        print(f"  [ok] {p.name}")
        return False
    out = []
    for ln in t.splitlines():
        if ln.strip().startswith("pytsk3"):
            out.append("pytsk3>=20231030 ; python_version >= '3.10'")
        else:
            out.append(ln)
    # OLMA не требует внешних зависимостей
    if "no external deps" not in t:
        out.append("# OLMA: без внешних зависимостей (stdlib only)")
    new = insert_header("\\n".join(out) + "\\n", f"{MARKER} — auto-patched + OLMA\\n")
    backup(p, dry=dry)
    write(p, new, dry=dry)
    print(f"  [fixed] {p.name}")
    return True


# =========================================================================== #
# ОРКЕСТРАЦИЯ
# =========================================================================== #


def apply_all(root: Path, *, dry: bool, force: bool) -> int:
    print(f"Корень проекта: {root}")
    n = 0

    print("\\n[1/3] Создание пакета OLMA...")
    n += create_olma_package(root, dry=dry)

    print("\\n[2/3] Патч существующих модулей...")
    text_patches = [
        ("mountfile/qt_compat.py", patch_qt_compat),
        ("mountfile/partition.py", patch_partition),
        ("mountfile/fat32.py", patch_fat32),
        ("mountfile/vfs.py", patch_vfs),
        ("mountfile/updater.py", patch_updater),
        ("main.py", patch_main),
    ]
    for rel, fn in text_patches:
        p = root / rel
        if patch_file(p, dry=dry, force=force, transform=fn):
            n += 1

    print("\\n[3/3] requirements.txt...")
    try:
        if patch_requirements(root, dry=dry):
            n += 1
    except Exception as e:
        print(f"  [err] {e}")

    print(f"\\nИтого изменений: {n}")
    return n


def check_all(root: Path) -> int:
    print(f"Проверка синтаксиса в: {root}")
    bad = 0
    for p in sorted(root.rglob("*.py")):
        if p.name == Path(__file__).name:
            continue
        if not syntax_ok(p) or not compile_file(p):
            bad += 1
    print("  Все файлы корректны." if bad == 0 else f"  Проблем: {bad}")
    return bad


def revert_all(root: Path) -> int:
    print(f"Откат из .bak: {root}")
    n = 0
    for bak in list(root.rglob("*" + BAK)) + list(root.rglob("*.olma.bak")):
        orig = Path(str(bak).replace(".olma.bak", "").replace(BAK, ""))
        if not orig.exists():
            continue
        try:
            shutil.copy2(bak, orig)
            print(f"  [reverted] {orig.relative_to(root)}")
            n += 1
        except Exception as e:
            print(f"  [err] {bak}: {e}")
    print(f"  Восстановлено: {n}")
    return n


def run_smoke(root: Path) -> int:
    print("Smoke-тест (OLMA full)...")
    tests = [
        ["python", "-c", "from mountfile.olma import olma; " "print('OLMA import OK')"],
        [
            "python",
            "-c",
            "from mountfile.olma import olma; "
            "iss = olma.analyze(); "
            "print(f'OLMA analyze: {len(iss)} issues')",
        ],
        [
            "python",
            "-c",
            "from mountfile.olma import olma; "
            "print('OLMA report:', len(olma.report(fmt='text')))",
        ],
        ["python", "main.py", "--diag"],
        ["python", "main.py", "--olma", "rules"],
    ]
    rc = 0
    for cmd in tests:
        try:
            r = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, timeout=90
            )
            ok = r.returncode == 0
            print(
                f"  [{'ok' if ok else 'warn'}] {' '.join(cmd[:3])} -> RC={r.returncode}"
            )
            if not ok:
                if r.stderr:
                    print("    err:", r.stderr.strip().splitlines()[-1])
                if rc == 0:
                    rc = r.returncode
        except subprocess.TimeoutExpired:
            print(f"  [timeout] {' '.join(cmd[:3])}")
            rc = 1
        except FileNotFoundError:
            print("  [skip] python не найден")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mount File + OLMA full integration")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else find_root(Path.cwd())

    if args.revert:
        return 0 if revert_all(root) >= 0 else 1
    if args.check:
        return 1 if check_all(root) > 0 else 0

    apply_all(root, dry=args.dry_run, force=args.force)
    if args.dry_run:
        return 0

    bad = check_all(root)
    if bad > 0:
        print("\\nВНИМАНИЕ: синтаксические ошибки. Откат: --revert")
        return 1

    if args.smoke:
        rc = run_smoke(root)
        if rc != 0:
            return rc

    print("\\nГотово. Дальше:")
    print("  python main.py --olma analyze        # статический анализ")
    print("  python main.py --olma analyze --format html --out report.html")
    print("  python main.py --olma fix            # dry-run исправлений")
    print("  python main.py --olma fix --apply    # применить")
    print("  python main.py --olma report --format md")
    print("  python main.py --olma watch          # runtime-трассировка")
    print("  python main.py --diag                # диагностика")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- сохранено из ветки #39 при восстановлении ---


def _insert_after_imports(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    last = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(("import ", "from ")) or s.startswith("#") or not s:
            last = i
        else:
            break
    lines.insert(last + 1, block)
    return "".join(lines)


def _insert_header(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    at = 0
    for i, ln in enumerate(lines[:3]):
        if ln.startswith("#!") or "coding" in ln or "encoding" in ln:
            at = i + 1
    lines.insert(at, header)
    return "".join(lines)


def create_olma_module(root: Path, *, dry_run: bool) -> bool:
    p = root / "mountfile" / "olma.py"
    if dry_run:
        print(f"  [dry-run] создал бы {p}")
        return True
    if p.exists():
        backup(p, dry_run=False)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(OLMA_TEMPLATE, encoding="utf-8")
    print(f"  [created] {p.name} ({len(OLMA_TEMPLATE.splitlines())} строк)")
    return True


def create_qt_compat(root: Path, *, dry_run: bool) -> bool:
    p = root / "mountfile" / "qt_compat.py"
    if dry_run:
        print(f"  [dry-run] перезапишу {p}")
        return True
    backup(p, dry_run=False)
    p.write_text(QT_COMPAT_TEMPLATE, encoding="utf-8")
    print(f"  [fixed] {p.name} (канонический слой Qt)")
    return True


def find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(10):
        if (cur / "main.py").exists() and (cur / "mountfile").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


# --- модульные константы (шаблоны) из ветки #39, потеряны при слиянии ---

OLMA_TEMPLATE = '''\
# MOUNTFILE_FIX — OLMA (Object-Level Memory Access) layer
"""
olma.py — безопасный слой доступа к памяти и файлам для Mount File.

Ключевые объекты:
    OlmaError                 — базовое исключение слоя
    OlmaBoundsError           — выход за границы
    OlmaIntegrityError        — нарушение целостности

    OlmaHandle                — RAII-обёртка над файловым дескриптором
    OlmaView                  — read-only окно в файл с явным size
    OlmaBuffer                — буфер с проверкой границ на __getitem__
    OlmaCluster               — кластер FAT32 с инвариантом 2..max-1
    OlmaChain                 — обход цепочки кластеров с бюджетом
    OlmaPartition             — раздел с валидацией границ
    OlmaPartitionTable        — MBR/GPT с проверкой сигнатур
    OlmaTransaction           — атомарная запись (commit/rollback)
    OlmaXorDecoder            — безопасная XOR-расшифровка (RT809H)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import BinaryIO, Iterable, Iterator, Optional


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #

class OlmaError(Exception):
    """Базовое исключение OLMA-слоя."""


class OlmaBoundsError(OlmaError):
    """Попытка доступа за границами объекта."""


class OlmaIntegrityError(OlmaError):
    """Нарушена целостность структуры (сигнатура, размер, хэш)."""


class OlmaStateError(OlmaError):
    """Операция недопустима в текущем состоянии объекта."""


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

SECTOR = 512
MAX_PARTITIONS = 128
FAT32_MAX_CLUSTER = 0x0FFFFFF5
FAT32_EOC_MIN = 0x0FFFFFF8
FAT32_BAD = 0x0FFFFFF7
DEFAULT_CHUNK = 1 << 20
DEFAULT_MAX_DOWNLOAD = 512 * 1024 * 1024


# --------------------------------------------------------------------------- #
# OlmaHandle — RAII над файлом
# --------------------------------------------------------------------------- #

class OlmaHandle:
    """
    RAII-обёртка над бинарным файлом.
    Гарантирует закрытие дескриптора даже при исключении.
    """

    __slots__ = ("_fh", "_path", "_size", "_closed")

    def __init__(self, path: str | os.PathLike, mode: str = "rb") -> None:
        self._path = os.fspath(path)
        self._fh: BinaryIO = open(self._path, mode)
        self._closed = False
        try:
            self._fh.seek(0, os.SEEK_END)
            self._size = self._fh.tell()
            self._fh.seek(0)
        except OSError:
            self._size = 0

    # --- свойства
    @property
    def path(self) -> str:
        return self._path

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    # --- чтение
    def read_at(self, offset: int, size: int) -> bytes:
        """Читает ровно size байт или бросает OlmaBoundsError."""
        if self._closed:
            raise OlmaStateError("handle закрыт")
        if size < 0:
            raise OlmaBoundsError(f"отрицательный size: {size}")
        if offset < 0:
            raise OlmaBoundsError(f"отрицательный offset: {offset}")
        if offset + size > self._size:
            raise OlmaBoundsError(
                f"чтение {offset}+{size} за границей файла ({self._size})"
            )
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise OlmaIntegrityError(
                f"короткое чтение: {len(data)} != {size}"
            )
        return data

    def read_upto(self, offset: int, size: int) -> bytes:
        """Читает не более size; допускает короткое чтение в конце."""
        if self._closed:
            raise OlmaStateError("handle закрыт")
        if size <= 0 or offset >= self._size:
            return b""
        offset = max(0, offset)
        self._fh.seek(offset)
        return self._fh.read(min(size, self._size - offset))

    # --- запись (только в режимах с записью)
    def write_at(self, offset: int, data: bytes) -> None:
        if self._closed:
            raise OlmaStateError("handle закрыт")
        if self._fh.mode == "rb":
            raise OlmaStateError("handle открыт только для чтения")
        if offset < 0:
            raise OlmaBoundsError(f"отрицательный offset: {offset}")
        self._fh.seek(offset)
        self._fh.write(data)
        self._fh.flush()

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def close(self) -> None:
        if not self._closed:
            try:
                self._fh.close()
            finally:
                self._closed = True

    # --- контекст
    def __enter__(self) -> "OlmaHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# OlmaView — read-only окно в файл
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OlmaView:
    """
    Окно в OlmaHandle: фиксированные base_offset и length.
    Все обращения — относительно окна, не файла.
    """
    handle: OlmaHandle
    base_offset: int
    length: int

    def __post_init__(self) -> None:
        if self.base_offset < 0:
            raise OlmaBoundsError("base_offset < 0")
        if self.length < 0:
            raise OlmaBoundsError("length < 0")
        if self.base_offset + self.length > self.handle.size:
            raise OlmaBoundsError(
                f"окно {self.base_offset}+{self.length} вне файла "
                f"({self.handle.size})"
            )

    @property
    def size(self) -> int:
        return self.length

    def read(self, rel_offset: int, size: int) -> bytes:
        if rel_offset < 0 or size < 0:
            raise OlmaBoundsError("отрицательные rel_offset/size")
        if rel_offset + size > self.length:
            raise OlmaBoundsError(
                f"чтение {rel_offset}+{size} за границей окна ({self.length})"
            )
        return self.handle.read_at(self.base_offset + rel_offset, size)

    def read_upto(self, rel_offset: int, size: int) -> bytes:
        if rel_offset < 0:
            rel_offset = 0
        if rel_offset >= self.length or size <= 0:
            return b""
        size = min(size, self.length - rel_offset)
        return self.handle.read_at(self.base_offset + rel_offset, size)

    def subview(self, rel_offset: int, length: int) -> "OlmaView":
        if rel_offset < 0 or length < 0 or rel_offset + length > self.length:
            raise OlmaBoundsError("subview вне окна")
        return OlmaView(self.handle, self.base_offset + rel_offset, length)


# --------------------------------------------------------------------------- #
# OlmaBuffer — буфер с проверкой границ
# --------------------------------------------------------------------------- #

class OlmaBuffer:
    """
    Буфер в памяти с проверкой границ на каждом обращении.
    Используется для чтения таблиц FAT/MBR/GPT.
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray) -> None:
        self._data = bytes(data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._data[key]  # срез безопасен
        if not isinstance(key, int):
            raise TypeError(f"неверный тип ключа: {type(key).__name__}")
        if key < 0 or key >= len(self._data):
            raise OlmaBoundsError(f"индекс {key} вне [0, {len(self._data)})")
        return self._data[key]

    def slice(self, start: int, size: int) -> bytes:
        if start < 0 or size < 0 or start + size > len(self._data):
            raise OlmaBoundsError(
                f"slice {start}+{size} вне [0, {len(self._data)})"
            )
        return self._data[start:start + size]

    def u8(self, off: int) -> int:
        return self.slice(off, 1)[0]

    def u16le(self, off: int) -> int:
        return int.from_bytes(self.slice(off, 2), "little")

    def u32le(self, off: int) -> int:
        return int.from_bytes(self.slice(off, 4), "little")

    def u64le(self, off: int) -> int:
        return int.from_bytes(self.slice(off, 8), "little")

    def raw(self) -> bytes:
        return self._data

    def sha256(self) -> str:
        return hashlib.sha256(self._data).hexdigest()


# --------------------------------------------------------------------------- #
# OlmaCluster — кластер FAT32 с инвариантом
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OlmaCluster:
    """Кластер FAT32: значение в допустимом диапазоне."""
    value: int
    max_cluster: int = FAT32_MAX_CLUSTER

    def __post_init__(self) -> None:
        if not (2 <= self.value < self.max_cluster):
            raise OlmaBoundsError(
                f"кластер {self.value:#x} вне [2, {self.max_cluster:#x})"
            )

    @property
    def is_eoc(self) -> bool:
        return self.value >= FAT32_EOC_MIN

    @property
    def is_bad(self) -> bool:
        return self.value == FAT32_BAD

    def next_from(self, fat_get) -> Optional["OlmaCluster"]:
        nxt = fat_get(self.value)
        if nxt == 0 or nxt >= FAT32_EOC_MIN or nxt == FAT32_BAD:
            return None
        try:
            return OlmaCluster(nxt, self.max_cluster)
        except OlmaBoundsError:
            return None


class OlmaChain:
    """
    Обход цепочки кластеров FAT32 с бюджетом и защитой от циклов.
    """

    def __init__(self, start: int, fat_get, max_cluster: int = FAT32_MAX_CLUSTER,
                 budget: int = 1_000_000) -> None:
        self._start = OlmaCluster(start, max_cluster)
        self._fat_get = fat_get
        self._max = max_cluster
        self._budget = budget

    def __iter__(self) -> Iterator[OlmaCluster]:
        seen: set[int] = set()
        cur: Optional[OlmaCluster] = self._start
        left = self._budget
        while cur is not None and left > 0:
            if cur.value in seen:
                raise OlmaIntegrityError(
                    f"цикл в цепочке кластеров на {cur.value:#x}"
                )
            seen.add(cur.value)
            yield cur
            left -= 1
            cur = cur.next_from(self._fat_get)
        if left <= 0:
            raise OlmaIntegrityError("исчерпан бюджет обхода цепочки")


# --------------------------------------------------------------------------- #
# OlmaPartition + OlmaPartitionTable
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OlmaPartition:
    """Раздел с валидированными границами."""
    index: int
    start_sector: int
    size_sectors: int
    type_id: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        if self.index < 0:
            raise OlmaBoundsError("index < 0")
        if self.start_sector < 0 or self.size_sectors < 0:
            raise OlmaBoundsError("отрицательные границы раздела")

    def clamp(self, total_sectors: int) -> "OlmaPartition":
        start = max(0, self.start_sector)
        if start >= total_sectors:
            return OlmaPartition(self.index, 0, 0, self.type_id, self.label)
        size = min(self.size_sectors, total_sectors - start)
        return OlmaPartition(self.index, start, size, self.type_id, self.label)

    def view(self, handle: OlmaHandle, total_sectors: int) -> OlmaView:
        p = self.clamp(total_sectors)
        return OlmaView(handle, p.start_sector * SECTOR, p.size_sectors * SECTOR)


class OlmaPartitionTable:
    """
    Валидированная таблица разделов (MBR или GPT).
    """

    def __init__(self, buf: OlmaBuffer) -> None:
        self._buf = buf
        self._kind: str = "unknown"
        self._parts: list[OlmaPartition] = []
        self._parse()

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def partitions(self) -> list[OlmaPartition]:
        return list(self._parts)

    def _parse(self) -> None:
        if len(self._buf) < SECTOR:
            raise OlmaIntegrityError("буфер меньше сектора")
        if self._buf.slice(510, 2) == b"\\x55\\xAA":
            if len(self._buf) >= 1024 and self._buf.slice(512, 8) == b"EFI PART":
                self._kind = "gpt"
                self._parse_gpt()
            else:
                self._kind = "mbr"
                self._parse_mbr()
        else:
            raise OlmaIntegrityError("нет сигнатуры 0x55AA")

    def _parse_mbr(self) -> None:
        for i in range(4):
            off = 446 + i * 16
            entry = self._buf.slice(off, 16)
            start = int.from_bytes(entry[8:12], "little")
            size = int.from_bytes(entry[12:16], "little")
            type_id = entry[4]
            if start == 0 and size == 0:
                continue
            if size == 0:
                continue
            self._parts.append(
                OlmaPartition(i, start, size, type_id, "")
            )
            if len(self._parts) > MAX_PARTITIONS:
                raise OlmaIntegrityError("слишком много разделов MBR")

    def _parse_gpt(self) -> None:
        # Упрощённо: одна запись = 128 байт, начиная с LBA 2
        header = self._buf.slice(512, 92)
        entries_lba = int.from_bytes(header[72:80], "little")
        num = int.from_bytes(header[80:84], "little")
        entry_size = int.from_bytes(header[84:88], "little")
        if num > MAX_PARTITIONS:
            num = MAX_PARTITIONS
        if entry_size == 0 or entry_size > 512:
            raise OlmaIntegrityError(f"неверный entry_size={entry_size}")
        base = entries_lba * SECTOR
        for i in range(num):
            off = base + i * entry_size
            if off + 16 > len(self._buf):
                break
            type_guid = self._buf.slice(off, 16)
            if type_guid == b"\\x00" * 16:
                continue
            first_lba = int.from_bytes(self._buf.slice(off + 32, 8), "little")
            last_lba = int.from_bytes(self._buf.slice(off + 40, 8), "little")
            if last_lba < first_lba:
                continue
            self._parts.append(
                OlmaPartition(i, first_lba, last_lba - first_lba + 1, 0, "")
            )


# --------------------------------------------------------------------------- #
# OlmaTransaction — атомарная запись
# --------------------------------------------------------------------------- #

class OlmaTransaction:
    """
    Атомарная запись: писать во временный файл, затем os.replace.
    При исключении — откат (temp удаляется, оригинал не тронут).
    """

    def __init__(self, target: str | os.PathLike) -> None:
        self._target = os.fspath(target)
        d = os.path.dirname(os.path.abspath(self._target)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".olma-", suffix=".tmp")
        os.close(fd)
        self._tmp = tmp
        self._committed = False
        self._handle = OlmaHandle(tmp, "wb")

    @property
    def handle(self) -> OlmaHandle:
        return self._handle

    def write_all(self, data: bytes) -> None:
        self._handle.write_at(0, data)

    def commit(self) -> None:
        if self._committed:
            raise OlmaStateError("транзакция уже зафиксирована")
        self._handle.flush()
        self._handle.close()
        os.replace(self._tmp, self._target)
        self._committed = True

    def rollback(self) -> None:
        if self._committed:
            return
        try:
            self._handle.close()
        except Exception:
            pass
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    def __enter__(self) -> "OlmaTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


# --------------------------------------------------------------------------- #
# OlmaXorDecoder — безопасная XOR-расшифровка (RT809H MST/MTK)
# --------------------------------------------------------------------------- #

class OlmaXorDecoder:
    """
    Потоковая XOR-расшифровка с проверкой размера таблицы.
    """

    def __init__(self, table: bytes | bytearray) -> None:
        self._table = bytes(table)
        if not self._table:
            raise OlmaIntegrityError("пустая XOR-таблица")

    @property
    def table_size(self) -> int:
        return len(self._table)

    def decode(self, data: bytes | bytearray) -> bytes:
        return bytes(
            b ^ self._table[i % len(self._table)]
            for i, b in enumerate(data)
        )

    def decode_file(self, src: OlmaHandle, dst: OlmaHandle,
                    *, chunk: int = DEFAULT_CHUNK) -> int:
        total = src.size
        written = 0
        off = 0
        while off < total:
            n = min(chunk, total - off)
            block = src.read_at(off, n)
            dst.write_at(off, self.decode(block))
            off += n
            written += n
        return written


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def safe_download(url: str, *, timeout: float = 30.0,
                  max_size: int = DEFAULT_MAX_DOWNLOAD) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "MountFile/OLMA"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        length = resp.headers.get("Content-Length")
        if length and int(length) > max_size:
            raise OlmaIntegrityError(f"файл слишком большой: {length}")
        data = bytearray()
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_size:
                raise OlmaIntegrityError("превышен лимит при скачивании")
    return bytes(data)


def verify_sha256(data: bytes, expected_hex: str) -> bool:
    if not expected_hex:
        return True
    return hashlib.sha256(data).hexdigest().lower() == expected_hex.strip().lower()


__all__ = [
    "OlmaError", "OlmaBoundsError", "OlmaIntegrityError", "OlmaStateError",
    "OlmaHandle", "OlmaView", "OlmaBuffer",
    "OlmaCluster", "OlmaChain",
    "OlmaPartition", "OlmaPartitionTable",
    "OlmaTransaction", "OlmaXorDecoder",
    "safe_download", "verify_sha256",
    "SECTOR", "FAT32_MAX_CLUSTER", "FAT32_EOC_MIN", "FAT32_BAD",
]
'''

QT_COMPAT_TEMPLATE = '''\
# MOUNTFILE_FIX — auto-patched + OLMA
"""qt_compat.py — унифицированный слой PyQt5/PyQt6."""

from __future__ import annotations

IS_QT6 = False
QT_VERSION = "none"
_IMPORT_ERROR = None

try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore
    from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot  # type: ignore
    from PyQt6.QtGui import QKeySequence, QAction, QIcon  # type: ignore
    IS_QT6 = True
    QT_VERSION = "PyQt6"
except Exception as e6:  # noqa: BLE001
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore
        from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot  # type: ignore
        from PyQt5.QtGui import QKeySequence, QIcon  # type: ignore
        from PyQt5.QtWidgets import QAction  # type: ignore
        IS_QT6 = False
        QT_VERSION = "PyQt5"
    except Exception as e5:  # noqa: BLE001
        _IMPORT_ERROR = f"PyQt6: {e6!r}; PyQt5: {e5!r}"


def is_qt_available() -> bool:
    return _IMPORT_ERROR is None


def require_qt() -> None:
    if not is_qt_available():
        raise RuntimeError(
            "Qt не найден. Установите PyQt6 (рекомендуется) или PyQt5.\\n"
            f"Детали: {_IMPORT_ERROR}"
        )


__all__ = [
    "QtCore", "QtGui", "QtWidgets", "Qt",
    "pyqtSignal", "pyqtSlot", "QKeySequence", "QAction", "QIcon",
    "IS_QT6", "QT_VERSION", "is_qt_available", "require_qt",
]
'''
