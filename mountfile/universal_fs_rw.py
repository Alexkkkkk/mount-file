#!/usr/bin/env python3
"""
mountfile/universal_fs_rw.py — Universal Read/Write Layer (single file)
=======================================================================

Расширяет UniversalFS записью через стратегии с явными ограничениями:

  1. NativeFat32Writer  — FAT12/16/32, прямая правка FAT + LFN + каталогов
  2. NativeExt2Writer   — ограниченный clean ext2 profile, in-process Python
  3. ExtDebugfsWriter   — явный legacy ext2/3/4 compatibility backend через `debugfs -w`
  4. NtfsWriter         — NTFS через `ntfs-3g` (Linux) или ImDisk (Windows)
  5. LoopMountWriter    — Linux loop-mount или Windows ImDisk для NTFS/FAT/exFAT
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
    pytsk3, ntfs-3g, ImDisk Virtual Disk Driver, e2fsprogs (legacy debugfs/checker), util-linux,
    dosfstools (mkfs.vfat, fsck.fat)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import shutil
import stat
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

from mountfile.rw.journal import Journal as _VerifiedJournal

try:
    import pytsk3
except ImportError:
    pytsk3 = None

log = logging.getLogger("mountfile.universal_fs_rw")

# These formats can be inspected/carved, but they do not have a writable
# filesystem semantics that a loop mount can safely provide.  Refusing a
# mutation is preferable to mounting them with a misleading ``rw`` option and
# reporting success after the kernel silently kept the image read-only.
INHERENTLY_READ_ONLY_FS = frozenset({"iso9660", "iso", "squashfs", "jffs2", "ubifs"})


def _normalise_mount_fs(fs_type: str) -> str:
    """Map detector display names to the names accepted by ``mount -t``."""
    value = str(fs_type or "").strip().lower()
    return {
        "hfs+": "hfsplus",
        "hfs": "hfsplus",
        "fat12": "vfat",
        "fat16": "vfat",
        "fat32": "vfat",
        "exfat": "exfat",
    }.get(value, value)


WINDOWS_NATIVE_RW_FS = frozenset(
    {"ntfs", "vfat", "fat", "fat12", "fat16", "fat32", "exfat"}
)


def _find_imdisk() -> str | None:
    """Find the ImDisk CLI without assuming its install directory."""
    executable = shutil.which("imdisk.exe") or shutil.which("imdisk")
    if executable:
        return executable
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        candidate = Path(system_root) / "System32" / "imdisk.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_is_admin() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _imdisk_status(fs_type: str) -> tuple[str, str]:
    """Describe whether Windows can safely mount this filesystem read/write."""
    if not sys.platform.startswith("win"):
        return "read-only-platform", "ImDisk image writing is available only on Windows"
    fs_name = _normalise_mount_fs(fs_type)
    if fs_name not in WINDOWS_NATIVE_RW_FS:
        return (
            "read-only-platform",
            "Windows native image writing does not support %s" % fs_type,
        )
    if not _find_imdisk():
        return (
            "read-only-imdisk-tools",
            "ImDisk Virtual Disk Driver is required for Windows image writing",
        )
    if not _windows_is_admin():
        return (
            "read-only-admin",
            "ImDisk image writing requires MountFile to run as Administrator",
        )
    return "imdisk-rw", ""


def _available_windows_drive_letter() -> str:
    """Choose a currently unused drive letter for a temporary ImDisk mount."""
    import ctypes

    used = int(ctypes.windll.kernel32.GetLogicalDrives())
    for letter in reversed("ZYXWVUTSRQPONMLKJIHGFED"):
        bit = ord(letter) - ord("A")
        if not used & (1 << bit):
            return letter + ":"
    raise RuntimeError("No free Windows drive letter is available for ImDisk")


def _imdisk_mount(image: Path, offset: int) -> tuple[str, Path]:
    """Attach one detected volume using ImDisk and Windows' native FS driver.

    Deliberately omit ImDisk's ``-s`` option: for file-backed devices it can
    resize the backing image. The OS filesystem driver observes the volume's
    own BPB length; ``-b`` confines the virtual device to the detected start.
    """
    executable = _find_imdisk()
    if not executable:
        raise RuntimeError(
            "Install ImDisk Virtual Disk Driver from https://ltr-data.se/opencode.html/"
        )
    if not _windows_is_admin():
        raise RuntimeError(
            "Run MountFile as Administrator to write an image through ImDisk"
        )
    if offset < 0 or offset >= image.stat().st_size:
        raise ValueError("filesystem offset is outside the image")

    drive = _available_windows_drive_letter()
    command = [
        executable,
        "-a",
        "-t",
        "file",
        "-f",
        str(image),
        "-b",
        str(offset),
        "-m",
        drive,
        "-o",
        "rw,shared",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "ImDisk could not attach the image. Check Administrator rights, "
            "volume state and offset. %s" % detail
        )
    return drive, Path(drive + "\\")


def _imdisk_unmount(drive: str) -> None:
    executable = _find_imdisk()
    if not executable:
        raise RuntimeError("ImDisk CLI disappeared before the volume was detached")
    result = subprocess.run(
        [executable, "-d", "-m", drive], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError("ImDisk could not safely detach %s: %s" % (drive, detail))


def _image_path_parent_name(path: str) -> tuple[str, str]:
    """Split a filesystem-in-image path using FAT/POSIX separators on every OS."""
    image_path = PurePosixPath(str(path).replace("\\", "/"))
    parent = str(image_path.parent)
    if parent in ("", "."):
        parent = "/"
    return parent, image_path.name


def _safe_mounted_path(mount_point: Path, path: str) -> Path:
    """Resolve an image-relative path without following links outside the volume."""
    relative = PurePosixPath(str(path).replace("\\", "/").lstrip("/"))
    if any(
        part in ("..", "") or ":" in part or "\x00" in part for part in relative.parts
    ):
        raise ValueError("unsafe path in mounted filesystem: %r" % path)

    current = Path(mount_point).resolve()
    for part in relative.parts:
        candidate = current / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            current = candidate
            continue
        reparse_point = bool(getattr(info, "st_file_attributes", 0) & 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse_point:
            raise PermissionError(
                "refusing to follow a link/reparse point inside mounted image: %s"
                % candidate
            )
        current = candidate
    return current


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


JournalV2 = _VerifiedJournal


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
        base = (
            "".join(
                c for c in base.upper() if c.isascii() and (c.isalnum() or c in "_~")
            )
            or "FILE"
        )
        ext = "".join(c for c in ext.upper() if c.isascii() and c.isalnum())[:3]
        direct = base[:8].ljust(8).encode("ascii") + ext.ljust(3).encode("ascii")
        if self._is_83_name(name) and direct not in used:
            return direct
        for number in range(1, 100000):
            suffix = f"~{number}"
            stem = base[: max(1, 8 - len(suffix))]
            candidate = (stem + suffix).ljust(8).encode("ascii") + ext[:3].ljust(
                3
            ).encode("ascii")
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
                    struct.pack_into("<H", entry, start + 2 * slot, chunk[cursor])
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
                raise RuntimeError(
                    f"directory entry not found while overwriting {path}"
                )
            buf = bytearray(raw)
            first_cluster = clusters[0] if clusters else 0
            struct.pack_into("<H", buf, idx + 26, first_cluster & 0xFFFF)
            struct.pack_into(
                "<H",
                buf,
                idx + 20,
                (first_cluster >> 16) & 0xFFFF if self.fat_type == 32 else 0,
            )
            struct.pack_into("<I", buf, idx + 28, len(data))
            # If this directory write fails partway through, retain the new
            # chain: a partially updated entry may already point to it.
            self._write_dir_raw(parent_cluster, bytes(buf))
        else:
            first_cluster = clusters[0] if clusters else 0
            try:
                self._create_in_dir(
                    parent_cluster, name, first_cluster, len(data), False
                )
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
# 5. EXT2/3/4 (native ext2 phase 1; explicit debugfs compatibility)
# =============================================================================
class Ext234RW(RWBackend):
    """
    ext2 uses MountFile's bounded in-process writer when its clean feature
    profile is supported. The e2fsprogs debugfs compatibility path is disabled
    unless the library caller explicitly sets ``allow_legacy_debugfs=True``.
    """

    def __init__(
        self,
        image: Path,
        offset: int = 0,
        volume_size: int | None = None,
        *,
        allow_legacy_debugfs: bool = False,
    ):
        super().__init__(image, offset)
        self.volume_size = int(volume_size) if volume_size is not None else None
        self.allow_legacy_debugfs = bool(allow_legacy_debugfs)
        self._native = None
        self._native_rejection = ""
        from .ext2_native import NativeExt2ImageWriter, NativeExt2UnsafeError

        try:
            self._native = NativeExt2ImageWriter(
                self.image,
                offset=self.offset,
                volume_size=self.volume_size,
            )
        except NativeExt2UnsafeError:
            # Do not let debugfs or a generic loop mount bypass failed native
            # geometry/clean-state checks for an ext2 image.
            raise
        except Exception as exc:
            self._native = None
            self._native_rejection = str(exc)
        self._check_ext()
        if self._native is None:
            if not self.allow_legacy_debugfs:
                raise RuntimeError(
                    "native ext2 profile rejected this volume; legacy debugfs "
                    "compatibility requires allow_legacy_debugfs=True: %s"
                    % self._native_rejection
                )
            self._require_debugfs()
        else:
            self._tmp = None
            self._tmp_source_hash = None

    def _check_ext(self) -> None:
        sb = self._read_at(1024, 1024)
        if sb[56:58] != b"\x53\xef":
            raise ValueError("not an ext superblock")
        self.feature_incompat = struct.unpack("<I", sb[96:100])[0]
        self.feature_compat = struct.unpack("<I", sb[92:96])[0]
        self.label = sb[120:136].rstrip(b"\x00").decode("utf-8", "replace")
        self.blocks_count = struct.unpack("<I", sb[4:8])[0]
        self.block_size = 1024 << struct.unpack("<I", sb[24:28])[0]
        if self.feature_incompat & 0x80:
            self.blocks_count |= struct.unpack("<I", sb[0x150:0x154])[0] << 32
        self.filesystem_size = self.blocks_count * self.block_size
        from .ext2_native import NativeExt2UnsafeError

        if self.volume_size is not None and self.filesystem_size > self.volume_size:
            raise NativeExt2UnsafeError(
                "ext filesystem extent exceeds selected partition bounds"
            )
        if self.offset + self.filesystem_size > self.image.stat().st_size:
            raise NativeExt2UnsafeError("ext filesystem extent exceeds image bounds")
        self.is_ext4 = bool(
            self.feature_incompat & 0x40 or self.feature_incompat & 0x80
        )
        self.is_ext3 = (self.feature_compat & 0x4) and not self.is_ext4
        self._tmp: Path | None = None
        self._tmp_source_hash: bytes | None = None

    @staticmethod
    def _require_debugfs() -> None:
        if shutil.which("debugfs") is None:
            raise RuntimeError(
                "e2fsprogs not installed (debugfs required for ext write)"
            )

    def _ensure_local(self) -> Path:
        """Stage exactly one filesystem extent for explicit debugfs compatibility."""
        if self.offset == 0:
            return self.image
        if self._tmp is None:
            self._tmp = Path(tempfile.mkstemp(suffix=".ext")[1])
            with self.image.open("rb") as src, self._tmp.open("wb") as dst:
                src.seek(self.offset)
                remaining = self.filesystem_size
                digest = hashlib.sha256()
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError(
                            "short read while staging ext filesystem extent"
                        )
                    dst.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            self._tmp_source_hash = digest.digest()
        return self._tmp

    def _extent_hash(self, path: Path, offset: int) -> bytes:
        digest = hashlib.sha256()
        remaining = self.filesystem_size
        with path.open("rb") as stream:
            stream.seek(offset)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("short read while hashing ext filesystem extent")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.digest()

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
            if self._tmp.stat().st_size != self.filesystem_size:
                raise RuntimeError("debugfs staging extent has an unexpected size")
            if self._tmp_source_hash != self._extent_hash(self.image, self.offset):
                raise RuntimeError(
                    "image changed concurrently; refusing legacy ext commit"
                )
            with self.image.open("r+b") as dst:
                dst.seek(self.offset)
                with self._tmp.open("rb") as src:
                    remaining = self.filesystem_size
                    while remaining:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError("debugfs staging extent is truncated")
                        if dst.write(chunk) != len(chunk):
                            raise RuntimeError(
                                "short write while committing ext filesystem extent"
                            )
                        remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            self._tmp_source_hash = self._extent_hash(self.image, self.offset)

    # ---- public API -------------------------------------------------------
    def info(self) -> FSInfo:
        if self._native is not None:
            return FSInfo(
                fs_type="ext2",
                offset=self.offset,
                backend="native-ext2",
                label=self.label,
                total_size=self._native.filesystem_size,
                writable=True,
                write_strategy="native-ext2",
            )
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
        if self._native is not None:
            base = str(path or "/").rstrip("/") or "/"
            return [
                FSEntry(
                    name=item["name"],
                    path=(base.rstrip("/") + "/" + item["name"]),
                    size=item["size"],
                    is_dir=item["is_dir"],
                    inode=item.get("inode", 0),
                )
                for item in self._native.list_dir(path)
            ]
        out = self._debugfs(f"ls -l {path}")
        entries: list[FSEntry] = []
        for line in out.splitlines():
            line.strip().split("/", 6)
            # debugfs: `ls -l /` формат: inode mode uid gid size date name/
            parts = line.split()
            if len(parts) < 8 or parts[0] == "debugfs:":
                continue
            try:
                inode = int(parts[0])
                mode = int(parts[1], 8)
            except ValueError:
                continue
            # ``ls -l`` emits date and time before the name; names may contain
            # spaces, so retain all tokens after the timestamp instead of
            # treating the time as the entry name.
            if len(parts) < 9:
                continue
            name = " ".join(parts[8:])
            is_dir = (mode & 0o170000) == 0o040000
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
        if self._native is not None:
            return self._native.read_file(path)
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = Path(tf.name)
        try:
            self._debugfs(f"dump {path} {tmp}")
            return tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

    def write_file(self, path: str, data: bytes) -> None:
        if self._native is not None:
            return self._native.write_file(path, data)
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp = Path(tf.name)
        try:
            # удалить если существует
            self._debugfs(f"rm {path}", write=True)
            self._debugfs(f"write {tmp} {path}", write=True)
            self._flush_tmp()
        finally:
            tmp.unlink(missing_ok=True)

    def mkdir(self, path: str) -> None:
        if self._native is not None:
            return self._native.mkdir(path)
        self._debugfs(f"mkdir {path}", write=True)
        self._flush_tmp()

    def delete(self, path: str, recursive: bool = False) -> None:
        if self._native is not None:
            return self._native.delete(path, recursive=recursive)
        if recursive:
            raise NotImplementedError(
                "recursive ext deletion is not supported by debugfs"
            )
        self._debugfs(f"rm {path}", write=True)
        self._flush_tmp()

    def rename(self, src: str, dst: str) -> None:
        if self._native is not None:
            return self._native.rename(src, dst)
        # debugfs has no ``rename`` command.  Use a bounded dump/write/unlink
        # sequence for regular files, preserving the source until the new
        # directory entry has been created successfully.  Directory rename is
        # intentionally rejected rather than pretending that a file-only
        # emulation preserves an entire subtree.
        entries = self.list(str(PurePosixPath(src).parent))
        source = next((item for item in entries if item.path == src), None)
        if source is None:
            raise FileNotFoundError(src)
        if source.is_dir:
            raise NotImplementedError(
                "ext directory rename is not supported by debugfs"
            )
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = Path(tf.name)
        try:
            self._debugfs(f"dump {src} {tmp}")
            self._debugfs(f"rm {dst}", write=True)
            self._debugfs(f"write {tmp} {dst}", write=True)
            self._debugfs(f"rm {src}", write=True)
            self._flush_tmp()
        finally:
            tmp.unlink(missing_ok=True)

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.unlink(missing_ok=True)
            self._tmp = None
        self._tmp_source_hash = None


# =============================================================================
# 6. NTFS via subprocess (ntfs-3g / ntfsclone)
# =============================================================================
class NtfsRW(RWBackend):
    """NTFS reader via pytsk3; writes via ntfs-3g or Windows ImDisk."""

    def __init__(self, image: Path, offset: int = 0):
        super().__init__(image, offset)
        if pytsk3 is None:
            raise RuntimeError("pytsk3 required for NTFS")
        self._img = None
        self._fs = None
        self._open_reader()
        self._imdisk_drive: str | None = None

    def _open_reader(self) -> None:
        self._img = pytsk3.Img_Info(str(self.image))
        self._fs = pytsk3.FS_Info(self._img, offset=self.offset)

    def _close_reader(self) -> None:
        fs, self._fs = self._fs, None
        img, self._img = self._img, None
        for handle in (fs, img):
            close = getattr(handle, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _mount_point(self) -> Path:
        if sys.platform.startswith("win"):
            strategy, issue = _imdisk_status("ntfs")
            if strategy != "imdisk-rw":
                raise RuntimeError(issue)
            self._imdisk_drive, mount_point = _imdisk_mount(self.image, self.offset)
            return mount_point
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
        if sys.platform.startswith("win"):
            if self._imdisk_drive:
                drive, self._imdisk_drive = self._imdisk_drive, None
                _imdisk_unmount(drive)
            return
        subprocess.run(["umount", str(mp)], capture_output=True)
        shutil.rmtree(mp, ignore_errors=True)

    def info(self) -> FSInfo:
        if sys.platform.startswith("win"):
            strategy, _ = _imdisk_status("ntfs")
        elif sys.platform.startswith("linux"):
            strategy = (
                "ntfs-3g"
                if shutil.which("ntfs-3g") and shutil.which("umount")
                else "read-only-ntfs-tools"
            )
        else:
            strategy = "read-only-platform"
        return FSInfo(
            fs_type="ntfs",
            offset=self.offset,
            backend="ntfs-rw",
            total_size=self.image.stat().st_size,
            writable=strategy in ("ntfs-3g", "imdisk-rw"),
            write_strategy=strategy,
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
        release_reader = sys.platform.startswith("win")
        if release_reader:
            self._close_reader()
        try:
            mp = self._mount_point()
            try:
                fn(mp)
            finally:
                self._unmount(mp)
        finally:
            if release_reader:
                self._open_reader()

    def write_file(self, path: str, data: bytes) -> None:
        def _do(mp: Path):
            dst = _safe_mounted_path(mp, path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

        self._do_mounted(_do)

    def mkdir(self, path: str) -> None:
        self._do_mounted(
            lambda mp: _safe_mounted_path(mp, path).mkdir(parents=True, exist_ok=True)
        )

    def delete(self, path: str, recursive: bool = False) -> None:
        def _do(mp: Path):
            p = _safe_mounted_path(mp, path)
            if p.is_dir():
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do_mounted(_do)

    def rename(self, src: str, dst: str) -> None:
        self._do_mounted(
            lambda mp: _safe_mounted_path(mp, src).rename(_safe_mounted_path(mp, dst))
        )


# =============================================================================
# 7. LOOP-MOUNT fallback (любая ФС)
# =============================================================================
class LoopMountRW(RWBackend):
    """Write via Linux loop-mount or Windows ImDisk for native Windows filesystems."""

    def __init__(self, image: Path, offset: int, fs_type: str):
        super().__init__(image, offset)
        self.fs_type = _normalise_mount_fs(fs_type)

    def _assert_writable(self) -> None:
        if self.fs_type in INHERENTLY_READ_ONLY_FS:
            raise RuntimeError(
                "%s is an inherently read-only filesystem; mutation is disabled"
                % self.fs_type
            )
        _, issue = self._runtime_status()
        if issue:
            raise RuntimeError(issue)

    def _runtime_status(self) -> tuple[str, str]:
        """Return the available write strategy and any platform/tool issue."""
        if sys.platform.startswith("win"):
            return _imdisk_status(self.fs_type)
        if not sys.platform.startswith("linux"):
            return "read-only-platform", "loop-mount writing is supported only on Linux"
        missing = [name for name in ("mount", "umount") if shutil.which(name) is None]
        if missing:
            return (
                "read-only-mount-tools",
                "loop-mount writing requires these system tools: %s"
                % ", ".join(missing),
            )
        return "loop-mount", ""

    def _mount_point(self) -> Path:
        strategy, issue = self._runtime_status()
        if issue:
            raise RuntimeError(issue)
        if strategy == "imdisk-rw":
            self._imdisk_drive, mount_point = _imdisk_mount(self.image, self.offset)
            return mount_point
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
        if sys.platform.startswith("win"):
            if getattr(self, "_imdisk_drive", None):
                drive, self._imdisk_drive = self._imdisk_drive, None
                _imdisk_unmount(drive)
            return
        subprocess.run(["umount", str(mp)], capture_output=True)
        shutil.rmtree(mp, ignore_errors=True)

    def _do(self, fn) -> None:
        mp = self._mount_point()
        try:
            fn(mp)
        finally:
            self._unmount(mp)

    def info(self) -> FSInfo:
        strategy, _ = self._runtime_status()
        if self.fs_type in INHERENTLY_READ_ONLY_FS:
            strategy = "read-only-format"
        return FSInfo(
            fs_type=self.fs_type,
            offset=self.offset,
            backend=strategy,
            total_size=self.image.stat().st_size,
            writable=strategy in ("loop-mount", "imdisk-rw"),
            write_strategy=strategy,
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        def _do(mp: Path) -> list[FSEntry]:
            p = _safe_mounted_path(mp, path)
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
        self._do(lambda mp: result.append(_safe_mounted_path(mp, path).read_bytes()))
        return result[0]

    def write_file(self, path: str, data: bytes) -> None:
        self._assert_writable()
        self._do(lambda mp: _safe_mounted_path(mp, path).write_bytes(data))

    def mkdir(self, path: str) -> None:
        self._assert_writable()
        self._do(lambda mp: _safe_mounted_path(mp, path).mkdir(parents=True))

    def delete(self, path: str, recursive: bool = False) -> None:
        self._assert_writable()

        def _do(mp: Path):
            p = _safe_mounted_path(mp, path)
            if p.is_dir():
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do(_do)

    def rename(self, src: str, dst: str) -> None:
        self._assert_writable()
        self._do(
            lambda mp: _safe_mounted_path(mp, src).rename(_safe_mounted_path(mp, dst))
        )


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

        volumes = detect_volumes(self.image)
        image_size = self.image.stat().st_size
        if self.offset is not None:
            off = int(self.offset)
            fs_type = probe_filesystem(self.image, off) or "unknown"
            candidate = next(
                (volume for volume in volumes if volume.offset == off), None
            )
        else:
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
        next_offsets = [int(volume.offset) for volume in volumes if volume.offset > off]
        boundary = min(next_offsets) if next_offsets else image_size
        if candidate is not None and candidate.size:
            boundary = min(boundary, off + int(candidate.size))
        volume_size = max(0, min(image_size, boundary) - off)
        self.volume_size = volume_size
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
                    if cls is Ext234RW:
                        self.backend = cls(self.image, off, volume_size=volume_size)
                    else:
                        self.backend = cls(self.image, off)
                    return
                except Exception as e:
                    log.warning("%s failed: %s", cls.__name__, e)
                    if cls is Ext234RW:
                        from .ext2_native import NativeExt2UnsafeError

                        if isinstance(e, NativeExt2UnsafeError):
                            raise RuntimeError(
                                "refusing unsafe ext2 geometry/state: %s" % e
                            ) from e
                        raise RuntimeError(
                            "refusing unsupported ext profile without an explicit "
                            "legacy compatibility backend: %s" % e
                        ) from e
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
        info = self.info()
        fs_type = info.fs_type
        offset = self.offset if self.offset is not None else info.offset
        if getattr(info, "write_strategy", "") == "native-ext2":
            result["fsck"] = {
                "skipped": "native ext2 metadata was validated after each staged mutation; no external repair tool was run"
            }
        elif fs_type.startswith(("ext",)) and shutil.which("e2fsck"):
            command = ["e2fsck"]
            if offset:
                # e2fsck's documented offset extended option confines repair
                # to the filesystem embedded at this byte offset.
                command.extend(["-E", "offset=%d" % offset])
            command.extend(["-f", "-y", str(self.image)])
            r = subprocess.run(
                command,
                capture_output=True,
                text=True,
            )
            result["fsck"] = {"exit": r.returncode, "out": r.stdout[-1000:]}
        elif fs_type.startswith("fat") and shutil.which("fsck.fat"):
            if offset:
                result["fsck"] = {
                    "skipped": "fsck.fat does not support filesystem offsets"
                }
            else:
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
