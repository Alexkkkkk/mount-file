#!/usr/bin/env python3
"""
mountfile/universal_fs_rw.py — Universal Read/Write Layer (single file)
=======================================================================

Расширяет UniversalFS полноценной записью через 5 стратегий:

  1. NativeFat32Writer  — FAT12/16/32, прямая правка FAT + LFN + каталогов
  2. NativeExt2Writer   — ext2 (без журнала), bitmap + прямой write
  3. ExtDebugfsWriter   — ext2/3/4 через `debugfs -w`
  4. NtfsWriter         — NTFS через `ntfs-3g` / `ntfsclone`
  5. LoopMountWriter    — любые ФС через `mount -o loop,rw`
  6. RebuildWriter      — fallback: unpack → mkfs → repack

Единый API:
    with UniversalFSRW("dump.img") as fs:
        fs.mkdir("/new_dir")
        fs.write_file("/new_dir/hello.txt", b"Hello!")
        fs.rename("/new_dir/hello.txt", "/new_dir/hi.txt")
        fs.delete("/new_dir/hi.txt")
        fs.flush()                # fsck + audit
        fs.restore_from_backup()  # откат

Зависимости (опциональные):
    pytsk3, ntfs-3g, e2fsprogs (debugfs, e2fsck), util-linux (mount),
    dosfstools (mkfs.vfat, fsck.fat)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

try:
    import pytsk3
except ImportError:
    pytsk3 = None

log = logging.getLogger("mountfile.universal_fs_rw")


def _image_path_parent_name(path: str) -> tuple[str, str]:
    """Split a filesystem-in-image path using FAT/POSIX separators on every OS."""
    image_path = PurePosixPath(str(path).replace("\\", "/"))
    parent = str(image_path.parent)
    if parent in ("", "."):
        parent = "/"
    return parent, image_path.name


# =============================================================================
# 1. МОДЕЛИ
# =============================================================================
@dataclass
class FSEntry:
    name: str
    path: str
    size: int
    is_dir: bool
    inode: int = 0
    deleted: bool = False
    mtime: float | None = None
    mode: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FSInfo:
    fs_type: str
    offset: int
    backend: str
    label: str = ""
    total_size: int = 0
    writable: bool = False
    write_strategy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WriteOperation:
    op: str  # write | mkdir | delete | rename
    path: str
    extra: dict = field(default_factory=dict)
    ts: str = ""
    hash: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = _dt.datetime.now(_dt.timezone.utc).isoformat()


# =============================================================================
# 2. AUDIT + BACKUP
# =============================================================================
class RWJournal:
    """Hash-chain журнал всех операций записи."""

    def __init__(self, image: Path):
        self.image = Path(image)
        self.path = Path.home() / ".mountfile" / "rw_journal.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = "0" * 64
        if self.path.exists():
            for line in reversed(self.path.read_text().splitlines()):
                if line.strip():
                    try:
                        self._prev = __import__("json").loads(line)["hash"]
                    except Exception:
                        pass
                    break

    def log(self, op: WriteOperation) -> None:
        import json

        body = {
            "ts": op.ts,
            "image": str(self.image),
            "op": op.op,
            "path": op.path,
            "extra": op.extra,
            "prev": self._prev,
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode()
        ).hexdigest()
        body["hash"] = digest
        with self.path.open("a") as f:
            f.write(json.dumps(body, default=str) + "\n")
        self._prev = digest


class JournalV2:
    """SQLite transaction journal with byte-exact rollback support.

    This is the package home of the v2 journal that was previously implemented
    only by ``mountfile_rw_v2.py``.  The schema is deliberately compatible with
    the legacy journal: opening an existing journal adds ``offset``, ``length``
    and ``committed`` columns in place.  Backups are made before a mutation and
    rollback restores exactly the recorded byte range.
    """

    SCHEMA_VERSION = 2

    def __init__(self, image: Path):
        self.image_path = Path(image)
        self.db_path = self.image_path.with_suffix(
            self.image_path.suffix + ".rw-journal.sqlite"
        )
        self.backup_dir = self.image_path.with_suffix(
            self.image_path.suffix + ".rw-backups"
        )
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                   tx_id TEXT PRIMARY KEY,
                   op TEXT NOT NULL,
                   path TEXT NOT NULL,
                   backup_path TEXT,
                   sha256 TEXT,
                   prev_hash TEXT NOT NULL,
                   created_at REAL NOT NULL
               )"""
        )
        self._migrate()
        self._current = None
        self._last_hash = self._get_last_hash()

    def _migrate(self):
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(journal)")
        }
        if "offset" not in columns:
            self._conn.execute("ALTER TABLE journal ADD COLUMN offset INTEGER")
        if "length" not in columns:
            self._conn.execute("ALTER TABLE journal ADD COLUMN length INTEGER")
        if "committed" not in columns:
            self._conn.execute(
                "ALTER TABLE journal ADD COLUMN committed INTEGER DEFAULT 0"
            )
        self._conn.commit()

    def _get_last_hash(self):
        row = self._conn.execute(
            "SELECT sha256 FROM journal WHERE sha256 IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else "0" * 64

    def begin(self, op: str, path: str) -> str:
        tx_id = str(uuid.uuid4())
        self._current = tx_id
        self._conn.execute(
            "INSERT INTO journal(tx_id, op, path, prev_hash, created_at) "
            "VALUES (?,?,?,?,?)",
            (tx_id, op, path, self._last_hash, time.time()),
        )
        self._conn.commit()
        return tx_id

    def backup_region(self, offset: int, length: int) -> Path:
        if self._current is None:
            raise RuntimeError("begin() must be called before backup_region()")
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        backup = self.backup_dir / (self._current + ".bin")
        with self.image_path.open("rb") as source, backup.open("wb") as target:
            source.seek(offset)
            target.write(source.read(length))
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        self._conn.execute(
            "UPDATE journal SET backup_path=?, sha256=?, offset=?, length=? "
            "WHERE tx_id=?",
            (str(backup), digest, offset, length, self._current),
        )
        self._conn.commit()
        self._last_hash = digest
        return backup

    def commit(self):
        if self._current is not None:
            self._conn.execute(
                "UPDATE journal SET committed=1 WHERE tx_id=?", (self._current,)
            )
            self._conn.commit()
        self._current = None

    def rollback(self, tx_id=None):
        tx_id = tx_id or self._current
        if not tx_id:
            return False
        row = self._conn.execute(
            "SELECT backup_path, offset, length FROM journal WHERE tx_id=?",
            (tx_id,),
        ).fetchone()
        restored = False
        if row and row[0] and row[1] is not None and row[2] is not None:
            backup = Path(row[0])
            if backup.exists():
                with backup.open("rb") as source, self.image_path.open("r+b") as image:
                    image.seek(int(row[1]))
                    image.write(source.read(int(row[2])))
                restored = True
        self._conn.execute("DELETE FROM journal WHERE tx_id=?", (tx_id,))
        self._conn.commit()
        if tx_id == self._current:
            self._current = None
        return restored

    def uncommitted(self):
        rows = self._conn.execute(
            "SELECT tx_id, op, path, offset, length, backup_path, created_at "
            "FROM journal WHERE committed=0 AND backup_path IS NOT NULL "
            "ORDER BY created_at"
        ).fetchall()
        return [
            {
                "tx_id": row[0], "op": row[1], "path": row[2],
                "offset": row[3], "length": row[4], "backup": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    def close(self):
        self._conn.close()


class ImageBackup:
    """Полная резервная копия образа перед записью."""

    def __init__(self, image: Path, keep: int = 3):
        self.image = Path(image)
        self.dir = Path.home() / ".mountfile" / "backups"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep

    def create(self) -> Path:
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = self.dir / f"{self.image.name}.{ts}.bak"
        log.info("backup → %s", dst)
        shutil.copy2(self.image, dst)
        self._rotate()
        return dst

    def _rotate(self) -> None:
        prefix = f"{self.image.name}."
        backups = sorted(self.dir.glob(f"{prefix}*.bak"))
        for old in backups[: -self.keep]:
            try:
                old.unlink()
            except Exception:
                pass

    def latest(self) -> Path | None:
        prefix = f"{self.image.name}."
        backups = sorted(self.dir.glob(f"{prefix}*.bak"))
        return backups[-1] if backups else None


# =============================================================================
# 3. БАЗОВЫЙ КЛАСС ЧТЕНИЯ/ЗАПИСИ
# =============================================================================
class RWBackend(ABC):
    """Unified read/write interface."""

    def __init__(self, image: Path, offset: int = 0):
        self.image = Path(image)
        self.offset = offset
        self._fh: object | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _read_at(self, offset: int, length: int) -> bytes:
        with self.image.open("rb") as f:
            f.seek(self.offset + offset)
            return f.read(length)

    def _write_at(self, offset: int, data: bytes) -> None:
        with self.image.open("r+b") as f:
            f.seek(self.offset + offset)
            f.write(data)

    # ---- read side (must implement) --------------------------------------
    @abstractmethod
    def info(self) -> FSInfo: ...

    @abstractmethod
    def list(self, path: str = "/") -> list[FSEntry]: ...

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    # ---- write side (must implement) -------------------------------------
    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None: ...

    @abstractmethod
    def mkdir(self, path: str) -> None: ...

    @abstractmethod
    def delete(self, path: str, recursive: bool = False) -> None: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...

    # ---- helpers ----------------------------------------------------------
    def exists(self, path: str) -> bool:
        # ``stat`` returns None for a missing path instead of raising, so the
        # old truthiness shortcut reported *every* path as present.
        try:
            return self.stat(path) is not None
        except Exception:
            return False

    def stat(self, path: str) -> FSEntry | None:
        parent, name = _image_path_parent_name(path)
        for e in self.list(parent):
            if e.name == name:
                return e
        return None

    def extract(self, path: str, dest: Path) -> Path:
        data = self.read(path)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def walk(self, path: str = "/") -> Iterator[FSEntry]:
        try:
            entries = self.list(path)
        except Exception:
            return
        for e in entries:
            yield e
            if e.is_dir:
                yield from self.walk(e.path)


# =============================================================================
# 4. NATIVE FAT32 (read + write)
# =============================================================================
class Fat32RW(RWBackend):
    """Native FAT12/16/32 — read + write без внешних утилит."""

    ATTR_DIR = 0x10
    ATTR_LFN = 0x0F
    ATTR_VOL = 0x08

    def __init__(self, image: Path, offset: int = 0):
        super().__init__(image, offset)
        self._parse_bpb()

    def _parse_bpb(self) -> None:
        bs = self._read_at(0, 512)
        if bs[510:512] != b"\x55\xaa":
            raise ValueError("not a FAT boot sector")
        self.bytes_per_sector = struct.unpack("<H", bs[11:13])[0]
        self.sectors_per_cluster = bs[13]
        self.reserved_sectors = struct.unpack("<H", bs[14:16])[0]
        self.num_fats = bs[16]
        self.root_entries = struct.unpack("<H", bs[17:19])[0]
        self.total_sectors = (
            struct.unpack("<H", bs[19:21])[0] or struct.unpack("<I", bs[32:36])[0]
        )
        self.fat_size = (
            struct.unpack("<H", bs[22:24])[0] or struct.unpack("<I", bs[36:40])[0]
        )

        root_secs = (
            self.root_entries * 32 + self.bytes_per_sector - 1
        ) // self.bytes_per_sector
        data_secs = (
            self.total_sectors
            - self.reserved_sectors
            - self.num_fats * self.fat_size
            - root_secs
        )
        self.cluster_count = max(0, data_secs // self.sectors_per_cluster)

        if self.cluster_count < 4085:
            self.fat_type = 12
        elif self.cluster_count < 65525:
            self.fat_type = 16
        else:
            self.fat_type = 32

        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.root_dir_offset = (
            self.reserved_sectors + self.num_fats * self.fat_size
        ) * self.bytes_per_sector
        self.data_start = self.root_dir_offset + root_secs * self.bytes_per_sector

        if self.fat_type == 32:
            self.root_cluster = struct.unpack("<I", bs[44:48])[0] & 0x0FFFFFFF
            if self.root_cluster < 2:
                self.root_cluster = 2
            # FSINFO only caches the allocator state; the FAT stays the source
            # of truth.  The pointer is kept so the writer paths below can
            # refresh that cache - a stale free-cluster count is exactly what
            # `fsck.fat` reports as "Free cluster summary wrong".
            self.fsinfo_sector = struct.unpack("<H", bs[48:50])[0]
        else:
            self.root_cluster = 0
            self.fsinfo_sector = 0

    # ---- FSINFO ------------------------------------------------------------
    def free_clusters(self) -> tuple:
        """Count free clusters (and the first one) from the FAT itself."""
        free = 0
        first = 0xFFFFFFFF
        for n in range(2, self.cluster_count + 2):
            if self._fat_get(n) == 0:
                free += 1
                if first == 0xFFFFFFFF:
                    first = n
        return free, first

    def sync_fsinfo(self) -> bool:
        """Refresh the cached free-cluster count/next-free hint.

        Recounted from the FAT rather than incremented/decremented, so the cache
        cannot drift after a failed allocation or a partially applied delete.
        Volumes that declare no FSINFO sector, or one whose signatures do not
        match, are left untouched: writing counters into an unrelated sector
        would corrupt the image while "fixing" a cache.  Returns ``True`` when a
        valid FSINFO structure was found and updated.
        """
        if self.fat_type != 32 or not self.fsinfo_sector:
            return False
        off = self.fsinfo_sector * self.bytes_per_sector
        try:
            raw = self._read_at(off, self.bytes_per_sector)
        except (OSError, ValueError):
            return False
        if len(raw) < self.bytes_per_sector:
            return False
        if struct.unpack("<I", raw[0:4])[0] != 0x41615252:  # lead signature
            return False
        if struct.unpack("<I", raw[484:488])[0] != 0x61417272:  # structure
            return False
        if struct.unpack("<I", raw[508:512])[0] != 0xAA550000:  # trail
            return False
        free, first = self.free_clusters()
        buf = bytearray(raw)
        struct.pack_into("<I", buf, 488, free)
        struct.pack_into("<I", buf, 492, first)
        self._write_at(off, bytes(buf))
        return True

    # ---- FAT ---------------------------------------------------------------
    def _fat_get(self, cluster: int) -> int:
        if self.fat_type == 32:
            return (
                struct.unpack("<I", self._read_at(self.fat_offset + cluster * 4, 4))[0]
                & 0x0FFFFFFF
            )
        if self.fat_type == 16:
            return struct.unpack("<H", self._read_at(self.fat_offset + cluster * 2, 2))[
                0
            ]
        raw = self._read_at(self.fat_offset + (cluster * 3) // 2, 2)
        val = struct.unpack("<H", raw)[0]
        return (val >> 4) if (cluster & 1) else (val & 0x0FFF)

    def _fat_set(self, cluster: int, value: int) -> None:
        for i in range(self.num_fats):
            fat_base = self.fat_offset + i * self.fat_size * self.bytes_per_sector
            if self.fat_type == 32:
                self._write_at(
                    fat_base + cluster * 4, struct.pack("<I", value & 0x0FFFFFFF)
                )
            elif self.fat_type == 16:
                self._write_at(
                    fat_base + cluster * 2, struct.pack("<H", value & 0xFFFF)
                )
            else:
                off = fat_base + (cluster * 3) // 2
                old = struct.unpack("<H", self._read_at(off, 2))[0]
                if cluster & 1:
                    new = (old & 0x000F) | ((value & 0x0FFF) << 4)
                else:
                    new = (old & 0xF000) | (value & 0x0FFF)
                self._write_at(off, struct.pack("<H", new))

    def _cluster_chain(self, start: int, max_n: int = 100_000) -> list[int]:
        chain, seen, cur = [], set(), start
        while 2 <= cur < 0x0FFFFFF8 and cur not in seen and len(chain) < max_n:
            chain.append(cur)
            seen.add(cur)
            cur = self._fat_get(cur)
        return chain

    def _cluster_offset(self, cluster: int) -> int:
        return self.data_start + (cluster - 2) * self.cluster_size

    def _read_cluster(self, cluster: int) -> bytes:
        return self._read_at(self._cluster_offset(cluster), self.cluster_size)

    def _write_cluster(self, cluster: int, data: bytes) -> None:
        assert len(data) == self.cluster_size
        self._write_at(self._cluster_offset(cluster), data)

    def _read_chain(self, start: int, size: int | None = None) -> bytes:
        chain = self._cluster_chain(start)
        if not chain:
            return self._read_at(self._cluster_offset(start), size or self.cluster_size)
        data = b"".join(self._read_cluster(c) for c in chain)
        return data[:size] if size is not None else data

    # ---- allocation --------------------------------------------------------
    def _find_free_cluster(self, count: int = 1) -> list[int]:
        free: list[int] = []
        max_cluster = self.cluster_count + 2
        for c in range(2, max_cluster):
            if self._fat_get(c) == 0:
                free.append(c)
                if len(free) == count:
                    return free
        raise RuntimeError("not enough free clusters")

    def _allocate_chain(self, count: int) -> list[int]:
        if count == 0:
            return []
        clusters = self._find_free_cluster(count)
        for i, c in enumerate(clusters):
            self._fat_set(c, 0x0FFFFFFF if i == len(clusters) - 1 else clusters[i + 1])
        # zero fill
        zero = b"\x00" * self.cluster_size
        for c in clusters:
            self._write_cluster(c, zero)
        return clusters

    def _free_chain(self, start: int) -> None:
        for c in self._cluster_chain(start):
            self._fat_set(c, 0)

    def _write_chain(self, clusters: list[int], data: bytes) -> None:
        full = (len(data) + self.cluster_size - 1) // self.cluster_size
        if len(clusters) < full:
            raise RuntimeError("not enough clusters allocated")
        for i, c in enumerate(clusters[:full]):
            chunk = data[i * self.cluster_size : (i + 1) * self.cluster_size]
            chunk = chunk.ljust(self.cluster_size, b"\x00")
            self._write_cluster(c, chunk)

    # ---- directory ---------------------------------------------------------
    def _read_dir_raw(self, cluster: int) -> bytes:
        if self.fat_type == 32 or cluster >= 2:
            return self._read_chain(cluster)
        return self._read_at(self.root_dir_offset, self.root_entries * 32)

    def _write_dir_raw(self, cluster: int, data: bytes) -> None:
        if self.fat_type == 32 or cluster >= 2:
            chain = self._cluster_chain(cluster)
            if not chain:
                raise RuntimeError("empty dir chain")
            for i, c in enumerate(chain):
                chunk = data[i * self.cluster_size : (i + 1) * self.cluster_size]
                if not chunk:
                    break
                chunk = chunk.ljust(self.cluster_size, b"\x00")
                self._write_cluster(c, chunk)
        else:
            self._write_at(self.root_dir_offset, data)

    def _parse_dir_raw(self, data: bytes, base: str) -> list[FSEntry]:
        entries: list[FSEntry] = []
        lfn: list[int] = []
        lfn_checksum: int | None = None
        for i in range(0, len(data) - 31, 32):
            raw = data[i : i + 32]
            first = raw[0]
            if first == 0x00:
                break
            attr = raw[11]
            deleted = first == 0xE5
            if attr == 0x0F:
                if lfn_checksum is None or lfn_checksum != raw[13]:
                    lfn = []
                    lfn_checksum = raw[13]
                chunk = raw[1:11] + raw[14:26] + raw[28:32]
                units = [
                    struct.unpack("<H", chunk[j : j + 2])[0]
                    for j in range(0, len(chunk), 2)
                ]
                lfn = units + lfn
                continue
            if attr & self.ATTR_VOL:
                lfn = []
                lfn_checksum = None
                continue
            if lfn and lfn_checksum == self._short_checksum(raw[:11]):
                name = self._decode_lfn_units(lfn)
            else:
                n = raw[:8].rstrip(b" ")
                ext = raw[8:11].rstrip(b" ")
                if first == 0xE5 and n:
                    n = b"_" + n[1:]
                name = n.decode("cp437", "replace")
                if ext:
                    name += "." + ext.decode("cp437", "replace")
            lfn = []
            lfn_checksum = None
            if name in (".", "..", ""):
                continue
            is_dir = bool(attr & self.ATTR_DIR)
            cluster = struct.unpack("<H", raw[26:28])[0]
            if self.fat_type == 32:
                cluster |= struct.unpack("<H", raw[20:22])[0] << 16
                cluster &= 0x0FFFFFFF
            size = struct.unpack("<I", raw[28:32])[0]
            mtime = None
            date = struct.unpack("<H", raw[24:26])[0]
            tm = struct.unpack("<H", raw[22:24])[0]
            if date:
                try:
                    mtime = _dt.datetime(
                        1980 + ((date >> 9) & 0x7F),
                        (date >> 5) & 0x0F,
                        date & 0x1F,
                        (tm >> 11) & 0x1F,
                        (tm >> 5) & 0x3F,
                        (tm & 0x1F) * 2,
                    ).timestamp()
                except ValueError:
                    pass
            entries.append(
                FSEntry(
                    name=name,
                    path=base.rstrip("/") + "/" + name,
                    size=size,
                    is_dir=is_dir,
                    inode=cluster,
                    deleted=deleted,
                    mtime=mtime,
                )
            )
        return entries

    def _find_entry_slot(self, data: bytes, need_entries: int) -> int | None:
        """Найти свободное место для N*32 байт подряд."""
        run = 0
        start = None
        for i in range(0, len(data) - 31, 32):
            first = data[i]
            free = first in (0x00, 0xE5)
            if free:
                if run == 0:
                    start = i
                run += 1
                if run == need_entries:
                    return start
            else:
                run = 0
                start = None
        return None

    def _build_dir_entry(
        self,
        name: str,
        cluster: int,
        size: int,
        is_dir: bool,
        short_name: tuple[bytes, bytes] | None = None,
    ) -> bytes:
        short = short_name or self._short_name(name)
        attr = self.ATTR_DIR if is_dir else 0x20
        now = _dt.datetime.now()
        tm = (now.hour << 11) | (now.minute << 5) | (now.second // 2)
        date = ((now.year - 1980) << 9) | (now.month << 5) | now.day
        entry = bytearray(32)
        entry[0:8] = short[0].ljust(8)
        entry[8:11] = short[1].ljust(3)
        entry[11] = attr
        struct.pack_into("<H", entry, 22, tm)
        struct.pack_into("<H", entry, 24, date)
        struct.pack_into("<H", entry, 26, cluster & 0xFFFF)
        struct.pack_into("<I", entry, 28, size)
        if self.fat_type == 32:
            struct.pack_into("<H", entry, 20, (cluster >> 16) & 0xFFFF)
        return bytes(entry)

    def _short_name(self, name: str) -> tuple[bytes, bytes]:
        clean = name.upper()
        if "." in clean:
            base, ext = clean.rsplit(".", 1)
        else:
            base, ext = clean, ""
        base = "".join(c for c in base if c.isalnum() or c in "_~")[:8]
        ext = "".join(c for c in ext if c.isalnum())[:3]
        if not base:
            base = "FILE"
        return base.encode("cp437"), ext.encode("cp437")

    def _short_name_for_dir(self, name: str, data: bytes) -> bytes:
        used = {
            data[i : i + 11]
            for i in range(0, len(data) - 31, 32)
            if data[i] not in (0x00, 0xE5) and data[i + 11] != self.ATTR_LFN
        }
        if "." in name:
            base, ext = name.rsplit(".", 1)
        else:
            base, ext = name, ""
        base = "".join(
            c for c in base.upper() if c.isascii() and (c.isalnum() or c in "_~")
        ) or "FILE"
        ext = "".join(c for c in ext.upper() if c.isascii() and c.isalnum())[:3]
        direct = base[:8].ljust(8).encode("ascii") + ext.ljust(3).encode("ascii")
        if self._is_83_name(name) and direct not in used:
            return direct
        for number in range(1, 100000):
            suffix = f"~{number}"
            stem = base[: max(1, 8 - len(suffix))]
            candidate = (
                (stem + suffix).ljust(8).encode("ascii")
                + ext[:3].ljust(3).encode("ascii")
            )
            if candidate not in used:
                return candidate
        raise RuntimeError("cannot generate a unique FAT 8.3 alias")

    @staticmethod
    def _short_checksum(short_name: bytes) -> int:
        checksum = 0
        for value in short_name:
            checksum = (((checksum & 1) << 7) + (checksum >> 1) + value) & 0xFF
        return checksum

    @staticmethod
    def _decode_lfn_units(units: list[int]) -> str:
        if 0 in units:
            units = units[: units.index(0)]
        else:
            units = list(units)
            while units and units[-1] == 0xFFFF:
                units.pop()
        raw = b"".join(struct.pack("<H", unit) for unit in units)
        return raw.decode("utf-16le", "replace")

    def _lfn_entries(self, name: str, short_check: bytes) -> bytes:
        encoded_name = name.encode("utf-16le")
        units = [
            struct.unpack("<H", encoded_name[i : i + 2])[0]
            for i in range(0, len(encoded_name), 2)
        ]
        if len(units) > 255:
            raise ValueError("FAT long names cannot exceed 255 UTF-16 code units")
        units.append(0x0000)
        while len(units) % 13:
            units.append(0xFFFF)
        chunks = [units[i : i + 13] for i in range(0, len(units), 13)]
        short_check = short_check[:11].ljust(11, b" ")
        checksum = self._short_checksum(short_check)
        out = b""
        for i, chunk in enumerate(reversed(chunks)):
            seq = len(chunks) - i
            entry = bytearray(32)
            entry[0] = (seq | 0x40) if i == 0 else seq
            entry[11] = self.ATTR_LFN
            entry[13] = checksum
            cursor = 0
            for start, count in ((1, 5), (14, 6), (28, 2)):
                for slot in range(count):
                    struct.pack_into(
                        "<H", entry, start + 2 * slot, chunk[cursor]
                    )
                    cursor += 1
            out += bytes(entry)
        return out

    def _create_in_dir(
        self, dir_cluster: int, name: str, cluster: int, size: int, is_dir: bool
    ) -> None:
        data = bytearray(self._read_dir_raw(dir_cluster))
        need_lfn = not self._is_83_name(name)
        short_name = self._short_name_for_dir(name, bytes(data))
        lfn = self._lfn_entries(name, short_name) if need_lfn else b""
        lfn_count = len(lfn) // 32
        need = lfn_count + 1
        slot = self._find_entry_slot(bytes(data), need)
        if slot is None:
            raise RuntimeError(f"no free dir slot for {name}")
        if need_lfn:
            data[slot : slot + len(lfn)] = lfn
        short_parts = (short_name[:8], short_name[8:11])
        entry_offset = slot + len(lfn)
        data[entry_offset : entry_offset + 32] = self._build_dir_entry(
            name, cluster, size, is_dir, short_name=short_parts
        )
        self._write_dir_raw(dir_cluster, bytes(data))

    @staticmethod
    def _is_83_name(name: str) -> bool:
        if "." in name:
            base, ext = name.rsplit(".", 1)
        else:
            base, ext = name, ""
        return (
            len(base) <= 8
            and len(ext) <= 3
            and name.upper() == name
            and bool(base)
            and all(c.isalnum() or c == "_" for c in base + ext)
        )

    def _walk_to(self, path: str) -> tuple[int, str]:
        """Return (dir_cluster, basename) for the parent of `path`."""
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        if not parts:
            raise ValueError("invalid path")
        name = parts[-1]
        if len(parts) == 1:
            cluster = self.root_cluster if self.fat_type == 32 else 0
            return cluster, name
        cur_cluster = self.root_cluster if self.fat_type == 32 else 0
        cur_path = "/"
        for part in parts[:-1]:
            raw = self._read_dir_raw(cur_cluster)
            entries = self._parse_dir_raw(raw, cur_path)
            found = next((e for e in entries if e.name == part and e.is_dir), None)
            if not found:
                raise FileNotFoundError(f"dir not found: {part}")
            cur_cluster = found.inode
            cur_path = found.path
        return cur_cluster, name

    # ---- public API -------------------------------------------------------
    def info(self) -> FSInfo:
        return FSInfo(
            fs_type=f"fat{self.fat_type}",
            offset=self.offset,
            backend="native-fat-rw",
            total_size=self.total_sectors * self.bytes_per_sector,
            writable=True,
            write_strategy="native",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        path = str(path).replace("\\", "/")
        if path in ("/", ""):
            if self.fat_type == 32:
                data = self._read_chain(self.root_cluster)
            else:
                data = self._read_at(self.root_dir_offset, self.root_entries * 32)
            return self._parse_dir_raw(data, "/")
        try:
            parent_cluster, name = self._walk_to(path)
        except FileNotFoundError:
            return []
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        found = next((e for e in entries if e.name == name and e.is_dir), None)
        if not found:
            return []
        return self._parse_dir_raw(self._read_dir_raw(found.inode), found.path)

    def read(self, path: str) -> bytes:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        entry = next((e for e in entries if e.name == name), None)
        if not entry:
            raise FileNotFoundError(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return self._read_chain(entry.inode, entry.size)

    def write_file(self, path: str, data: bytes) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        existing = next((e for e in entries if e.name == name), None)
        if existing and existing.is_dir:
            raise IsADirectoryError(path)
        old_chain = (
            self._cluster_chain(existing.inode)
            if existing and existing.inode >= 2
            else []
        )
        clusters_needed = (len(data) + self.cluster_size - 1) // self.cluster_size
        clusters = self._allocate_chain(clusters_needed) if clusters_needed else []
        try:
            if clusters:
                self._write_chain(clusters, data)
        except Exception:
            # Keep an overwritten file's original chain intact until the new
            # data is fully written. A failed write must not destroy evidence.
            if clusters:
                self._free_chain(clusters[0])
            raise
        if existing:
            idx = self._find_entry_index(raw, existing, parent_path)
            if idx is None:
                if clusters:
                    self._free_chain(clusters[0])
                raise RuntimeError(f"directory entry not found while overwriting {path}")
            buf = bytearray(raw)
            first_cluster = clusters[0] if clusters else 0
            struct.pack_into("<H", buf, idx + 26, first_cluster & 0xFFFF)
            struct.pack_into(
                "<H", buf, idx + 20,
                (first_cluster >> 16) & 0xFFFF if self.fat_type == 32 else 0,
            )
            struct.pack_into("<I", buf, idx + 28, len(data))
            # If this directory write fails partway through, retain the new
            # chain: a partially updated entry may already point to it.
            self._write_dir_raw(parent_cluster, bytes(buf))
        else:
            first_cluster = clusters[0] if clusters else 0
            try:
                self._create_in_dir(parent_cluster, name, first_cluster, len(data), False)
            except ValueError:
                if clusters:
                    self._free_chain(clusters[0])
                raise
            except RuntimeError as exc:
                if clusters and str(exc).startswith("no free dir slot for "):
                    self._free_chain(clusters[0])
                raise
        # Commit point: the directory now points at the fully written data.
        # Reclaim the old clusters only after that point.
        if old_chain:
            self._free_chain(old_chain[0])
        self.sync_fsinfo()

    def _find_entry_index(self, raw: bytes, entry: FSEntry, base: str) -> int | None:
        lfn: list[int] = []
        lfn_checksum: int | None = None
        for i in range(0, len(raw) - 31, 32):
            first = raw[i]
            attr = raw[i + 11]
            if first == 0x00:
                break
            if first == 0xE5:
                lfn = []
                lfn_checksum = None
                continue
            if attr == 0x0F:
                if lfn_checksum is None or lfn_checksum != raw[i + 13]:
                    lfn = []
                    lfn_checksum = raw[i + 13]
                chunk = (
                    raw[i + 1 : i + 11] + raw[i + 14 : i + 26] + raw[i + 28 : i + 32]
                )
                units = [
                    struct.unpack("<H", chunk[j : j + 2])[0]
                    for j in range(0, len(chunk), 2)
                ]
                lfn = units + lfn
                continue
            if attr & self.ATTR_VOL:
                lfn = []
                lfn_checksum = None
                continue
            if lfn and lfn_checksum == self._short_checksum(raw[i : i + 11]):
                name = self._decode_lfn_units(lfn)
            else:
                n = raw[i : i + 8].rstrip(b" ")
                ext = raw[i + 8 : i + 11].rstrip(b" ")
                name = n.decode("cp437", "replace")
                if ext:
                    name += "." + ext.decode("cp437", "replace")
            lfn = []
            lfn_checksum = None
            if name == entry.name:
                return i
        return None

    def mkdir(self, path: str) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        if any(e.name == name for e in entries):
            raise FileExistsError(path)
        new_cluster = self._allocate_chain(1)[0]
        # init . and ..
        dot = bytearray(
            self._build_dir_entry(".", new_cluster, 0, True, short_name=(b".", b""))
        )
        # Microsoft's FAT specification: a directory created in the *root*
        # directory stores 0 in the ".." cluster field, because the root
        # directory has no cluster number of its own.  Storing the root cluster
        # number (2) instead produced images that `fsck.fat` rejects with
        # "Invalid '..' entry in the second slot" - a volume our own writer
        # (and MF-WE's verify()) reported as clean.
        dotdot_cluster = (
            0
            if self.fat_type == 32 and parent_cluster == self.root_cluster
            else parent_cluster
        )
        dotdot = bytearray(
            self._build_dir_entry(
                "..", dotdot_cluster, 0, True, short_name=(b"..", b"")
            )
        )
        buf = bytes(dot) + bytes(dotdot) + b"\x00" * (self.cluster_size - 64)
        self._write_cluster(new_cluster, buf)
        try:
            self._create_in_dir(parent_cluster, name, new_cluster, 0, True)
        except ValueError:
            self._free_chain(new_cluster)
            raise
        except RuntimeError as exc:
            if str(exc).startswith("no free dir slot for "):
                self._free_chain(new_cluster)
            raise
        self.sync_fsinfo()

    def delete(self, path: str, recursive: bool = False) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        entry = next((e for e in entries if e.name == name), None)
        if not entry:
            raise FileNotFoundError(path)
        if entry.is_dir:
            sub = self._parse_dir_raw(self._read_dir_raw(entry.inode), entry.path)
            if sub and not recursive:
                raise IsADirectoryError("use recursive=True")
            if recursive:
                for s in sub:
                    self.delete(s.path, recursive=True)
            self._free_chain(entry.inode)
        else:
            self._free_chain(entry.inode)
        # mark dir entry deleted
        idx = self._find_entry_index(raw, entry, parent_path)
        if idx is not None:
            self._write_dir_raw(parent_cluster, self._tombstone_entry_slots(raw, idx))
        self.sync_fsinfo()

    def _tombstone_entry_slots(self, raw: bytes, short_entry_index: int) -> bytes:
        start = short_entry_index
        while start >= 32:
            previous = start - 32
            if raw[previous] in (0x00, 0xE5) or raw[previous + 11] != self.ATTR_LFN:
                break
            start = previous
        updated = bytearray(raw)
        for offset in range(start, short_entry_index + 32, 32):
            updated[offset] = 0xE5
        return bytes(updated)

    def rename(self, src: str, dst: str) -> None:
        """Rename a same-directory 8.3 entry without changing its cluster chain.

        Long-filename and cross-directory renames are deliberately rejected
        until they can be implemented with an atomic directory-entry update.
        """
        src_parent, src_name = _image_path_parent_name(src)
        dst_parent, dst_name = _image_path_parent_name(dst)
        if src_parent != dst_parent:
            raise NotImplementedError("cross-directory rename not supported")
        parent_cluster, _ = self._walk_to(src)
        raw = self._read_dir_raw(parent_cluster)
        parent_path = src_parent
        entries = self._parse_dir_raw(raw, parent_path)
        if src_name.casefold() == dst_name.casefold():
            return
        entry = next((e for e in entries if e.name == src_name), None)
        if not entry:
            raise FileNotFoundError(src)
        if any(e.name.casefold() == dst_name.casefold() for e in entries):
            raise FileExistsError(dst)
        if not self._is_83_name(src_name) or not self._is_83_name(dst_name):
            raise NotImplementedError(
                "FAT rename currently supports only same-directory 8.3 names"
            )
        idx = self._find_entry_index(raw, entry, parent_path)
        if idx is None:
            raise RuntimeError(f"directory entry not found while renaming {src}")

        short = self._short_name(dst_name)
        short_raw = short[0].ljust(8, b" ") + short[1].ljust(3, b" ")
        for other_idx in range(0, len(raw) - 31, 32):
            if other_idx == idx:
                continue
            first = raw[other_idx]
            if first in (0x00, 0xE5) or raw[other_idx + 11] == self.ATTR_LFN:
                continue
            if raw[other_idx : other_idx + 11] == short_raw:
                raise FileExistsError(dst)

        # Updating the short-name bytes in place preserves the file's FAT
        # chain and all other directory metadata; the old implementation
        # called delete(), which freed the chain before recreating the entry.
        buf = bytearray(raw)
        buf[idx : idx + 11] = short_raw
        self._write_dir_raw(parent_cluster, bytes(buf))


# =============================================================================
# 5. NATIVE EXT2 (read+write через debugfs — простой subprocess-путь)
# =============================================================================
class Ext234RW(RWBackend):
    """
    ext2/3/4 — чтение через debugfs `ls/dump`, запись через `debugfs -w`.
    Требует e2fsprogs.
    """

    def __init__(self, image: Path, offset: int = 0):
        super().__init__(image, offset)
        self._check_ext()
        self._require_debugfs()

    def _check_ext(self) -> None:
        sb = self._read_at(1024, 1024)
        if sb[56:58] != b"\x53\xef":
            raise ValueError("not an ext superblock")
        self.feature_incompat = struct.unpack("<I", sb[96:100])[0]
        self.feature_compat = struct.unpack("<I", sb[92:96])[0]
        self.label = sb[120:136].rstrip(b"\x00").decode("utf-8", "replace")
        self.blocks_count = struct.unpack("<I", sb[4:8])[0]
        self.block_size = 1024 << struct.unpack("<I", sb[24:28])[0]
        self.is_ext4 = bool(
            self.feature_incompat & 0x40 or self.feature_incompat & 0x80
        )
        self.is_ext3 = (self.feature_compat & 0x4) and not self.is_ext4
        self._tmp: Path | None = None

    @staticmethod
    def _require_debugfs() -> None:
        if shutil.which("debugfs") is None:
            raise RuntimeError(
                "e2fsprogs not installed (debugfs required for ext write)"
            )

    def _ensure_local(self) -> Path:
        """debugfs работает с raw-образом; если offset != 0 — извлечь."""
        if self.offset == 0:
            return self.image
        if self._tmp is None:
            self._tmp = Path(tempfile.mkstemp(suffix=".ext")[1])
            with self.image.open("rb") as src, self._tmp.open("wb") as dst:
                src.seek(self.offset)
                shutil.copyfileobj(src, dst)
        return self._tmp

    def _debugfs(self, *args: str, write: bool = False) -> str:
        img = self._ensure_local()
        cmd = ["debugfs"]
        if write:
            cmd.append("-w")
        cmd.extend(["-R", args[0], str(img)])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"debugfs failed: {r.stderr}")
        return r.stdout

    def _flush_tmp(self) -> None:
        if self._tmp and self.offset > 0:
            with self.image.open("r+b") as dst:
                dst.seek(self.offset)
                with self._tmp.open("rb") as src:
                    shutil.copyfileobj(src, dst)

    # ---- public API -------------------------------------------------------
    def info(self) -> FSInfo:
        name = "ext4" if self.is_ext4 else "ext3" if self.is_ext3 else "ext2"
        return FSInfo(
            fs_type=name,
            offset=self.offset,
            backend="debugfs-rw",
            label=self.label,
            total_size=self.blocks_count * self.block_size,
            writable=True,
            write_strategy="debugfs",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        out = self._debugfs(f"ls -l {path}")
        entries: list[FSEntry] = []
        for line in out.splitlines():
            m = line.strip().split("/", 6)
            # debugfs: `ls -l /` формат: inode mode uid gid size date name/
            parts = line.split()
            if len(parts) < 8 or parts[0] == "debugfs:":
                continue
            try:
                inode = int(parts[0])
                mode = int(parts[1], 8)
            except ValueError:
                continue
            name = parts[7] if len(parts) > 7 else parts[-1]
            is_dir = name.endswith("/")
            name = name.rstrip("/")
            if name in (".", ".."):
                continue
            try:
                size = int(parts[4])
            except (ValueError, IndexError):
                size = 0
            entries.append(
                FSEntry(
                    name=name,
                    path=path.rstrip("/") + "/" + name,
                    size=size,
                    is_dir=is_dir,
                    inode=inode,
                    mode=mode,
                )
            )
        return entries

    def read(self, path: str) -> bytes:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = Path(tf.name)
        try:
            self._debugfs(f"dump {path} {tmp}")
            return tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

    def write_file(self, path: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = Path(tf.name)
        try:
            # удалить если существует
            self._debugfs(f"rm {path}")
            self._debugfs(f"write {tmp} {path}", write=True)
            self._flush_tmp()
        finally:
            tmp.unlink(missing_ok=True)

    def mkdir(self, path: str) -> None:
        self._debugfs(f"mkdir {path}", write=True)
        self._flush_tmp()

    def delete(self, path: str, recursive: bool = False) -> None:
        if recursive:
            self._debugfs(f"rmdir {path}")
        else:
            self._debugfs(f"rm {path}", write=True)
            self._flush_tmp()

    def rename(self, src: str, dst: str) -> None:
        self._debugfs(f"rename {src} {dst}", write=True)
        self._flush_tmp()


# =============================================================================
# 6. NTFS via subprocess (ntfs-3g / ntfsclone)
# =============================================================================
class NtfsRW(RWBackend):
    """NTFS — чтение через pytsk3, запись через loop-mount ntfs-3g."""

    def __init__(self, image: Path, offset: int = 0):
        super().__init__(image, offset)
        if pytsk3 is None:
            raise RuntimeError("pytsk3 required for NTFS")
        self._img = pytsk3.Img_Info(str(image))
        self._fs = pytsk3.FS_Info(self._img, offset=offset)

    def _mount_point(self) -> Path:
        if not sys.platform.startswith("linux"):
            raise NotImplementedError("NTFS write requires Linux + ntfs-3g")
        if shutil.which("ntfs-3g") is None:
            raise RuntimeError("ntfs-3g not installed")
        mp = Path(tempfile.mkdtemp(prefix="mnt_"))
        cmd = ["ntfs-3g", "-o", f"loop,offset={self.offset},rw"]
        cmd += [str(self.image), str(mp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ntfs-3g mount failed: {r.stderr}")
        return mp

    def _unmount(self, mp: Path) -> None:
        subprocess.run(["umount", str(mp)], capture_output=True)
        shutil.rmtree(mp, ignore_errors=True)

    def info(self) -> FSInfo:
        return FSInfo(
            fs_type="ntfs",
            offset=self.offset,
            backend="ntfs-3g-rw",
            total_size=self.image.stat().st_size,
            writable=sys.platform.startswith("linux"),
            write_strategy="ntfs-3g",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        out = self._fs.open_dir(path=path)
        entries: list[FSEntry] = []
        for e in out:
            name_b = e.info.name.name
            if name_b in (b".", b".."):
                continue
            meta = e.info.meta
            if meta is None:
                continue
            entries.append(
                FSEntry(
                    name=name_b.decode("utf-8", "replace"),
                    path=path.rstrip("/") + "/" + name_b.decode(),
                    size=meta.size,
                    is_dir=(meta.type == pytsk3.TSK_FS_META_TYPE_DIR),
                    inode=meta.addr,
                    deleted=bool(meta.flags & pytsk3.TSK_FS_META_FLAG_UNALLOC),
                    mtime=float(meta.mtime) if meta.mtime else None,
                )
            )
        return entries

    def read(self, path: str) -> bytes:
        f = self._fs.open(path=path)
        return f.read_random(0, f.info.meta.size)

    def _do_mounted(self, fn) -> None:
        mp = self._mount_point()
        try:
            fn(mp)
        finally:
            self._unmount(mp)

    def write_file(self, path: str, data: bytes) -> None:
        def _do(mp: Path):
            dst = mp / path.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

        self._do_mounted(_do)

    def mkdir(self, path: str) -> None:
        self._do_mounted(
            lambda mp: (mp / path.lstrip("/")).mkdir(parents=True, exist_ok=True)
        )

    def delete(self, path: str, recursive: bool = False) -> None:
        def _do(mp: Path):
            p = mp / path.lstrip("/")
            if p.is_dir():
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do_mounted(_do)

    def rename(self, src: str, dst: str) -> None:
        self._do_mounted(lambda mp: (mp / src.lstrip("/")).rename(mp / dst.lstrip("/")))


# =============================================================================
# 7. LOOP-MOUNT fallback (любая ФС)
# =============================================================================
class LoopMountRW(RWBackend):
    """Универсальная запись через `mount -o loop,rw` (Linux/macOS)."""

    def __init__(self, image: Path, offset: int, fs_type: str):
        super().__init__(image, offset)
        self.fs_type = fs_type

    def _mount_point(self) -> Path:
        if shutil.which("mount") is None:
            raise RuntimeError("mount not available")
        mp = Path(tempfile.mkdtemp(prefix="mnt_"))
        opts = f"loop,offset={self.offset},rw"
        if self.fs_type in ("vfat", "fat", "fat32", "exfat"):
            cmd = ["mount", "-t", self.fs_type, "-o", opts, str(self.image), str(mp)]
        else:
            cmd = ["mount", "-t", self.fs_type, "-o", opts, str(self.image), str(mp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"mount failed: {r.stderr}")
        return mp

    def _unmount(self, mp: Path) -> None:
        subprocess.run(["umount", str(mp)], capture_output=True)
        shutil.rmtree(mp, ignore_errors=True)

    def _do(self, fn) -> None:
        mp = self._mount_point()
        try:
            fn(mp)
        finally:
            self._unmount(mp)

    def info(self) -> FSInfo:
        return FSInfo(
            fs_type=self.fs_type,
            offset=self.offset,
            backend="loop-mount-rw",
            total_size=self.image.stat().st_size,
            writable=sys.platform.startswith(("linux", "darwin")),
            write_strategy="loop-mount",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        def _do(mp: Path) -> list[FSEntry]:
            p = mp / path.lstrip("/")
            if not p.exists():
                return []
            entries: list[FSEntry] = []
            for child in p.iterdir():
                st = child.stat()
                entries.append(
                    FSEntry(
                        name=child.name,
                        path=path.rstrip("/") + "/" + child.name,
                        size=st.st_size,
                        is_dir=child.is_dir(),
                        mtime=st.st_mtime,
                    )
                )
            return entries

        result: list[FSEntry] = []
        self._do(lambda mp: result.extend(_do(mp)))
        return result

    def read(self, path: str) -> bytes:
        result: list[bytes] = []
        self._do(lambda mp: result.append((mp / path.lstrip("/")).read_bytes()))
        return result[0]

    def write_file(self, path: str, data: bytes) -> None:
        self._do(lambda mp: (mp / path.lstrip("/")).write_bytes(data))

    def mkdir(self, path: str) -> None:
        self._do(lambda mp: (mp / path.lstrip("/")).mkdir(parents=True))

    def delete(self, path: str, recursive: bool = False) -> None:
        def _do(mp: Path):
            p = mp / path.lstrip("/")
            if p.is_dir():
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do(_do)

    def rename(self, src: str, dst: str) -> None:
        self._do(lambda mp: (mp / src.lstrip("/")).rename(mp / dst.lstrip("/")))


# =============================================================================
# 8. REBUILD fallback (распаковать → mkfs → залить → заменить)
# =============================================================================
class RebuildRW(RWBackend):
    """Fallback: полная пересборка образа через mkfs + копирование."""

    MKFS = {
        "ext2": ["mkfs.ext2", "-F"],
        "ext3": ["mkfs.ext3", "-F"],
        "ext4": ["mkfs.ext4", "-F"],
        "ntfs": ["mkfs.ntfs", "-F", "-Q"],
        "vfat": ["mkfs.vfat", "-F", "32"],
        "fat32": ["mkfs.vfat", "-F", "32"],
    }

    def __init__(self, reader: RWBackend, fs_type: str):
        self.reader = reader
        self.fs_type = fs_type
        self.image = reader.image
        self.offset = reader.offset
        self._fh = None

    def info(self) -> FSInfo:
        info = self.reader.info()
        return FSInfo(
            fs_type=info.fs_type,
            offset=self.offset,
            backend="rebuild-rw",
            label=info.label,
            total_size=info.total_size,
            writable=True,
            write_strategy="rebuild",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        return self.reader.list(path)

    def read(self, path: str) -> bytes:
        return self.reader.read(path)

    def _rebuild_with(self, fn) -> None:
        stage = Path(tempfile.mkdtemp(prefix="rebuild_"))
        try:
            # распаковать всё
            for e in self.reader.walk("/"):
                dst = stage / e.path.lstrip("/")
                if e.is_dir:
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(self.reader.read(e.path))
                    except Exception as ex:
                        log.warning("skip %s: %s", e.path, ex)
            # применить операцию
            fn(stage)
            # mkfs + залить
            new_img = Path(tempfile.mkstemp(suffix=".img")[1])
            size = self.image.stat().st_size
            new_img.write_bytes(b"\x00" * min(size, 4096))
            with new_img.open("r+b") as f:
                f.truncate(size)
            mkfs = self.MKFS.get(self.fs_type)
            if not mkfs:
                raise RuntimeError(f"no mkfs for {self.fs_type}")
            r = subprocess.run(
                mkfs + [str(new_img)],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"mkfs failed: {r.stderr}")
            self._fill(stage, new_img)
            new_img.replace(self.image)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _fill(self, stage: Path, image: Path) -> None:
        mp = Path(tempfile.mkdtemp(prefix="mnt_"))
        try:
            r = subprocess.run(
                ["mount", "-o", "loop", str(image), str(mp)],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"mount failed: {r.stderr}")
            for item in stage.iterdir():
                dst = mp / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            subprocess.run(["sync"])
        finally:
            subprocess.run(["umount", str(mp)], capture_output=True)
            shutil.rmtree(mp, ignore_errors=True)

    def write_file(self, path: str, data: bytes) -> None:
        target = path

        def _do(stage: Path):
            dst = stage / target.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

        self._rebuild_with(_do)

    def mkdir(self, path: str) -> None:
        self._rebuild_with(lambda stage: (stage / path.lstrip("/")).mkdir(parents=True))

    def delete(self, path: str, recursive: bool = False) -> None:
        def _do(stage: Path):
            p = stage / path.lstrip("/")
            if p.is_dir():
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._rebuild_with(_do)

    def rename(self, src: str, dst: str) -> None:
        self._rebuild_with(
            lambda stage: (stage / src.lstrip("/")).rename(stage / dst.lstrip("/"))
        )


# =============================================================================
# 9. ФАСАД: UniversalFSRW
# =============================================================================
class UniversalFSRW:
    """
    Единый фасад чтения/записи с авто-выбором backend.

    >>> with UniversalFSRW("dump.img") as fs:
    ...     print(fs.info())
    ...     fs.write_file("/new.txt", b"Hello")
    ...     fs.flush()   # fsck + audit
    """

    def __init__(
        self, image: Path, offset: int | None = None, auto_backup: bool = True
    ):
        self.image = Path(image)
        self.offset = offset
        self.backend: RWBackend | None = None
        self.journal = RWJournal(self.image)
        self.backup = ImageBackup(self.image)
        self._auto_backup = auto_backup
        self._backup_path: Path | None = None
        self._open()

    def _open(self) -> None:
        # ``mountfile.universal_fs`` was never part of the package.  Use the
        # maintained signature/partition detector instead, keeping detection
        # read-only and avoiding a misleading universal backend claim.
        from .firmware import detect_volumes, probe_filesystem

        if self.offset is not None:
            fs_type = probe_filesystem(self.image, self.offset) or "unknown"
            off = self.offset
        else:
            volumes = detect_volumes(self.image)
            candidate = next((v for v in volumes if v.fstype != "raw"), None)
            if candidate is None:
                # detect_volumes only sees signatures inside the scan window;
                # the header at offset 0 is still worth one direct probe, and
                # synthetic/small fixtures rely on exactly that fallback.
                direct = probe_filesystem(self.image, 0)
                if not direct or direct.lower() == "raw":
                    raise RuntimeError("no filesystem detected")
                fs_type, off = direct, 0
            else:
                fs_type, off = candidate.fstype, candidate.offset
            self.offset = off
        # ``probe_filesystem`` returns display names such as ``FAT32`` or
        # ``NTFS`` while the backend table is lowercase, so the match below
        # must be case-insensitive.
        fs_type_lower = str(fs_type).lower()
        for cls, types in [
            (Fat32RW, ("fat", "fat12", "fat16", "fat32")),
            (Ext234RW, ("ext2", "ext3", "ext4")),
            (NtfsRW, ("ntfs",)),
        ]:
            if fs_type_lower.startswith(types):
                try:
                    self.backend = cls(self.image, off)
                    return
                except Exception as e:
                    log.warning("%s failed: %s", cls.__name__, e)
        # Last resort remains opt-in through an actually detected filesystem.
        try:
            self.backend = LoopMountRW(self.image, off, fs_type)
        except Exception as exc:
            raise RuntimeError(f"no RW backend for {fs_type} at 0x{off:X}") from exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self.backend:
            self.backend.close()

    # ---- read/write -------------------------------------------------------
    def info(self) -> FSInfo:
        return self.backend.info()

    def list(self, path: str = "/") -> list[FSEntry]:
        return self.backend.list(path)

    def read(self, path: str) -> bytes:
        return self.backend.read(path)

    def extract(self, path: str, dest: Path) -> Path:
        return self.backend.extract(path, dest)

    def walk(self, path: str = "/") -> Iterator[FSEntry]:
        yield from self.backend.walk(path)

    def _audit_and_backup(self, op: WriteOperation) -> None:
        if self._auto_backup and self._backup_path is None:
            self._backup_path = self.backup.create()
        self.journal.log(op)

    def write_file(self, path: str, data: bytes) -> None:
        op = WriteOperation("write", path, {"size": len(data)})
        self._audit_and_backup(op)
        self.backend.write_file(path, data)

    def mkdir(self, path: str) -> None:
        op = WriteOperation("mkdir", path)
        self._audit_and_backup(op)
        self.backend.mkdir(path)

    def delete(self, path: str, recursive: bool = False) -> None:
        op = WriteOperation("delete", path, {"recursive": recursive})
        self._audit_and_backup(op)
        self.backend.delete(path, recursive)

    def rename(self, src: str, dst: str) -> None:
        op = WriteOperation("rename", src, {"dst": dst})
        self._audit_and_backup(op)
        self.backend.rename(src, dst)

    # ---- maintenance ------------------------------------------------------
    def flush(self) -> dict:
        """fsck + sync. Возвращает статус."""
        result = {"fsck": None, "sync": None}
        fs_type = self.info().fs_type
        if fs_type.startswith(("ext",)) and shutil.which("e2fsck"):
            r = subprocess.run(
                ["e2fsck", "-f", "-y", str(self.image)],
                capture_output=True,
                text=True,
            )
            result["fsck"] = {"exit": r.returncode, "out": r.stdout[-1000:]}
        elif fs_type.startswith("fat") and shutil.which("fsck.fat"):
            r = subprocess.run(
                ["fsck.fat", "-a", str(self.image)],
                capture_output=True,
                text=True,
            )
            result["fsck"] = {"exit": r.returncode, "out": r.stdout[-1000:]}
        if hasattr(os, "sync"):
            os.sync()
            result["sync"] = "ok"
        return result

    def restore_from_backup(self) -> bool:
        latest = self.backup.latest()
        if not latest:
            return False
        shutil.copy2(latest, self.image)
        log.info("restored from %s", latest)
        return True


# =============================================================================
# 10. CLI
# =============================================================================
def _cli() -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(prog="universal_fs_rw")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for cmd in ("info", "ls", "cat", "write", "mkdir", "rm", "mv"):
        p = sub.add_parser(cmd)
        p.add_argument("image")
        if cmd == "ls":
            p.add_argument("path", nargs="?", default="/")
        elif cmd == "cat":
            p.add_argument("path")
        elif cmd == "write":
            p.add_argument("path")
            p.add_argument("source")
        elif cmd == "mkdir":
            p.add_argument("path")
        elif cmd == "rm":
            p.add_argument("path")
            p.add_argument("--recursive", "-r", action="store_true")
        elif cmd == "mv":
            p.add_argument("src")
            p.add_argument("dst")

    sub.add_parser("backup").add_argument("image")
    sub.add_parser("restore").add_argument("image")
    sub.add_parser("flush").add_argument("image")

    args = ap.parse_args()

    if args.cmd == "backup":
        p = ImageBackup(Path(args.image)).create()
        print(p)
        return 0
    if args.cmd == "restore":
        b = ImageBackup(Path(args.image))
        print("restored" if b.latest() else "no backups")
        if b.latest():
            shutil.copy2(b.latest(), args.image)
        return 0

    with UniversalFSRW(Path(args.image)) as fs:
        if args.cmd == "info":
            print(fs.info())
        elif args.cmd == "ls":
            for e in fs.list(args.path):
                print(f"{'D' if e.is_dir else 'F'} {e.size:>10}  {e.name}")
        elif args.cmd == "cat":
            sys.stdout.buffer.write(fs.read(args.path))
        elif args.cmd == "write":
            fs.write_file(args.path, Path(args.source).read_bytes())
            print(f"written {args.path}")
        elif args.cmd == "mkdir":
            fs.mkdir(args.path)
            print(f"mkdir {args.path}")
        elif args.cmd == "rm":
            fs.delete(args.path, args.recursive)
            print(f"rm {args.path}")
        elif args.cmd == "mv":
            fs.rename(args.src, args.dst)
            print(f"mv {args.src} -> {args.dst}")
        elif args.cmd == "flush":
            print(fs.flush())
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
