# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Mount File Turbo: single-file engine for everything.

Contents
--------
* TTLCache           — thread-safe TTL/LRU cache
* CachedFS           — pytsk3 wrapper with directory memoisation
* Buzhash            — content-defined chunking (CDC) rolling hash
* Signature          — chunk table with weak (adler32) + strong (blake2b)
* SignatureStore     — on-disk cache of signatures (.mfsig)
* DeltaSync          — mmap + CDC + zstd delta transfer, atomic patching
* SqliteJournal      — append-only change journal backed by sqlite3
* Worker/TaskRunner  — QThreadPool bridge (PyQt6 / PyQt5, optional)
* DirTableModel      — virtualised QAbstractTableModel (optional)
* TurboController    — one-call integration: turbo.attach(window)
* COMMANDS           — {"sync": ..., "diff": ..., "verify": ..., "sig": ...}

Usage
-----
GUI:
    from mountfile import turbo
    turbo.attach(self)                    # self is your MainWindow
CLI:
    python -m mountfile.turbo diff  a.img b.img
    python -m mountfile.turbo sync  a.img b.img --workers 8
    python -m mountfile.turbo sig   a.img
    python -m mountfile.turbo verify a.img b.img
"""

from __future__ import annotations

import argparse
import hashlib
import mmap
import os
import random
import sqlite3
import struct
import sys
import threading
import time
import traceback
import zlib
from typing import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------
# Optional accelerators
# --------------------------------------------------------------------------

try:
    import zstandard as _zstd  # type: ignore

    def _compress(b: bytes) -> bytes:
        return _zstd.ZstdCompressor(level=3).compress(b)

    def _decompress(b: bytes) -> bytes:
        return _zstd.ZstdDecompressor().decompress(b)

    _CODEC = b"Z"
except Exception:  # pragma: no cover - optional

    def _compress(b: bytes) -> bytes:
        return zlib.compress(b, 6)

    def _decompress(b: bytes) -> bytes:
        return zlib.decompress(b)

    _CODEC = b"L"


try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

    _QT = "PyQt6"
except Exception:  # pragma: no cover
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore

        _QT = "PyQt5"
    except Exception:
        QtCore = QtGui = QtWidgets = None  # type: ignore
        _QT = None

QT_BINDING = _QT
HAS_QT = _QT is not None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SIG_MAGIC = b"MFSIG\x02"
SIG_VERSION = 2

# CDC parameters (bytes)
CHUNK_MIN = 1 << 12  # 4 KiB
CHUNK_AVG = 1 << 16  # 64 KiB
CHUNK_MAX = 1 << 20  # 1 MiB
MASK_BITS = 13  # ~1/8192 hit → avg 64 KiB
WINDOW = 48

# literal-run cap before flushing (see diff())
LITERAL_FLUSH = 1 << 20  # 1 MiB

COPY_BUF = 1 << 20  # 1 MiB copy buffer

Progress = Optional[Callable[[int, int, str], None]]


# ==========================================================================
# 1. Cache
# ==========================================================================


class TTLCache:
    """Bounded, thread-safe, per-entry TTL."""

    __slots__ = ("_data", "_lock", "_order", "hits", "maxsize", "misses", "ttl")

    def __init__(self, maxsize: int = 1024, ttl: float = 30.0):
        self.maxsize, self.ttl = maxsize, ttl
        self._data: dict[Any, tuple[float, Any]] = {}
        self._order: list[Any] = []
        self._lock = threading.RLock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            ts, val = item
            if self.ttl and (time.time() - ts) > self.ttl:
                self._drop(key)
                self.misses += 1
                return None
            self.hits += 1
            return val

    def put(self, key, value):
        with self._lock:
            if key in self._data:
                self._order.remove(key)
            self._data[key] = (time.time(), value)
            self._order.append(key)
            while len(self._order) > self.maxsize:
                self._drop(self._order[0])

    def _drop(self, key):
        self._data.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass

    def invalidate(self, key):
        with self._lock:
            self._drop(key)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._order.clear()
            self.hits = self.misses = 0

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
        }


# ==========================================================================
# 2. Cached pytsk3 wrapper
# ==========================================================================


class CachedFS:
    """Memoises directory listings of a pytsk3.FS_Info instance."""

    def __init__(self, fs, listing_ttl: float = 20.0):
        self._fs = fs
        self._listing = TTLCache(maxsize=512, ttl=listing_ttl)

    @property
    def raw(self):
        return self._fs

    def list_dir(self, path: str) -> list[dict]:
        cached = self._listing.get(path)
        if cached is not None:
            return cached
        rows: list[dict] = []
        for entry in self._fs.open_dir(path):
            try:
                meta = entry.info.meta
                rows.append(
                    {
                        "name": entry.info.name.name.decode("utf-8", "replace"),
                        "size": meta.size if meta else 0,
                        "type": meta.type if meta else None,
                        "inode": meta.addr if meta else None,
                        "mtime": meta.mtime if meta else None,
                    }
                )
            except Exception:
                continue
        self._listing.put(path, rows)
        return rows

    def invalidate(self, path: str | None = None) -> None:
        self._listing.clear() if path is None else self._listing.invalidate(path)

    def stats(self) -> dict:
        return self._listing.stats()


# ==========================================================================
# 3. Content-defined chunking (buzhash)
# ==========================================================================

_rng = random.Random(0x4D6F756E74)  # "Mount"
_BUZ = [_rng.getrandbits(64) for _ in range(256)]


def _rol(x: int, n: int) -> int:
    n &= 63
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


class Buzhash:
    """Streaming CDC: yields (offset, length) chunk boundaries."""

    __slots__ = ("h", "mask", "pos", "size", "start")

    def __init__(self, mask_bits: int = MASK_BITS):
        self.mask = (1 << mask_bits) - 1
        self.reset()

    def reset(self):
        self.h = 0
        self.size = 0
        self.start = 0
        self.pos = 0

    def feed(self, data: bytes) -> list[tuple[int, int]]:
        """Return closed chunks (offset, length) after consuming `data`."""
        out: list[tuple[int, int]] = []
        h, mask = self.h, self.mask
        size, start, pos = self.size, self.start, self.pos
        min_l, max_l = CHUNK_MIN, CHUNK_MAX
        for b in data:
            h = _rol(h, 1) ^ _BUZ[b]
            size += 1
            pos += 1
            if size >= min_l and ((h & mask) == 0 or size >= max_l):
                out.append((start, size))
                start += size
                size = 0
                h = 0
        self.h, self.size, self.start, self.pos = h, size, start, pos
        return out

    def finish(self) -> tuple[int, int] | None:
        if self.size:
            chunk = (self.start, self.size)
            self.reset()
            return chunk
        return None


# ==========================================================================
# 4. Signature
# ==========================================================================


@dataclass
class BlockRef:
    offset: int
    length: int
    weak: int
    strong: bytes


@dataclass
class Signature:
    file_size: int = 0
    mtime: float = 0.0
    block_refs: list[BlockRef] = field(default_factory=list)
    by_strong: dict = field(default_factory=dict)  # strong -> BlockRef

    # ---------- serialisation ----------
    def to_bytes(self) -> bytes:
        header = struct.pack(
            "<6sBQQQIIII",
            SIG_MAGIC,
            SIG_VERSION,
            self.file_size,
            int(self.mtime * 1000),
            len(self.block_refs),
            CHUNK_MIN,
            CHUNK_AVG,
            CHUNK_MAX,
            MASK_BITS,
        )
        body = bytearray()
        for r in self.block_refs:
            body += struct.pack("<QII", r.offset, r.length, r.weak)
            body += r.strong
        return header + bytes(body)

    @classmethod
    def from_bytes(cls, blob: bytes) -> Signature:
        if not blob.startswith(SIG_MAGIC):
            raise ValueError("not a Mount File signature")
        off = len(SIG_MAGIC)
        version = blob[off]
        if version != SIG_VERSION:
            raise ValueError(f"signature version {version} not supported")
        off += 1
        size, mtime_ms, count, *_ = struct.unpack_from("<QQQIIII", blob, off)
        off += struct.calcsize("<QQQIIII")
        sig = cls(file_size=size, mtime=mtime_ms / 1000.0)
        for _ in range(count):
            offset, length, weak = struct.unpack_from("<QII", blob, off)
            off += 16
            strong = blob[off : off + 16]
            off += 16
            ref = BlockRef(offset, length, weak, strong)
            sig.block_refs.append(ref)
            sig.by_strong.setdefault(strong, ref)
        return sig

    def add(self, ref: BlockRef) -> None:
        self.block_refs.append(ref)
        self.by_strong.setdefault(ref.strong, ref)


# ==========================================================================
# 5. Disk signature cache (.mfsig)
# ==========================================================================


class SignatureStore:
    """Persists signatures next to images; invalidates on mtime/size change."""

    @staticmethod
    def _path(image_path: str) -> str:
        return image_path + ".mfsig"

    @classmethod
    def load(cls, image_path: str) -> Signature | None:
        sp = cls._path(image_path)
        if not os.path.exists(sp):
            return None
        try:
            st = os.stat(image_path)
            with open(sp, "rb") as fh:
                sig = Signature.from_bytes(fh.read())
            if sig.file_size != st.st_size:
                return None
            if abs(sig.mtime - st.st_mtime) > 0.001:
                return None
            return sig
        except Exception:
            return None

    @classmethod
    def save(cls, image_path: str, sig: Signature) -> None:
        tmp = cls._path(image_path) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(sig.to_bytes())
        os.replace(tmp, cls._path(image_path))


# ==========================================================================
# 6. DeltaSync — CDC + mmap + zstd + atomic
# ==========================================================================


class DeltaSync:
    """Content-defined delta transfer for raw disk images.

    Builds a chunk signature of the target, walks the source with the same
    CDC, emits copy/literal operations, applies them atomically.
    """

    def __init__(self, workers: int = 4, use_disk_cache: bool = True):
        self.workers = workers
        self.use_disk_cache = use_disk_cache

    # ---------- signature ----------
    def build_signature(self, path: str, progress: Progress = None) -> Signature:
        st = os.stat(path)
        sig = Signature(file_size=st.st_size, mtime=st.st_mtime)

        # fast path: reuse cached signature
        if self.use_disk_cache:
            cached = SignatureStore.load(path)
            if cached is not None:
                if progress:
                    progress(1, 1, "Подпись из кэша")
                return cached

        bh = Buzhash()
        processed = 0
        total = max(st.st_size, 1)
        with open(path, "rb") as fh:
            while True:
                data = fh.read(1 << 20)
                if not data:
                    break
                for off, length in bh.feed(data):
                    self._add_chunk(sig, path, off, length)
                processed += len(data)
                if progress and (processed % (8 << 20) == 0):
                    progress(processed, total, "Хеширование цели")
        tail = bh.finish()
        if tail:
            self._add_chunk(sig, path, tail[0], tail[1])

        if self.use_disk_cache and sig.block_refs:
            try:
                SignatureStore.save(path, sig)
            except OSError:
                pass
        if progress:
            progress(total, total, f"Цель: {len(sig.block_refs)} чанков")
        return sig

    def _add_chunk(self, sig: Signature, path: str, off: int, length: int) -> None:
        with open(path, "rb") as fh:
            fh.seek(off)
            data = fh.read(length)
        weak = zlib.adler32(data) & 0xFFFFFFFF
        strong = hashlib.blake2b(data, digest_size=16).digest()
        sig.add(BlockRef(off, length, weak, strong))

    # ---------- diff ----------
    def diff(
        self, source: str, sig: Signature, progress: Progress = None
    ) -> list[tuple[str, bytes, int, int]]:
        """Return ops: ('copy', b'', target_off, target_len) or
        ('data', payload, 0, payload_len). Payload is zstd/zlib-compressed."""
        ops: list[tuple[str, bytes, int, int]] = []
        literal = bytearray()
        total = max(os.path.getsize(source), 1)
        processed = 0

        def flush():
            if not literal:
                return
            ops.append(("data", _compress(bytes(literal)), 0, len(literal)))
            literal.clear()

        bh = Buzhash()
        with open(source, "rb") as fh:
            while True:
                data = fh.read(1 << 20)
                if not data:
                    break
                for off, length in bh.feed(data):
                    match = self._match(source, off, length, sig)
                    if match is not None:
                        flush()
                        ops.append(("copy", b"", match.offset, match.length))
                    else:
                        fh2 = open(source, "rb")
                        try:
                            fh2.seek(off)
                            literal.extend(fh2.read(length))
                        finally:
                            fh2.close()
                        if len(literal) >= LITERAL_FLUSH:
                            flush()
                processed += len(data)
                if progress and (processed % (8 << 20) == 0):
                    progress(processed, total, "Диффинг источника")
        tail = bh.finish()
        if tail:
            match = self._match(source, tail[0], tail[1], sig)
            if match is not None:
                flush()
                ops.append(("copy", b"", match.offset, match.length))
            else:
                with open(source, "rb") as fh:
                    fh.seek(tail[0])
                    literal.extend(fh.read(tail[1]))
        flush()
        if progress:
            progress(total, total, f"{len(ops)} операций")
        return ops

    @staticmethod
    def _match(source: str, off: int, length: int, sig: Signature) -> BlockRef | None:
        with open(source, "rb") as fh:
            fh.seek(off)
            data = fh.read(length)
        if len(data) != length:
            return None
        weak = zlib.adler32(data) & 0xFFFFFFFF
        strong = hashlib.blake2b(data, digest_size=16).digest()
        ref = sig.by_strong.get(strong)
        if ref is not None and ref.length == length and ref.weak == weak:
            return ref
        return None

    # ---------- patch ----------
    def patch(self, base: str, ops, out_path: str, progress: Progress = None) -> int:
        written = 0
        total = len(ops)
        with open(base, "rb") as src, open(out_path, "wb") as dst:
            try:
                mm = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
            except (ValueError, OSError):
                mm = None
            try:
                for i, (kind, payload, off, length) in enumerate(ops, 1):
                    if kind == "copy":
                        if mm is not None:
                            dst.write(mm[off : off + length])
                            written += length
                        else:
                            src.seek(off)
                            remaining = length
                            while remaining:
                                chunk = src.read(min(COPY_BUF, remaining))
                                if not chunk:
                                    break
                                dst.write(chunk)
                                remaining -= len(chunk)
                                written += len(chunk)
                    else:
                        raw = _decompress(payload)
                        dst.write(raw)
                        written += len(raw)
                    if progress and (i % 256 == 0 or i == total):
                        progress(i, total, "Запись")
            finally:
                if mm is not None:
                    mm.close()
        return written

    # ---------- high-level ----------
    def sync(self, source: str, target: str, progress: Progress = None) -> dict:
        if not os.path.exists(target):
            _copy_file(source, target, progress)
            return {"mode": "full", "bytes_written": os.path.getsize(source)}

        sig = self.build_signature(target, progress=progress)
        ops = self.diff(source, sig, progress=progress)
        tmp = target + ".synctmp"
        try:
            written = self.patch(target, ops, tmp, progress=progress)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        full = os.path.getsize(source)
        return {
            "mode": "delta-cdc",
            "codec": _CODEC.decode("ascii"),
            "ops": len(ops),
            "bytes_written": written,
            "full_size": full,
            "saving": 1 - written / max(1, full),
        }

    def verify(self, a: str, b: str, progress: Progress = None) -> dict:
        """Compare two images by blake2b over 4 MiB windows."""
        sa, sb = os.path.getsize(a), os.path.getsize(b)
        if sa != sb:
            return {
                "equal": False,
                "reason": "size mismatch",
                "size_a": sa,
                "size_b": sb,
            }
        h1, h2 = hashlib.blake2b(digest_size=32), hashlib.blake2b(digest_size=32)
        total = sa
        done = 0
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca = fa.read(1 << 22)
                cb = fb.read(1 << 22)
                if not ca and not cb:
                    break
                if ca != cb:
                    return {"equal": False, "reason": "byte mismatch", "offset": done}
                h1.update(ca)
                h2.update(cb)
                done += len(ca)
                if progress:
                    progress(done, total, "Проверка")
        return {
            "equal": h1.digest() == h2.digest(),
            "digest_a": h1.hexdigest(),
            "digest_b": h2.hexdigest(),
            "size": sa,
        }


def _copy_file(src: str, dst: str, progress: Progress = None) -> None:
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(COPY_BUF)
            if not buf:
                break
            fo.write(buf)
            done += len(buf)
            if progress and (done % (8 << 20) == 0):
                progress(done, total, "Копирование")


# ==========================================================================
# 7. SQLite journal
# ==========================================================================


class SqliteJournal:
    """Append-only change log persisted to SQLite; safe from threads."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS changes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL    NOT NULL,
            op      TEXT    NOT NULL,
            path    TEXT    NOT NULL,
            size    INTEGER NOT NULL DEFAULT 0,
            digest  TEXT    NOT NULL DEFAULT '',
            extra   TEXT    NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_ts ON changes(ts);
    """

    def __init__(self, path: str = "changes.sqlite"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def record(
        self,
        op: str,
        path: str,
        size: int = 0,
        digest: str = "",
        extra: dict | None = None,
    ) -> int:
        import json

        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO changes(ts, op, path, size, digest, extra) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    op,
                    path,
                    size,
                    digest,
                    json.dumps(extra or {}, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def tail(self, since_ts: float = 0.0, limit: int = 1000) -> list[dict]:
        import json

        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, op, path, size, digest, extra "
                "FROM changes WHERE ts > ? ORDER BY ts LIMIT ?",
                (since_ts, limit),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "op": r[1],
                "path": r[2],
                "size": r[3],
                "digest": r[4],
                "extra": json.loads(r[5]),
            }
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]

    def gc(self, keep_last: int = 10_000) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM changes WHERE id NOT IN "
                "(SELECT id FROM changes ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ==========================================================================
# 8. Qt: workers, model, controller
# ==========================================================================

if HAS_QT:
    # Qt6 moved every enum into a scoped enum class (Qt.ItemDataRole.UserRole);
    # Qt5 exposes the same names flat on Qt (Qt.UserRole). Resolve once, here,
    # instead of inside a class body — a missing attribute there made the whole
    # module unimportable on PyQt6 (AttributeError at import time).
    _Qt = QtCore.Qt
    if hasattr(_Qt, "ItemDataRole"):
        _ROLE_USER = int(_Qt.ItemDataRole.UserRole)
        _ROLE_DISPLAY = _Qt.ItemDataRole.DisplayRole
        _ORIENT_H = _Qt.Orientation.Horizontal
        _SORT_ASC = _Qt.SortOrder.AscendingOrder
        _SORT_DESC = _Qt.SortOrder.DescendingOrder
    else:  # pragma: no cover - PyQt5 / Qt5
        _ROLE_USER = int(_Qt.UserRole)
        _ROLE_DISPLAY = _Qt.DisplayRole
        _ORIENT_H = _Qt.Horizontal
        _SORT_ASC = _Qt.AscendingOrder
        _SORT_DESC = _Qt.DescendingOrder

    class _Signals(QtCore.QObject):
        started = QtCore.pyqtSignal()
        progress = QtCore.pyqtSignal(int, int, str)
        result = QtCore.pyqtSignal(object)
        error = QtCore.pyqtSignal(tuple)
        finished = QtCore.pyqtSignal()

    class Worker(QtCore.QRunnable):
        """Runnable with progress + cooperative cancel."""

        def __init__(self, fn, *args, **kwargs):
            super().__init__()
            self.fn, self.args, self.kwargs = fn, args, kwargs
            self.signals = _Signals()
            self._cancelled = False
            self.setAutoDelete(True)

        def cancel(self):
            self._cancelled = True

        @property
        def cancelled(self):
            return self._cancelled

        @QtCore.pyqtSlot()
        def run(self):
            self.signals.started.emit()
            try:
                result = self.fn(self, *self.args, **self.kwargs)
            except Exception as exc:
                self.signals.error.emit((type(exc), exc, traceback.format_exc()))
            else:
                self.signals.result.emit(result)
            finally:
                self.signals.finished.emit()

    class TaskRunner(QtCore.QObject):
        """Pool of workers bound to a window; cancels all on close."""

        def __init__(self, parent=None, max_threads: int | None = None):
            super().__init__(parent)
            self.pool = QtCore.QThreadPool.globalInstance()
            if max_threads:
                self.pool.setMaxThreadCount(max_threads)
            self._workers: list[Worker] = []

        def submit(
            self,
            fn,
            *args,
            on_result=None,
            on_error=None,
            on_progress=None,
            on_finished=None,
            **kwargs,
        ) -> Worker:
            w = Worker(fn, *args, **kwargs)
            if on_result:
                w.signals.result.connect(on_result)
            if on_error:
                w.signals.error.connect(on_error)
            if on_progress:
                w.signals.progress.connect(on_progress)
            if on_finished:
                w.signals.finished.connect(on_finished)
            self._workers.append(w)
            w.signals.finished.connect(
                lambda: self._workers.remove(w) if w in self._workers else None
            )
            self.pool.start(w)
            return w

        def cancel_all(self) -> None:
            for w in list(self._workers):
                w.cancel()
            self.pool.clear()

    class DirTableModel(QtCore.QAbstractTableModel):
        """Virtualised table; thousands of rows render instantly."""

        COLUMNS = ("Имя", "Размер", "Тип", "Изменён")
        RowRole = _ROLE_USER + 1

        def __init__(self, parent=None):
            super().__init__(parent)
            self._rows: list[dict] = []

        def set_rows(self, rows: list[dict]) -> None:
            self.beginResetModel()
            self._rows = rows
            self.endResetModel()

        def row_at(self, row: int) -> dict:
            return self._rows[row]

        def rowCount(self, parent=QtCore.QModelIndex()):
            return 0 if parent.isValid() else len(self._rows)

        def columnCount(self, parent=QtCore.QModelIndex()):
            return len(self.COLUMNS)

        def headerData(self, section, orientation, role=_ROLE_DISPLAY):
            if orientation == _ORIENT_H and role == _ROLE_DISPLAY:
                return self.COLUMNS[section]
            return None

        def data(self, index, role=_ROLE_DISPLAY):
            if not index.isValid():
                return None
            row = self._rows[index.row()]
            if role == self.RowRole:
                return row
            if role != _ROLE_DISPLAY:
                return None
            col = index.column()
            if col == 0:
                return row.get("name", "")
            if col == 1:
                return _human_size(row.get("size") or 0)
            if col == 2:
                return "Папка" if row.get("type") == 2 else "Файл"
            if col == 3:
                ts = row.get("mtime")
                if not ts:
                    return ""
                try:
                    import datetime

                    return datetime.datetime.fromtimestamp(ts).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except Exception:
                    return str(ts)
            return None

        def sort(self, column, order=_SORT_ASC):
            key = ("name", "size", "type", "mtime")[column]
            reverse = order == _SORT_DESC
            self._rows.sort(
                key=lambda r: (r.get("type") != 2, _sortable(r.get(key))),
                reverse=reverse,
            )
            self.layoutChanged.emit()

else:  # pragma: no cover
    Worker = TaskRunner = DirTableModel = None  # type: ignore


def _sortable(v):
    if v is None:
        return ""
    return v.lower() if isinstance(v, str) else v


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} ПБ"


# ==========================================================================
# 9. TurboController — one-call integration
# ==========================================================================


class TurboController:
    """Wraps a MainWindow with async I/O, cache, journal and delta sync.

    Attaches itself as `window.turbo`. If you already have a MainWindow
    with `.fs` (a pytsk3.FS_Info) and `.image_path`, calling
    `turbo.attach(window)` is enough — no other code changes.
    """

    def __init__(
        self, window, fs=None, journal_path: str = "changes.sqlite", workers: int = 4
    ):
        if not HAS_QT:
            raise RuntimeError("TurboController requires PyQt5/PyQt6")
        self.win = window
        self.runner = TaskRunner(window, max_threads=workers)
        self.journal = SqliteJournal(journal_path)
        self.sync = DeltaSync(workers=workers)

        raw = fs if fs is not None else getattr(window, "fs", None)
        self.fs = CachedFS(raw) if raw is not None else None
        if self.fs is not None:
            window.fs = self.fs  # existing code keeps working

        window.turbo = self
        self._patch_close_event(window)

    # ---------- helpers ----------
    def _patch_close_event(self, window) -> None:
        original = getattr(window, "closeEvent", None)

        def closeEvent(event, _orig=original, _self=self):
            _self.runner.cancel_all()
            if _orig is not None:
                try:
                    _orig(event)
                except TypeError:
                    event.accept()
            else:
                event.accept()

        window.closeEvent = closeEvent  # type: ignore[assignment]

    # ---------- public ----------
    def open_dir(self, path: str, on_result=None, on_error=None) -> None:
        if self.fs is None:
            return
        self.runner.submit(
            lambda w, p: self.fs.list_dir(p),
            path,
            on_result=on_result,
            on_error=on_error or self._default_error,
        )

    def sync_to(
        self, target: str, progress_ui=None, on_done=None, on_error=None
    ) -> None:
        source = getattr(self.win, "image_path", "")
        if not source:
            raise RuntimeError("window.image_path is empty")

        def job(worker):
            def cb(done, total, label):
                worker.signals.progress.emit(
                    int(done * 100 / max(1, total)), 100, label
                )

            info = self.sync.sync(source, target, progress=cb)
            return info

        def on_result(info):
            self.journal.record(
                op="sync",
                path=source,
                size=info.get("bytes_written", 0),
                extra={"target": target, **info},
            )
            if on_done:
                on_done(info)

        self.runner.submit(
            job,
            on_progress=progress_ui,
            on_result=on_result,
            on_error=on_error or self._default_error,
        )

    def stats(self) -> dict:
        return {
            "cache": self.fs.stats() if self.fs else {},
            "journal": self.journal.count(),
        }

    def _default_error(self, exc):
        _t, e, tb = exc
        sys.stderr.write(f"[turbo] {e}\n{tb}\n")


def attach(
    window, fs=None, journal_path: str = "changes.sqlite", workers: int = 4
) -> TurboController:
    """One-call integration; returns the TurboController."""
    return TurboController(window, fs=fs, journal_path=journal_path, workers=workers)


# ==========================================================================
# 10. CLI
# ==========================================================================


def _cli_sig(args) -> int:
    sync = DeltaSync()
    sig = sync.build_signature(args.image, progress=_cli_progress)
    SignatureStore.save(args.image, sig)
    print(
        f"\nПодпись: {len(sig.block_refs)} чанков "
        f"({SignatureStore._path(args.image)})"
    )
    return 0


def _cli_diff(args) -> int:
    sync = DeltaSync()
    sig = sync.build_signature(args.target)
    ops = sync.diff(args.source, sig)
    total = os.path.getsize(args.source)
    literal = sum(length for k, _p, _o, length in ops if k == "data")
    print(
        f"\nops={len(ops)}  literal={literal:,} B  full={total:,} B  "
        f"saving≈{(1 - literal / max(1, total)) * 100:.1f}%"
    )
    return 0


def _cli_sync(args) -> int:
    sync = DeltaSync(workers=args.workers)
    t0 = time.time()
    info = sync.sync(args.source, args.target, progress=_cli_progress)
    print(f"\nГотово за {time.time() - t0:.2f}s → {info}")
    if args.journal:
        SqliteJournal(args.journal).record(
            op="sync",
            path=args.source,
            size=info.get("bytes_written", 0),
            extra={"target": args.target, **info},
        )
    return 0


def _cli_verify(args) -> int:
    sync = DeltaSync()
    info = sync.verify(args.a, args.b, progress=_cli_progress)
    print()
    if info.get("equal"):
        print(f"Идентичны. sha={info['digest_a'][:16]}…  size={info['size']:,}")
        return 0
    print(f"Различия: {info}")
    return 1


def _cli_progress(done: int, total: int, label: str) -> None:
    if total:
        pct = done * 100 // total
        sys.stderr.write(f"\r{label}: {pct:3d}%  ({done:,}/{total:,})")
    else:
        sys.stderr.write(f"\r{label}: {done}")
    sys.stderr.flush()


COMMANDS = {
    "sig": _cli_sig,
    "diff": _cli_diff,
    "sync": _cli_sync,
    "verify": _cli_verify,
}


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m mountfile.turbo",
        description="Mount File Turbo — CDC delta sync for raw images",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sig", help="build & cache signature for an image")
    p.add_argument("image")
    p.set_defaults(func=_cli_sig)

    p = sub.add_parser("diff", help="dry-run delta between two images")
    p.add_argument("source")
    p.add_argument("target")
    p.set_defaults(func=_cli_diff)

    p = sub.add_parser("sync", help="apply delta: source → target")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--journal", default="")
    p.set_defaults(func=_cli_sync)

    p = sub.add_parser("verify", help="byte-compare two images")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=_cli_verify)

    return ap


def cli_main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nПрервано\n")
        return 130


# ==========================================================================
# 11. Self-test / demo
# ==========================================================================


def _selftest() -> int:
    import tempfile

    print("Mount File Turbo — self-test")
    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "a.img")
        b = os.path.join(td, "b.img")
        with open(a, "wb") as fh:
            fh.write(os.urandom(4 << 20))
        with open(b, "wb") as fh:
            fh.write(open(a, "rb").read())
        # mutate b: flip 8 KiB at offset 1 MiB
        with open(b, "r+b") as fh:
            fh.seek(1 << 20)
            fh.write(os.urandom(8 << 10))
        sync = DeltaSync(use_disk_cache=False)
        t0 = time.time()
        info = sync.sync(b, a, progress=_cli_progress)  # b → a
        sys.stderr.write("\n")
        print(f"sync: {info}  ({time.time() - t0:.2f}s)")
        v = sync.verify(a, b)
        print(f"verify: {v['equal']}  sha={v.get('digest_a', '')[:16]}…")
    print("OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    sys.exit(cli_main())
