#!/usr/bin/env python3
"""
mountfile/universal_fs_rw.py — Universal Read/Write Layer (single file)
=======================================================================

Расширяет UniversalFS записью через стратегии с явными ограничениями:

  1. NativeFat32Writer  — bounded native FAT32 writer (FAT, LFN, каталоги)
  2. NativeExt2Writer   — ограниченный clean ext2 profile, in-process Python
  3. ExtDebugfsWriter   — явный legacy unjournaled ext2 compatibility backend через `debugfs -w`
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

import contextlib
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
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from mountfile.capabilities import (
    NATIVE_WRITE_PROFILES_BY_STRATEGY,
    RW_READ_CAPABILITIES,
    RW_WRITE_CAPABILITIES,
    canonical_operation_for_api_operation,
    get_native_capability_status,
)
from mountfile.extract import ExtractionLimits, extract_file, extract_path
from mountfile.rw.journal import Journal as _VerifiedJournal
from mountfile.safety import (
    RefusalCode,
    RefusalError,
    RefusalRuntimeError,
    checked_range,
)

try:
    import pytsk3
except ImportError:
    pytsk3 = None

log = logging.getLogger("mountfile.universal_fs_rw")
RW_READ_RANGE_LIMIT = 1 << 20


@contextlib.contextmanager
def _exclusive_image_lock(image: Path):
    """Serialize cooperating writers with a stable sidecar lock file."""
    lock_path = Path(str(image.resolve()) + ".mountfile.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# These formats can be inspected/carved, but they do not have a writable
# filesystem semantics that a loop mount can safely provide.  Refusing a
# mutation is preferable to mounting them with a misleading ``rw`` option and
# reporting success after the kernel silently kept the image read-only.
INHERENTLY_READ_ONLY_FS = frozenset(
    {
        "iso9660",
        "iso",
        "squashfs",
        "jffs2",
        "ubifs",
        "hfs",
        "hfsplus",
        "apfs",
    }
)


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


def _splice_range(current: bytes, offset: int, data: bytes) -> bytes:
    """Return ``current`` with ``data`` written at ``offset`` (POSIX write)."""
    payload = bytes(data)
    if not payload:
        return current
    if offset > len(current):
        current = current + b"\x00" * (offset - len(current))
    end = offset + len(payload)
    if end <= len(current):
        return current[:offset] + payload + current[end:]
    return current[:offset] + payload


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
    read_capabilities: tuple[str, ...] = RW_READ_CAPABILITIES
    write_capabilities: tuple[str, ...] = ()
    write_rejection_reason: str = ""
    write_rejection_code: str | None = None

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
                    except Exception as exc:
                        logging.getLogger(__name__).debug(
                            "best-effort operation failed", exc_info=exc
                        )
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
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            raise ValueError("backup keep count must be a positive integer")
        self.keep = keep
        identity = hashlib.sha256(os.fsencode(self.image.resolve())).hexdigest()[:16]
        self._prefix = f"{self.image.name}.{identity}."

    def create(self) -> Path:
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        descriptor, name = tempfile.mkstemp(
            prefix=self._prefix + ts + ".", suffix=".bak", dir=self.dir
        )
        os.close(descriptor)
        dst = Path(name)
        log.info("backup → %s", dst)
        try:
            shutil.copy2(self.image, dst)
            _fsync_file(dst)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
        self._rotate()
        return dst

    def _backups(self) -> list[Path]:
        return sorted(
            path
            for path in self.dir.iterdir()
            if path.name.startswith(self._prefix)
            and path.name.endswith(".bak")
            and path.is_file()
            and not path.is_symlink()
        )

    def _rotate(self) -> None:
        backups = self._backups()
        for old in backups[: -self.keep]:
            try:
                old.unlink()
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "best-effort operation failed", exc_info=exc
                )

    def latest(self) -> Path | None:
        backups = self._backups()
        return backups[-1] if backups else None


# =============================================================================
# 3. БАЗОВЫЙ КЛАСС ЧТЕНИЯ/ЗАПИСИ
# =============================================================================
class _RWBackendExtractionProvider:
    """Adapt RWBackend reads to the hardened destination writer.

    Do not require ``stat`` here: some backends resolve names case-insensitively
    while the generic RWBackend.stat performs an exact-case directory lookup.
    Use a backend reader's bounded range API where available, and retain the
    whole-file compatibility path for backends exposing only ``read``.
    """

    def __init__(self, backend: RWBackend):
        self._backend = backend
        self.image_path = backend.image
        reader = getattr(backend, "_reader", None)
        file_size = getattr(reader, "file_size", None)
        read_range = getattr(reader, "read_file_range", None)
        if callable(file_size) and callable(read_range):
            self.file_size = file_size
            self.read_file_range = read_range
            self.max_read_range_bytes = getattr(reader, "max_read_range_bytes", 1 << 20)

    def list_dir(self, path: str = "/") -> list[dict]:
        """Expose the RW backend tree in the hardened extractor's shape."""
        return [entry.to_dict() for entry in self._backend.list(path)]

    def read_file(self, path: str) -> bytes:
        return self._backend.read(path)


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
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "best-effort operation failed", exc_info=exc
                )
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

    # ---- bounded range I/O (generic fallback) ------------------------------
    def read_range(
        self, path: str, offset: int = 0, length: int | None = None
    ) -> bytes:
        """Read ``length`` bytes starting at ``offset`` inside a regular file.

        This default implementation is correct for every backend; providers
        with a native cluster/extent reader override it to avoid buffering the
        whole file.
        """
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("read offset must be a non-negative integer")
        if length is not None and (
            not isinstance(length, int) or isinstance(length, bool) or length < 0
        ):
            raise ValueError("read length must be a non-negative integer or None")
        data = self.read(path)
        if length is None:
            return data[offset:]
        return data[offset : offset + length]

    def write_range(self, path: str, offset: int, data: bytes) -> None:
        """Write ``data`` at ``offset`` inside an existing regular file.

        Bytes are not automatically appended: writing past the current end
        grows the file and zero-fills the gap, exactly like a POSIX write.
        """
        self._check_write_range_arguments(offset, data)
        current = self.read(path)
        self.write_file(path, _splice_range(current, offset, data))

    def append(self, path: str, data: bytes) -> None:
        """Append ``data`` to the end of an existing regular file."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("append payload must be bytes-like")
        payload = bytes(data)
        self.write_range(path, len(self.read(path)), payload)

    def truncate(self, path: str, size: int) -> None:
        """Resize a regular file to ``size`` bytes (zero-filling when growing)."""
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("truncate size must be a non-negative integer")
        current = self.read(path)
        if size == len(current):
            return
        if size < len(current):
            self.write_file(path, current[:size])
        else:
            self.write_file(path, current + b"\x00" * (size - len(current)))

    @staticmethod
    def _check_write_range_arguments(offset: int, data: bytes) -> None:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("write offset must be a non-negative integer")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("write payload must be bytes-like")

    # ---- helpers ----------------------------------------------------------
    def exists(self, path: str) -> bool:
        # ``stat`` returns None for a missing path instead of raising, so the
        # old truthiness shortcut reported *every* path as present.
        try:
            return self.stat(path) is not None
        except (FileNotFoundError, NotADirectoryError):
            return False

    def stat(self, path: str) -> FSEntry | None:
        if str(path).replace("\\", "/").strip("/") == "":
            return FSEntry("/", "/", 0, True)
        parent, name = _image_path_parent_name(path)
        for e in self.list(parent):
            if e.name == name:
                return e
        return None

    def extract(
        self,
        path: str,
        dest: Path,
        *,
        limits: ExtractionLimits | None = None,
    ) -> Path:
        dest = Path(dest)
        provider = _RWBackendExtractionProvider(self)
        normalized = "/" + str(path or "/").strip().strip("/")
        source_path = normalized
        if normalized == "/":
            is_dir = True
        else:
            parent, name = _image_path_parent_name(normalized)
            entry = next(
                (
                    candidate
                    for candidate in self.list(parent)
                    if candidate.name.casefold() == name.casefold()
                ),
                None,
            )
            is_dir = entry is not None and entry.is_dir
            if entry is not None and entry.is_dir:
                source_path = entry.path
        if is_dir:
            extract_path(provider, source_path, dest, limits=limits)
        else:
            extract_file(provider, source_path, dest, limits=limits)
        return dest

    def walk(self, path: str = "/") -> Iterator[FSEntry]:
        entries = self.list(path)
        for e in entries:
            yield e
            if e.is_dir:
                yield from self.walk(e.path)


# =============================================================================
# 4. NATIVE EXFAT (phase-one read + limited write)
# =============================================================================
class NativeExfatRW(RWBackend):
    """Bounded exFAT writer for profile-validated file and directory operations."""

    def __init__(self, image: Path, offset: int = 0, volume_size: int | None = None):
        super().__init__(image, offset)
        from mountfile.exfat_reader import NativeExfatImage
        from mountfile.exfat_writer import NativeExfatWriter

        self._reader = NativeExfatImage(image, offset=offset, volume_size=volume_size)
        try:
            self._writer = NativeExfatWriter(
                image, offset=offset, volume_size=volume_size
            )
        except BaseException:
            self._reader.close()
            raise
        self.volume_size = int(volume_size)

    def close(self) -> None:
        self._reader.close()

    def info(self) -> FSInfo:
        return FSInfo(
            fs_type="exfat",
            offset=self.offset,
            backend="native-exfat-rw",
            label=self._reader.volume_label,
            total_size=self._reader.profile.volume_length
            * self._reader.profile.bytes_per_sector,
            writable=True,
            write_strategy="native-exfat-phase1",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        return [
            FSEntry(
                name=item["name"],
                path=(path.rstrip("/") + "/" + item["name"]).replace("//", "/"),
                size=item["size"],
                is_dir=item["is_dir"],
            )
            for item in self._reader.list_dir(path)
        ]

    def read(self, path: str) -> bytes:
        return self._reader.read_file(path)

    def read_range(
        self, path: str, offset: int = 0, length: int | None = None
    ) -> bytes:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("read offset must be a non-negative integer")
        if length is None:
            size = self._reader.file_size(path)
            length = max(0, size - offset)
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError("read length must be a non-negative integer or None")
        if length == 0:
            return b""
        chunks: list[bytes] = []
        remaining = length
        cursor = offset
        while remaining:
            chunk = self._reader.read_file_range(
                path,
                offset=cursor,
                length=min(remaining, self.max_read_range_bytes),
            )
            if not chunk:
                break
            chunks.append(chunk)
            cursor += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    max_read_range_bytes = 16 * 1024 * 1024

    def _mutate(self, operation: str, *args, **kwargs) -> None:
        """Release the source handle for Windows replace, then refresh the reader."""
        self._reader.close()
        try:
            getattr(self._writer, operation)(*args, **kwargs)
        except BaseException as mutation_error:
            try:
                self._reader.reopen()
            except Exception as reopen_error:
                raise mutation_error from reopen_error
            raise
        else:
            self._reader.reopen()

    def write_file(self, path: str, data: bytes) -> None:
        self._mutate("write_file", path, data)

    def mkdir(self, path: str) -> None:
        self._mutate("mkdir", path)

    def delete(self, path: str, recursive: bool = False) -> None:
        self._mutate("delete", path, recursive=recursive)

    def rename(self, src: str, dst: str) -> None:
        self._mutate("rename", src, dst)


# =============================================================================
# 5. NATIVE FAT32 (read + write)
# =============================================================================
class Fat32RW(RWBackend):
    """Bounded native FAT32 read/write without external utilities.

    The historical class remains FAT32-only. ``Fat1216RW`` below enables the
    same transactional engine after validating classic fixed-root geometry.
    """

    ALLOW_FAT1216 = False

    ATTR_DIR = 0x10
    ATTR_LFN = 0x0F
    ATTR_VOL = 0x08

    def __init__(self, image: Path, offset: int = 0, volume_size: int | None = None):
        super().__init__(image, offset)
        if offset < 0:
            raise ValueError("negative FAT volume offset")
        image_size = self.image.stat().st_size
        inferred_whole_image = volume_size is None
        self._inferred_whole_image = inferred_whole_image
        if volume_size is None:
            if offset != 0:
                raise ValueError(
                    "an explicit FAT volume_size is required at nonzero offsets"
                )
            volume_size = image_size
        if (
            not isinstance(volume_size, int)
            or isinstance(volume_size, bool)
            or volume_size <= 0
            or offset + volume_size < offset
            or offset + volume_size > image_size
        ):
            raise ValueError("FAT extent exceeds image bounds")
        self.volume_size = volume_size
        self._selected_volume_size = volume_size
        self._in_staged_mutation = False
        self._quarantine_marker = Path(str(self.image) + ".mountfile-quarantined")
        self._parse_bpb()

    def _read_at(self, offset: int, length: int) -> bytes:
        try:
            checked_range(offset, length, upper=self.volume_size)
        except RefusalError as exc:
            raise RefusalError(
                "FAT read exceeds selected volume bounds", exc.code
            ) from exc
        return super()._read_at(offset, length)

    def _write_at(self, offset: int, data: bytes) -> None:
        try:
            checked_range(offset, len(data), upper=self.volume_size)
        except RefusalError as exc:
            raise RefusalError(
                "FAT write exceeds selected volume bounds", exc.code
            ) from exc
        with self.image.open("r+b") as stream:
            stream.seek(self.offset + offset)
            written = stream.write(data)
            if written != len(data):
                raise OSError(
                    "short write at FAT offset 0x%X: %d of %d bytes"
                    % (offset, written or 0, len(data))
                )

    def _temporary_sibling(self, suffix: str) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.image.name}.",
            suffix=suffix,
            dir=str(self.image.parent),
        )
        os.close(descriptor)
        return Path(name)

    def _verify_staged_mutation(
        self, image: Path, operation: str, arguments: tuple
    ) -> None:
        verifier = type(self)(
            image,
            offset=self.offset,
            volume_size=self._selected_volume_size,
        )
        try:
            if operation == "write_file":
                path, expected = arguments
                actual = verifier.read(path)
                if actual != expected:
                    raise OSError("staged FAT read-back did not match written data")
            elif operation == "write_range":
                path, offset, payload, expected_size = arguments
                if verifier.stat(path) is None:
                    raise OSError("staged FAT read-back lost the written file")
                if verifier.stat(path).size != expected_size:
                    raise OSError("staged FAT read-back size did not match the write")
                if verifier.read_range(path, offset, len(payload)) != payload:
                    raise OSError("staged FAT read-back did not match the range write")
            elif operation == "mkdir":
                (path,) = arguments
                entry = verifier.stat(path)
                if entry is None or not entry.is_dir:
                    raise OSError("staged FAT read-back did not find created directory")
            elif operation == "delete":
                path, _recursive = arguments
                if verifier.stat(path) is not None:
                    raise OSError("staged FAT read-back found deleted entry")
            elif operation == "rename":
                source, destination = arguments
                source_parent_cluster, _ = verifier._walk_to(source)
                destination_parent_cluster, _ = verifier._walk_to(destination)
                if source.casefold() == destination.casefold():
                    if (
                        verifier.stat(source) is None
                        and verifier.stat(destination) is None
                    ):
                        raise OSError("staged FAT read-back lost renamed entry")
                else:
                    if verifier.stat(source) is not None:
                        raise OSError("staged FAT read-back retained renamed source")
                    destination_entry = verifier.stat(destination)
                    if destination_entry is None:
                        raise OSError("staged FAT read-back lost renamed entry")
                    if (
                        destination_entry.is_dir
                        and source_parent_cluster != destination_parent_cluster
                        and verifier._read_directory_parent_cluster(
                            destination_entry.inode
                        )
                        != destination_parent_cluster
                    ):
                        raise OSError(
                            "staged FAT read-back found an invalid moved-directory '..' entry"
                        )
            else:
                raise RuntimeError(f"unknown staged FAT operation: {operation}")
        finally:
            verifier.close()

    def _mutate_staged(self, operation: str, *arguments):
        inplace = getattr(self, f"_{operation}_inplace")
        if self._in_staged_mutation:
            return inplace(*arguments)
        if self._quarantine_marker.exists():
            raise RuntimeError(
                "FAT image is quarantined after a failed rollback; inspect "
                f"{self._quarantine_marker} before writing"
            )
        if self.image.is_symlink():
            raise ValueError("refusing staged FAT writes through a symlink")

        original = self.image
        parent = original.parent.resolve()
        with _exclusive_image_lock(original):
            metadata = original.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("staged FAT writes require a regular image file")
            if metadata.st_nlink != 1:
                raise ValueError("staged FAT writes refuse multiply-linked images")
            source_identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            original_hash = _sha256_file(original)
            image_size = metadata.st_size
            if shutil.disk_usage(parent).free < image_size * 2:
                raise OSError(
                    "insufficient free space for staged FAT image and rollback copy"
                )

            staged = self._temporary_sibling(".mountfile-stage")
            rollback: Path | None = None
            keep_rollback = False
            try:
                rollback = self._temporary_sibling(".mountfile-rollback")
                shutil.copy2(original, staged)
                shutil.copy2(original, rollback)
                staged_stat = original.stat()
                if (
                    (
                        staged_stat.st_dev,
                        staged_stat.st_ino,
                        staged_stat.st_size,
                        staged_stat.st_mtime_ns,
                        staged_stat.st_ctime_ns,
                    )
                    != source_identity
                    or _sha256_file(staged) != original_hash
                    or _sha256_file(rollback) != original_hash
                ):
                    raise RuntimeError(
                        "source image changed while creating FAT staging copies"
                    )
                self.image = staged
                self._in_staged_mutation = True
                try:
                    result = inplace(*arguments)
                finally:
                    self.image = original
                    self._in_staged_mutation = False

                _fsync_file(staged)
                self._verify_staged_mutation(staged, operation, arguments)
                current_stat = original.stat()
                current_identity = (
                    current_stat.st_dev,
                    current_stat.st_ino,
                    current_stat.st_size,
                    current_stat.st_mtime_ns,
                    current_stat.st_ctime_ns,
                )
                if (
                    current_identity != source_identity
                    or _sha256_file(original) != original_hash
                ):
                    raise RuntimeError(
                        "source image changed during staged FAT mutation; "
                        "staged changes were discarded"
                    )

                committed = False
                try:
                    os.replace(staged, original)
                    committed = True
                    _fsync_directory(parent)
                    self._verify_staged_mutation(original, operation, arguments)
                except BaseException as commit_error:
                    if not committed:
                        raise
                    try:
                        assert rollback is not None
                        os.replace(rollback, original)
                        _fsync_directory(parent)
                        if _sha256_file(original) != original_hash:
                            raise OSError("restored image hash does not match snapshot")
                    except BaseException as rollback_error:
                        keep_rollback = True
                        self._quarantine_marker.write_text(
                            f"rollback={rollback}\n"
                            f"commit_error={commit_error!r}\n"
                            f"rollback_error={rollback_error!r}\n",
                            encoding="utf-8",
                        )
                        raise RuntimeError(
                            "FAT commit and rollback both failed; image is "
                            f"quarantined and rollback copy retained at {rollback}"
                        ) from rollback_error
                    raise
                return result
            finally:
                self.image = original
                self._in_staged_mutation = False
                for temporary in (staged, rollback):
                    if (
                        temporary is not None
                        and temporary.exists()
                        and not (temporary == rollback and keep_rollback)
                    ):
                        temporary.unlink()

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
        if (
            self.bytes_per_sector not in (512, 1024, 2048, 4096)
            or self.sectors_per_cluster == 0
            or self.sectors_per_cluster & (self.sectors_per_cluster - 1)
            or self.reserved_sectors == 0
            or self.num_fats not in (1, 2)
            or self.fat_size == 0
            or self.total_sectors == 0
        ):
            raise ValueError("invalid FAT geometry")

        root_secs = (
            self.root_entries * 32 + self.bytes_per_sector - 1
        ) // self.bytes_per_sector
        data_secs = (
            self.total_sectors
            - self.reserved_sectors
            - self.num_fats * self.fat_size
            - root_secs
        )
        if data_secs <= 0:
            raise ValueError("invalid FAT layout: no data sectors remain")
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
        declared_bytes = self.total_sectors * self.bytes_per_sector
        if declared_bytes > self.volume_size:
            raise ValueError("FAT BPB exceeds the selected volume extent")
        if self._inferred_whole_image and declared_bytes != self.volume_size:
            raise ValueError(
                "FAT BPB must cover the whole image when volume_size is omitted"
            )
        self.volume_size = declared_bytes

        if self.fat_type == 32:
            ext_flags = struct.unpack("<H", bs[40:42])[0]
            if ext_flags & 0x0080:
                raise ValueError("non-mirrored FAT32 mode is unsupported")
            if self.num_fats > 1:
                primary = self._read_at(
                    self.fat_offset, self.fat_size * self.bytes_per_sector
                )
                for index in range(1, self.num_fats):
                    replica = self._read_at(
                        self.fat_offset + index * self.fat_size * self.bytes_per_sector,
                        self.fat_size * self.bytes_per_sector,
                    )
                    if replica != primary:
                        raise ValueError("mirrored FAT32 copies are inconsistent")
            backup_sector = struct.unpack("<H", bs[50:52])[0]
            if backup_sector:
                backup_offset = backup_sector * self.bytes_per_sector
                if backup_offset + self.bytes_per_sector > declared_bytes:
                    raise ValueError("FAT32 backup boot sector exceeds volume bounds")
                if (
                    self._read_at(backup_offset, self.bytes_per_sector)
                    != bs[: self.bytes_per_sector]
                ):
                    raise ValueError("FAT32 backup boot sector is inconsistent")
            self.root_cluster = struct.unpack("<I", bs[44:48])[0] & 0x0FFFFFFF
            if not 2 <= self.root_cluster < self.cluster_count + 2:
                raise ValueError("invalid FAT32 root cluster")
            if self.cluster_count + 2 > self.fat_size * self.bytes_per_sector // 4:
                raise ValueError("FAT32 FAT is too small for the declared volume")
            # FSINFO only caches the allocator state; the FAT stays the source
            # of truth.  The pointer is kept so the writer paths below can
            # refresh that cache - a stale free-cluster count is exactly what
            # `fsck.fat` reports as "Free cluster summary wrong".
            self.fsinfo_sector = struct.unpack("<H", bs[48:50])[0]
            if self.fsinfo_sector not in (0, 0xFFFF) and not (
                1 <= self.fsinfo_sector < self.reserved_sectors
            ):
                raise ValueError("FAT32 FSInfo sector is outside the reserved region")
            if (
                self.fsinfo_sector not in (0, 0xFFFF)
                and backup_sector
                and self.fsinfo_sector == backup_sector
            ):
                raise ValueError("FAT32 FSInfo sector aliases the backup boot sector")
        elif self.ALLOW_FAT1216:
            if not self.root_entries:
                raise ValueError("FAT12/16 requires a fixed root directory")
            self.root_cluster = 0
            bits_per_entry = 12 if self.fat_type == 12 else 16
            capacity = self.fat_size * self.bytes_per_sector * 8 // bits_per_entry
            if self.cluster_count + 2 > capacity:
                raise ValueError("FAT12/16 FAT is too small for the declared volume")
        else:
            raise ValueError(
                "native FAT12/16 writes are unsupported; use the read-only FAT reader"
            )

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
        if not 0 <= cluster < self.cluster_count + 2:
            raise ValueError("FAT entry is outside the declared data area")
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
        if start in (0, 1):
            return []
        eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.fat_type]
        bad = {12: 0xFF7, 16: 0xFFF7, 32: 0x0FFFFFF7}[self.fat_type]
        if not 2 <= start < self.cluster_count + 2:
            raise ValueError("FAT chain starts outside the data area")
        chain, seen, cur = [], set(), start
        while (
            2 <= cur < self.cluster_count + 2 and cur not in seen and len(chain) < max_n
        ):
            chain.append(cur)
            seen.add(cur)
            nxt = self._fat_get(cur)
            if nxt >= eoc:
                return chain
            if nxt in (0, 1, bad) or not 2 <= nxt < self.cluster_count + 2:
                raise ValueError("FAT chain contains an invalid link")
            cur = nxt
        if cur in seen:
            raise ValueError("FAT cluster chain contains a cycle")
        if len(chain) >= max_n:
            raise ValueError("FAT cluster chain exceeds safety limit")
        return chain

    def _cluster_offset(self, cluster: int) -> int:
        return self.data_start + (cluster - 2) * self.cluster_size

    def _read_cluster(self, cluster: int) -> bytes:
        return self._read_at(self._cluster_offset(cluster), self.cluster_size)

    def _write_cluster(self, cluster: int, data: bytes) -> None:
        assert len(data) == self.cluster_size
        self._write_at(self._cluster_offset(cluster), data)

    def _read_chain(self, start: int, size: int | None = None) -> bytes:
        if size == 0 and start in (0, 1):
            return b""
        chain = self._cluster_chain(start)
        if not chain:
            if size:
                raise ValueError("non-empty FAT file has no cluster chain")
            return b""
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
        eoc = {12: 0xFFF, 16: 0xFFFF, 32: 0x0FFFFFFF}[self.fat_type]
        for i, c in enumerate(clusters):
            self._fat_set(c, eoc if i == len(clusters) - 1 else clusters[i + 1])
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
            found = next(
                (
                    e
                    for e in entries
                    if e.name.casefold() == part.casefold() and e.is_dir
                ),
                None,
            )
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
        found = next(
            (e for e in entries if e.name.casefold() == name.casefold() and e.is_dir),
            None,
        )
        if not found:
            return []
        return self._parse_dir_raw(self._read_dir_raw(found.inode), found.path)

    def read(self, path: str) -> bytes:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        entry = next((e for e in entries if e.name.casefold() == name.casefold()), None)
        if not entry:
            raise FileNotFoundError(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return self._read_chain(entry.inode, entry.size)

    def _entry_for(self, path: str):
        """Resolve one FAT path to ``(parent_cluster, entry, raw_dir, base)``."""
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        entry = next((e for e in entries if e.name.casefold() == name.casefold()), None)
        if entry is None:
            raise FileNotFoundError(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return parent_cluster, entry, raw, parent_path

    def read_range(
        self, path: str, offset: int = 0, length: int | None = None
    ) -> bytes:
        """Read one bounded range straight from the cluster chain."""
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("read offset must be a non-negative integer")
        if length is not None and (
            not isinstance(length, int) or isinstance(length, bool) or length < 0
        ):
            raise ValueError("read length must be a non-negative integer or None")
        _parent, entry, _raw, _base = self._entry_for(path)
        size = entry.size
        if offset >= size or length == 0:
            return b""
        end = size if length is None else min(size, offset + length)
        chain = self._cluster_chain(entry.inode)
        if not chain:
            raise ValueError("non-empty FAT file has no cluster chain")
        first = offset // self.cluster_size
        last = (end - 1) // self.cluster_size
        if last >= len(chain):
            raise ValueError("FAT cluster chain is shorter than the declared size")
        buffer = b"".join(self._read_cluster(chain[i]) for i in range(first, last + 1))
        start = offset - first * self.cluster_size
        return buffer[start : start + (end - offset)]

    def write_file(self, path: str, data: bytes) -> None:
        self._mutate_staged("write_file", path, data)

    def write_range(self, path: str, offset: int, data: bytes) -> None:
        self._check_write_range_arguments(offset, data)
        payload = bytes(data)
        if not payload:
            # An empty write is still a presence check, not a silent success.
            self._entry_for(path)
            return
        expected_size = max(self._entry_for(path)[1].size, offset + len(payload))
        self._mutate_staged("write_range", path, offset, payload, expected_size)

    def _write_range_inplace(
        self, path: str, offset: int, data: bytes, expected_size: int | None = None
    ) -> None:
        """Splice ``data`` into an existing file, growing its chain if needed.

        ``expected_size`` is carried through only so the staged read-back can
        compare the resulting file size; it is not used for the mutation.
        """
        parent_cluster, entry, raw, parent_path = self._entry_for(path)
        chain = self._cluster_chain(entry.inode) if entry.size else []
        if entry.size and not chain:
            raise ValueError("non-empty FAT file has no cluster chain")
        old_size = entry.size
        existing_clusters = (old_size + self.cluster_size - 1) // self.cluster_size
        if len(chain) < existing_clusters:
            raise ValueError("FAT cluster chain is shorter than the declared size")
        new_size = max(old_size, offset + len(data))
        if new_size > 0xFFFFFFFF:
            raise ValueError("FAT file size exceeds the 32-bit directory limit")
        clusters_needed = (new_size + self.cluster_size - 1) // self.cluster_size
        if clusters_needed > len(chain):
            extra = self._allocate_chain(clusters_needed - len(chain))
            try:
                if chain:
                    # Link the tail of the old chain before the new clusters so
                    # a partially grown file is still a valid chain.
                    self._fat_set(chain[-1], extra[0])
                    chain = chain + extra
                else:
                    chain = extra
            except Exception:
                self._free_chain(extra[0])
                raise
        if offset > old_size:
            gap = old_size
            while gap < offset:
                index = gap // self.cluster_size
                if index >= len(chain):
                    raise ValueError(
                        "FAT cluster chain is shorter than the write range"
                    )
                cluster_offset = index * self.cluster_size
                gap_end = min(offset, cluster_offset + self.cluster_size)
                current = bytearray(self._read_cluster(chain[index]))
                local = gap - cluster_offset
                current[local : local + (gap_end - gap)] = b"\x00" * (gap_end - gap)
                self._write_cluster(chain[index], bytes(current))
                gap = gap_end
        first = offset // self.cluster_size
        last = (offset + len(data) - 1) // self.cluster_size
        for index in range(first, last + 1):
            cluster_offset = index * self.cluster_size
            chunk_start = max(offset, cluster_offset)
            chunk_end = min(offset + len(data), cluster_offset + self.cluster_size)
            current = bytearray(self._read_cluster(chain[index]))
            local = chunk_start - cluster_offset
            current[local : local + (chunk_end - chunk_start)] = data[
                chunk_start - offset : chunk_end - offset
            ]
            self._write_cluster(chain[index], bytes(current))
        index_in_dir = self._find_entry_index(raw, entry, parent_path)
        if index_in_dir is None:
            raise RuntimeError("directory entry not found while extending %s" % path)
        buffer = bytearray(raw)
        first_cluster = chain[0] if chain else 0
        struct.pack_into("<H", buffer, index_in_dir + 26, first_cluster & 0xFFFF)
        if self.fat_type == 32:
            struct.pack_into(
                "<H", buffer, index_in_dir + 20, (first_cluster >> 16) & 0xFFFF
            )
        struct.pack_into("<I", buffer, index_in_dir + 28, new_size)
        self._write_dir_raw(parent_cluster, bytes(buffer))
        self.sync_fsinfo()

    def _write_file_inplace(self, path: str, data: bytes) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        existing = next(
            (e for e in entries if e.name.casefold() == name.casefold()), None
        )
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
        self._mutate_staged("mkdir", path)

    def _mkdir_inplace(self, path: str) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        if any(e.name.casefold() == name.casefold() for e in entries):
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
        self._mutate_staged("delete", path, recursive)

    def _delete_inplace(self, path: str, recursive: bool = False) -> None:
        parent_cluster, name = self._walk_to(path)
        raw = self._read_dir_raw(parent_cluster)
        parent_path, _ = _image_path_parent_name(path)
        entries = self._parse_dir_raw(raw, parent_path)
        entry = next((e for e in entries if e.name.casefold() == name.casefold()), None)
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

    def _has_lfn_group(self, raw: bytes, short_entry_index: int) -> bool:
        """Return whether the entry at `short_entry_index` has long-name slots."""
        index = short_entry_index - 32
        while index >= 0:
            if raw[index] in (0x00, 0xE5) or raw[index + 11] != self.ATTR_LFN:
                return False
            return True
        return False

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

    @staticmethod
    def _validate_fat_move_name(name: str) -> None:
        if not name or name in (".", ".."):
            raise ValueError("invalid FAT destination name")
        if name[-1] in (" ", "."):
            raise ValueError("FAT names cannot end in a space or dot")
        if any(ord(char) < 0x20 or char in '"*/:<>?\\|' for char in name):
            raise ValueError("destination name contains a FAT-forbidden character")
        try:
            code_units = len(name.encode("utf-16le")) // 2
        except UnicodeEncodeError as exc:
            raise ValueError("destination name is not valid UTF-16") from exc
        if code_units > 255:
            raise ValueError("FAT long names cannot exceed 255 UTF-16 code units")

    def _validate_lfn_group(
        self, raw: bytes, short_entry_index: int, expected_name: str
    ) -> None:
        start = short_entry_index
        while start >= 32:
            previous = start - 32
            if raw[previous] in (0x00, 0xE5) or raw[previous + 11] != self.ATTR_LFN:
                break
            start = previous
        count = (short_entry_index - start) // 32
        if count == 0:
            return
        if count > 20:
            raise ValueError("source FAT long-name sequence is too large")

        short_entry = raw[short_entry_index : short_entry_index + 32]
        checksum = self._short_checksum(short_entry[:11])
        offsets = list(range(start, short_entry_index, 32))
        chunks: list[list[int]] = []
        for index, offset in enumerate(offsets):
            expected_sequence = count - index
            expected_first = expected_sequence | (0x40 if index == 0 else 0)
            if (
                raw[offset] != expected_first
                or raw[offset + 11] != self.ATTR_LFN
                or raw[offset + 12] != 0
                or raw[offset + 13] != checksum
                or struct.unpack_from("<H", raw, offset + 26)[0] != 0
            ):
                raise ValueError("source FAT long-name sequence is malformed")
            chunk = (
                raw[offset + 1 : offset + 11]
                + raw[offset + 14 : offset + 26]
                + raw[offset + 28 : offset + 32]
            )
            chunks.append(
                [
                    struct.unpack_from("<H", chunk, cursor)[0]
                    for cursor in range(0, len(chunk), 2)
                ]
            )
        units = [unit for chunk in reversed(chunks) for unit in chunk]
        if self._decode_lfn_units(units) != expected_name:
            raise ValueError("source FAT long-name sequence does not match its entry")

    def _read_directory_parent_cluster(self, directory_cluster: int) -> int:
        if not 2 <= directory_cluster < self.cluster_count + 2:
            raise ValueError("invalid moved FAT directory cluster")
        raw = self._read_dir_raw(directory_cluster)
        dot_name = b"." + b" " * 10
        dotdot_name = b".." + b" " * 9
        if len(raw) < 64:
            raise ValueError("FAT directory lacks '.' and '..' entries")
        dot = raw[:32]
        dotdot = raw[32:64]
        if (
            dot[:11] != dot_name
            or not dot[11] & self.ATTR_DIR
            or dotdot[:11] != dotdot_name
            or not dotdot[11] & self.ATTR_DIR
        ):
            raise ValueError("FAT directory has malformed '.' or '..' entries")
        dot_high = struct.unpack_from("<H", dot, 20)[0]
        dotdot_high = struct.unpack_from("<H", dotdot, 20)[0]
        if dot_high & 0xF000 or dotdot_high & 0xF000:
            raise ValueError("FAT directory cluster field has reserved bits set")
        dot_cluster = struct.unpack_from("<H", dot, 26)[0] | (dot_high << 16)
        if dot_cluster != directory_cluster:
            raise ValueError("FAT directory '.' entry points to a different cluster")
        parent_cluster = struct.unpack_from("<H", dotdot, 26)[0] | (dotdot_high << 16)
        if parent_cluster == 0:
            return self.root_cluster
        if not 2 <= parent_cluster < self.cluster_count + 2:
            raise ValueError("FAT directory '..' entry has an invalid parent cluster")
        return parent_cluster

    def _directory_parent_update(
        self,
        directory_cluster: int,
        expected_old_parent: int,
        new_parent: int,
    ) -> bytes:
        actual_parent = self._read_directory_parent_cluster(directory_cluster)
        if actual_parent != expected_old_parent:
            raise ValueError(
                "FAT directory '..' entry does not match its source parent"
            )
        raw = bytearray(self._read_dir_raw(directory_cluster))
        encoded_parent = 0 if new_parent == self.root_cluster else new_parent
        struct.pack_into("<H", raw, 32 + 20, (encoded_parent >> 16) & 0x0FFF)
        struct.pack_into("<H", raw, 32 + 26, encoded_parent & 0xFFFF)
        return bytes(raw)

    def _assert_destination_not_descendant(
        self,
        moved_directory_cluster: int,
        destination_parent: str,
        expected_parent_cluster: int,
    ) -> None:
        current_cluster = self.root_cluster
        if current_cluster == moved_directory_cluster:
            raise ValueError("cannot move the FAT root directory")
        prefix = "/"
        for component in destination_parent.replace("\\", "/").split("/"):
            if not component:
                continue
            if current_cluster == moved_directory_cluster:
                raise ValueError("cannot move a directory into itself or a descendant")
            entries = self._parse_dir_raw(self._read_dir_raw(current_cluster), prefix)
            matches = [
                entry
                for entry in entries
                if entry.is_dir and entry.name.casefold() == component.casefold()
            ]
            if len(matches) != 1:
                raise ValueError("destination parent path is ambiguous or invalid")
            current_cluster = matches[0].inode
            if current_cluster == moved_directory_cluster:
                raise ValueError("cannot move a directory into itself or a descendant")
            prefix = prefix.rstrip("/") + "/" + matches[0].name
        if current_cluster != expected_parent_cluster:
            raise RuntimeError("destination parent changed during FAT rename")

    def rename(self, src: str, dst: str) -> None:
        self._mutate_staged("rename", src, dst)

    def _rename_inplace(self, src: str, dst: str) -> None:
        """Rename within one directory or move an entry between FAT32 dirs."""
        src_parent, src_name = _image_path_parent_name(src)
        dst_parent, dst_name = _image_path_parent_name(dst)
        if not src_name or not dst_name:
            raise ValueError("FAT root cannot be renamed")
        source_parent_cluster, _ = self._walk_to(src)
        destination_parent_cluster, _ = self._walk_to(dst)
        source_raw = self._read_dir_raw(source_parent_cluster)
        source_entries = self._parse_dir_raw(source_raw, src_parent)
        source_matches = [
            item
            for item in source_entries
            if item.name.casefold() == src_name.casefold()
        ]
        if not source_matches:
            raise FileNotFoundError(src)
        if len(source_matches) != 1:
            raise ValueError("source FAT entry name is ambiguous")
        entry = source_matches[0]
        if (
            source_parent_cluster == destination_parent_cluster
            and src_name.casefold() == dst_name.casefold()
        ):
            return

        destination_raw = self._read_dir_raw(destination_parent_cluster)
        destination_entries = self._parse_dir_raw(destination_raw, dst_parent)
        if any(
            item.name.casefold() == dst_name.casefold() for item in destination_entries
        ):
            raise FileExistsError(dst)

        source_index = self._find_entry_index(source_raw, entry, src_parent)
        if source_index is None:
            raise RuntimeError(f"directory entry not found while renaming {src}")

        if source_parent_cluster == destination_parent_cluster:
            self._validate_fat_move_name(dst_name)
            self._validate_lfn_group(source_raw, source_index, entry.name)
            short_raw = self._short_name_for_dir(dst_name, source_raw)
            for other_idx in range(0, len(source_raw) - 31, 32):
                if other_idx == source_index:
                    continue
                first = source_raw[other_idx]
                if first in (0x00, 0xE5) or source_raw[other_idx + 11] == self.ATTR_LFN:
                    continue
                if source_raw[other_idx : other_idx + 11] == short_raw:
                    raise FileExistsError(dst)
            # A pure 8.3 rename of an entry that carries no long name can be
            # done in place. Anything else (LFN source or LFN destination)
            # tombstones the old entry group and inserts a fresh one below.
            if self._is_83_name(dst_name) and not self._has_lfn_group(
                source_raw, source_index
            ):
                updated = bytearray(source_raw)
                updated[source_index : source_index + 11] = short_raw
                self._write_dir_raw(source_parent_cluster, bytes(updated))
                self.sync_fsinfo()
                return
            updated = bytearray(self._tombstone_entry_slots(source_raw, source_index))
            lfn = (
                self._lfn_entries(dst_name, short_raw)
                if not self._is_83_name(dst_name)
                else b""
            )
            slot = self._find_entry_slot(bytes(updated), len(lfn) // 32 + 1)
            if slot is None:
                raise RuntimeError(f"no free dir slot for {dst_name}")
            updated[slot : slot + len(lfn)] = lfn
            replacement = bytearray(source_raw[source_index : source_index + 32])
            replacement[:11] = short_raw
            updated[slot + len(lfn) : slot + len(lfn) + 32] = replacement
            self._write_dir_raw(source_parent_cluster, bytes(updated))
            self.sync_fsinfo()
            return

        self._validate_fat_move_name(dst_name)
        self._validate_lfn_group(source_raw, source_index, entry.name)
        if entry.is_dir:
            self._assert_destination_not_descendant(
                entry.inode, dst_parent, destination_parent_cluster
            )
            updated_directory = self._directory_parent_update(
                entry.inode, source_parent_cluster, destination_parent_cluster
            )
        else:
            updated_directory = None

        short_name = self._short_name_for_dir(dst_name, destination_raw)
        lfn = (
            self._lfn_entries(dst_name, short_name)
            if not self._is_83_name(dst_name)
            else b""
        )
        new_short_entry = bytearray(source_raw[source_index : source_index + 32])
        new_short_entry[:11] = short_name
        new_entry_set = lfn + bytes(new_short_entry)
        slot = self._find_entry_slot(destination_raw, len(new_entry_set) // 32)
        if slot is None:
            raise RuntimeError(f"no free dir slot for {dst_name}")
        updated_destination = bytearray(destination_raw)
        updated_destination[slot : slot + len(new_entry_set)] = new_entry_set
        updated_source = self._tombstone_entry_slots(source_raw, source_index)

        # All edits happen against the private staged image. The public writer
        # validates the resulting namespace and atomically replaces the source
        # image only after the full move succeeds.
        self._write_dir_raw(destination_parent_cluster, bytes(updated_destination))
        if updated_directory is not None:
            self._write_dir_raw(entry.inode, updated_directory)
        self._write_dir_raw(source_parent_cluster, updated_source)
        self.sync_fsinfo()


class Fat1216RW(Fat32RW):
    """Native bounded writer for FAT12 and FAT16 volumes."""

    ALLOW_FAT1216 = True

    def info(self) -> FSInfo:
        value = super().info()
        value.backend = "native-fat-rw"
        value.write_strategy = "native-fat12-16"
        return value


NativeFat1216Writer = Fat1216RW


# =============================================================================
# 5. EXT2/3/4 (native ext2; journaled ext3/ext4 read-only; explicit ext2 compatibility)
# =============================================================================
class Ext234RW(RWBackend):
    """
    ext2 uses MountFile's bounded in-process writer when its clean feature
    profile is supported. The e2fsprogs debugfs compatibility path is disabled
    unless the library caller explicitly sets ``allow_legacy_debugfs=True``; it
    remains unavailable for journaled ext3/ext4 profiles because debugfs bypasses
    their journal ordering and recovery protocol.
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
            if self.is_ext3 or self.is_ext4:
                raise RefusalRuntimeError(
                    "legacy debugfs writes are refused for journaled ext3/ext4; "
                    "native journal ordering and recovery are not implemented",
                    RefusalCode.UNSUPPORTED_PROFILE,
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

    def read_range(
        self, path: str, offset: int = 0, length: int | None = None
    ) -> bytes:
        if self._native is not None:
            return self._native.read_file_range(path, offset, length)
        return super().read_range(path, offset, length)

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

    def link(self, source: str, destination: str) -> None:
        if self._native is None:
            raise NotImplementedError("hard links require the native ext2 profile")
        return self._native.link(source, destination)

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
# 6. Optional libguestfs writer for NTFS and ext2/3/4
# =============================================================================
class LibguestfsRW(RWBackend):
    """Explicit libguestfs writer for filesystems outside native profiles.

    The dependency is imported lazily and this backend is never selected unless
    the caller passes ``allow_libguestfs=True``.  The image is opened directly
    by libguestfs; callers should keep the normal UniversalFSRW backup enabled
    when using this compatibility backend.
    """

    def __init__(
        self,
        image: Path,
        offset: int = 0,
        volume_size: int | None = None,
        fs_type: str = "external",
    ):
        super().__init__(image, offset)
        self._image_lock = None
        try:
            self._acquire_image_lock()
            import guestfs
        except ImportError as exc:
            raise RuntimeError(
                "libguestfs Python bindings are required; install python3-libguestfs"
            ) from exc
        self.volume_size = int(volume_size) if volume_size is not None else None
        self.fs_type = str(fs_type).lower()
        self._g = guestfs.GuestFS(python_return_dict=True)
        self._device = None
        self._mounted = False
        self._shutdown = False
        try:
            self._g.add_drive_opts(str(self.image), format="raw", readonly=0)
            self._g.launch()
            devices = self._g.list_devices()
            if not devices:
                raise RuntimeError("libguestfs found no block device in the image")
            device = devices[0]
            if self.offset:
                selected = None
                for part in self._g.part_list(device):
                    start = int(part["part_start"]) * 512
                    end = (int(part["part_end"]) + 1) * 512
                    if start == self.offset and (
                        self.volume_size is None or end - start >= self.volume_size
                    ):
                        selected = f"{device}{part['part_num']}"
                        break
                if selected is None:
                    raise RuntimeError(
                        "libguestfs could not map the selected filesystem offset "
                        "to a partition"
                    )
                device = selected
            self._device = device
            try:
                self.mount_options("")
            except RuntimeError:
                if not self.fs_type.startswith("ntfs"):
                    raise
                # Some libguestfs builds need the explicit ntfs-3g helper.
                self.umount_all()
                self.mount_options("ntfs-3g")
        except BaseException:
            self.close()
            raise

    def _acquire_image_lock(self) -> None:
        """Serialize external writers before opening the image in libguestfs."""
        if os.name != "posix":
            raise RuntimeError("libguestfs image locking requires a POSIX host")
        import fcntl

        lock_path = self.image.with_name(self.image.name + ".mountfile.lock")
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError("filesystem image is already locked") from exc
        self._image_lock = handle

    def _release_image_lock(self) -> None:
        handle = self._image_lock
        if handle is None:
            return
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._image_lock = None

    def mount_options(self, options: str = "", mountpoint: str = "/") -> None:
        """Mount the selected image filesystem inside the libguestfs VM."""
        if self._mounted:
            return
        if self._device is None:
            raise RuntimeError("libguestfs device is not available")
        try:
            self._g.mount_options(str(options), self._device, mountpoint)
        except Exception as exc:
            raise RuntimeError(
                "libguestfs mount failed for %s: %s" % (self._device, exc)
            ) from exc
        self._mounted = True

    def mount(self, mountpoint: str = "/") -> None:
        """Mount with filesystem-driver defaults inside the isolated VM."""
        self.mount_options("", mountpoint)

    def unmount(self, path_or_device: str = "/") -> None:
        """Unmount one libguestfs mountpoint without touching the host system."""
        if not self._mounted:
            return
        try:
            self._g.umount(path_or_device)
        except Exception as exc:
            raise RuntimeError("libguestfs unmount failed: %s" % exc) from exc
        self._mounted = False

    def umount_all(self) -> None:
        """Unmount all filesystems currently mounted in the VM."""
        if not self._mounted:
            return
        try:
            self._g.umount_all()
        except Exception as exc:
            raise RuntimeError("libguestfs umount_all failed: %s" % exc) from exc
        self._mounted = False

    def shutdown(self) -> None:
        """Flush guestfs caches and shut down the appliance."""
        if self._shutdown:
            return
        if self._mounted:
            self.umount_all()
        self._g.sync()
        self._g.shutdown()
        self._shutdown = True

    @staticmethod
    def _path(path: str) -> str:
        value = "/" + str(path or "/").lstrip("/")
        parts = value.split("/")
        if value != "/" and any(
            part in ("", ".", "..") or "\x00" in part for part in parts[1:]
        ):
            raise ValueError("unsafe path for libguestfs: %r" % path)
        return value

    def _download(self, path: str) -> bytes:
        with tempfile.NamedTemporaryFile(prefix="mountfile-guestfs-") as tmp:
            self._g.download(self._path(path), tmp.name)
            tmp.flush()
            return Path(tmp.name).read_bytes()

    def info(self) -> FSInfo:
        return FSInfo(
            fs_type=self.fs_type,
            offset=self.offset,
            backend="libguestfs-rw",
            total_size=self.volume_size or self.image.stat().st_size,
            writable=True,
            write_strategy="libguestfs",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        parent = self._path(path)
        entries: list[FSEntry] = []
        for item in self._g.readdir(parent):
            name = item.get("name", "")
            if name in ("", ".", ".."):
                continue
            child = parent.rstrip("/") + "/" + name
            try:
                meta = self._g.statns(child)
                size = int(meta.get("size", 0))
                mode = int(meta.get("mode", 0))
                is_dir = bool(mode & stat.S_IFDIR)
            except Exception:
                size, mode, is_dir = 0, None, False
            entries.append(FSEntry(name, child, size, is_dir, mode=mode))
        return entries

    def read(self, path: str) -> bytes:
        return self._download(path)

    def write_file(self, path: str, data: bytes) -> None:
        target = self._path(path)
        parent = str(PurePosixPath(target).parent)
        self._g.mkdir_p(parent)
        with tempfile.NamedTemporaryFile(prefix="mountfile-guestfs-") as tmp:
            tmp.write(bytes(data))
            tmp.flush()
            self._g.upload(tmp.name, target)

    def mkdir(self, path: str) -> None:
        self._g.mkdir_p(self._path(path))

    def delete(self, path: str, recursive: bool = False) -> None:
        target = self._path(path)
        if recursive:
            self._g.rm_rf(target)
        elif self._g.is_dir(target):
            self._g.rmdir(target)
        else:
            self._g.rm(target)

    def rename(self, src: str, dst: str) -> None:
        self._g.mv(self._path(src), self._path(dst))

    def close(self) -> None:
        guest = getattr(self, "_g", None)
        try:
            if guest is not None:
                try:
                    self.shutdown()
                except Exception as exc:
                    log.warning("libguestfs shutdown failed: %s", exc)
        finally:
            if guest is not None:
                guest.close()
                self._g = None
            self._release_image_lock()


# =============================================================================
# 7. NTFS via subprocess (ntfs-3g / ntfsclone)
# =============================================================================
def _legacy_relative_path(path: str, *, allow_root: bool = False) -> str:
    """Validate before normalization can hide traversal or host drive syntax."""
    raw = str(path).replace("\\", "/")
    parts = raw.strip("/").split("/")
    if raw in ("", "/") and allow_root:
        return ""
    if any(part in ("", ".", "..") or ":" in part or "\x00" in part for part in parts):
        raise ValueError("unsafe path in legacy filesystem: %r" % path)
    return "/".join(parts)


def _legacy_path(root: Path, path: str, *, allow_root: bool = False) -> Path:
    relative = _legacy_relative_path(path, allow_root=allow_root)
    root = Path(root)
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or getattr(root_info, "st_file_attributes", 0) & 0x400
    ):
        raise PermissionError("legacy root must be a real directory: %s" % root)
    target = _safe_mounted_path(root, relative)
    current = root.resolve()
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.exists() and current.is_dir() and os.path.ismount(current):
            raise PermissionError("refusing nested mount point: %s" % current)
    return target


@contextlib.contextmanager
def _legacy_regular_file(
    root: Path, path: str, *, write: bool = False, exclusive: bool = False
):
    """Open beneath a pinned POSIX root; refuse links and special/hardlinked files."""
    target = _legacy_path(root, path)
    flags = (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
    if exclusive:
        flags |= os.O_EXCL
    flags |= (
        getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    directories = []
    descriptor = None
    try:
        if os.open in os.supports_dir_fd:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directories.append(os.open(root, directory_flags))
            for part in PurePosixPath(_legacy_relative_path(path)).parts[:-1]:
                directories.append(
                    os.open(part, directory_flags, dir_fd=directories[-1])
                )
            descriptor = os.open(target.name, flags, 0o600, dir_fd=directories[-1])
        else:
            descriptor = os.open(target, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError(
                "legacy file must be regular and non-linked: %s" % target
            )
        if write:
            os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb" if write else "rb") as handle:
            descriptor = None
            yield handle
            if write:
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _legacy_read_file(root: Path, path: str) -> bytes:
    with _legacy_regular_file(root, path) as handle:
        return handle.read()


def _legacy_write_file(
    root: Path, path: str, data: bytes, *, exclusive: bool = False
) -> None:
    if not isinstance(data, bytes):
        raise TypeError("legacy write data must be bytes")
    with _legacy_regular_file(root, path, write=True, exclusive=exclusive) as handle:
        if handle.write(data) != len(data):
            raise OSError("short write in legacy filesystem: %s" % path)


def _legacy_tree(root: Path, path: str = "/") -> Iterator[Path]:
    """Only ordinary files/directories; do not follow links or nested mounts."""
    target = _legacy_path(root, path, allow_root=True)
    info = target.lstat()
    if stat.S_ISREG(info.st_mode):
        yield target
    elif stat.S_ISDIR(info.st_mode):
        yield target
        for child in target.iterdir():
            relative = child.relative_to(root).as_posix()
            yield from _legacy_tree(root, relative)
    else:
        raise PermissionError("unsupported special file in legacy tree: %s" % target)


class _LegacyMountSafety:
    """Detach must succeed before non-recursive mount-directory cleanup."""

    def _check_pending_mount(self) -> None:
        if getattr(self, "_retained_mount_point", None) is not None:
            raise RuntimeError(
                "previous mount requires manual inspection at %s: %s"
                % (self._retained_mount_point, getattr(self, "_mount_failure", ""))
            )

    def _retain_mount(self, mp: Path, error: BaseException) -> None:
        self._retained_mount_point = mp
        self._mount_failure = repr(error)
        log.error("legacy mount retained at %s: %s", mp, error)

    def _unmount(self, mp: Path) -> None:
        self._retained_mount_point = mp
        try:
            if sys.platform.startswith("win"):
                drive = getattr(self, "_imdisk_drive", None)
                if not drive:
                    raise RuntimeError("no ImDisk drive recorded for %s" % mp)
                _imdisk_unmount(drive)
                self._imdisk_drive = None
            else:
                result = subprocess.run(
                    ["umount", str(mp)], capture_output=True, text=True, timeout=120
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        "umount failed for %s: %s"
                        % (mp, (result.stderr or result.stdout).strip())
                    )
                if os.path.ismount(mp):
                    raise RuntimeError("mount point is still attached: %s" % mp)
                # Never recursively remove a mount point, even after success.
                mp.rmdir()
        except BaseException as error:
            self._retain_mount(mp, error)
            raise RuntimeError(
                "mount detach/cleanup failed; retained %s: %s" % (mp, error)
            ) from error
        self._retained_mount_point = None
        self._mount_failure = ""


class NtfsRW(_LegacyMountSafety, RWBackend):
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
                except Exception as exc:
                    logging.getLogger(__name__).debug(
                        "best-effort operation failed", exc_info=exc
                    )

    def _mount_point(self) -> Path:
        self._check_pending_mount()
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
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"ntfs-3g mount failed: {r.stderr}")
        except BaseException as error:
            self._retain_mount(mp, error)
            raise
        return mp

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
            if release_reader and getattr(self, "_retained_mount_point", None) is None:
                self._open_reader()

    def write_file(self, path: str, data: bytes) -> None:
        _legacy_relative_path(path)

        def _do(mp: Path):
            dst = _legacy_path(mp, path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            _legacy_write_file(mp, path, data)

        self._do_mounted(_do)

    def mkdir(self, path: str) -> None:
        _legacy_relative_path(path)
        self._do_mounted(
            lambda mp: _legacy_path(mp, path).mkdir(parents=True, exist_ok=True)
        )

    def delete(self, path: str, recursive: bool = False) -> None:
        _legacy_relative_path(path)

        def _do(mp: Path):
            p = _legacy_path(mp, path)
            if p.is_dir():
                if recursive:
                    list(_legacy_tree(mp, path))
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do_mounted(_do)

    def rename(self, src: str, dst: str) -> None:
        _legacy_relative_path(src)
        _legacy_relative_path(dst)
        self._do_mounted(lambda mp: _legacy_path(mp, src).rename(_legacy_path(mp, dst)))


# =============================================================================
# 8. LOOP-MOUNT fallback (любая ФС)
# =============================================================================
class LoopMountRW(_LegacyMountSafety, RWBackend):
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
        self._check_pending_mount()
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
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"mount failed: {r.stderr}")
        except BaseException as error:
            self._retain_mount(mp, error)
            raise
        return mp

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
        _legacy_relative_path(path, allow_root=True)

        def _do(mp: Path) -> list[FSEntry]:
            p = _legacy_path(mp, path, allow_root=True)
            if not p.exists():
                return []
            entries: list[FSEntry] = []
            for child in p.iterdir():
                child = _legacy_path(mp, child.relative_to(mp).as_posix())
                st = child.lstat()
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                    raise PermissionError("unsupported special file: %s" % child)
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
        _legacy_relative_path(path)
        result: list[bytes] = []
        self._do(lambda mp: result.append(_legacy_read_file(mp, path)))
        return result[0]

    def write_file(self, path: str, data: bytes) -> None:
        self._assert_writable()
        _legacy_relative_path(path)
        self._do(lambda mp: _legacy_write_file(mp, path, data))

    def mkdir(self, path: str) -> None:
        self._assert_writable()
        _legacy_relative_path(path)
        self._do(lambda mp: _legacy_path(mp, path).mkdir(parents=True))

    def delete(self, path: str, recursive: bool = False) -> None:
        self._assert_writable()
        _legacy_relative_path(path)

        def _do(mp: Path):
            p = _legacy_path(mp, path)
            if p.is_dir():
                if recursive:
                    list(_legacy_tree(mp, path))
                shutil.rmtree(p) if recursive else p.rmdir()
            else:
                p.unlink()

        self._do(_do)

    def rename(self, src: str, dst: str) -> None:
        self._assert_writable()
        _legacy_relative_path(src)
        _legacy_relative_path(dst)
        self._do(lambda mp: _legacy_path(mp, src).rename(_legacy_path(mp, dst)))


# =============================================================================
# 9. REBUILD fallback (распаковать → mkfs → залить → заменить)
# =============================================================================
class RebuildRW(_LegacyMountSafety, RWBackend):
    """Explicit, whole-image legacy rebuild; never a native-profile fallback.

    This lossy strategy does not preserve filesystem metadata or free space.
    A reader without a reopen() API must be reconstructed after a commit.
    """

    MKFS = {
        "ext2": ["mkfs.ext2", "-F"],
        "ext3": ["mkfs.ext3", "-F"],
        "ext4": ["mkfs.ext4", "-F"],
        "ntfs": ["mkfs.ntfs", "-F", "-Q"],
        "vfat": ["mkfs.vfat", "-F", "32"],
        "fat32": ["mkfs.vfat", "-F", "32"],
    }
    MAX_ENTRIES = 100_000
    MAX_DEPTH = 128

    def __init__(self, reader: RWBackend, fs_type: str):
        self.reader = reader
        self.fs_type = str(fs_type).lower()
        self.image = Path(reader.image)
        self.offset = reader.offset
        self._fh = None
        self._reader_stale = False
        self._retained_workspace: Path | None = None
        self._quarantine_marker = Path(str(self.image) + ".mountfile-quarantined")

    def _require_reader(self) -> None:
        if self._reader_stale:
            raise RuntimeError(
                "rebuild reader is closed/stale; reconstruct the backend"
            )

    def _assert_profile(self) -> None:
        self._require_reader()
        self._check_pending_mount()
        if self._retained_workspace is not None or self._quarantine_marker.exists():
            raise RuntimeError(
                "rebuild evidence requires manual inspection before retry"
            )
        if self.fs_type not in self.MKFS:
            raise RuntimeError("no mkfs for %s" % self.fs_type)
        if self.offset != 0:
            raise ValueError("legacy rebuild requires a whole image at offset zero")
        info = self.reader.info()
        if _normalise_mount_fs(info.fs_type) != _normalise_mount_fs(self.fs_type):
            raise ValueError("rebuild filesystem does not match the reader profile")
        if info.total_size != self.image.stat().st_size:
            raise ValueError("legacy rebuild requires an exact whole-image extent")
        if not sys.platform.startswith("linux"):
            raise RuntimeError("legacy rebuild requires Linux; no ImDisk fallback")

    def info(self) -> FSInfo:
        self._require_reader()
        info = self.reader.info()
        supported = (
            self.fs_type in self.MKFS
            and self.offset == 0
            and info.total_size == self.image.stat().st_size
            and _normalise_mount_fs(info.fs_type) == _normalise_mount_fs(self.fs_type)
            and sys.platform.startswith("linux")
        )
        return FSInfo(
            fs_type=info.fs_type,
            offset=self.offset,
            backend="rebuild-rw",
            label=info.label,
            total_size=info.total_size,
            writable=supported,
            write_strategy="rebuild" if supported else "read-only-rebuild-profile",
        )

    def list(self, path: str = "/") -> list[FSEntry]:
        self._require_reader()
        _legacy_relative_path(path, allow_root=True)
        return self.reader.list(path)

    def read(self, path: str) -> bytes:
        self._require_reader()
        _legacy_relative_path(path)
        return self.reader.read(path)

    def close(self) -> None:
        self.reader.close()
        self._reader_stale = True

    @staticmethod
    def _identity(image: Path) -> tuple:
        metadata = image.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("legacy rebuild requires a regular, non-linked image")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _unpack(self, stage: Path, image_size: int) -> None:
        pending = [("/", 0)]
        seen: set[str] = set()
        directory_inodes: set[int] = set()
        total_bytes = 0
        while pending:
            parent, depth = pending.pop()
            if depth > self.MAX_DEPTH:
                raise ValueError("rebuild tree exceeds the directory depth limit")
            for entry in self.reader.list(parent):
                name = _legacy_relative_path(entry.name)
                if "/" in name:
                    raise ValueError("unsafe rebuild entry name: %r" % entry.name)
                relative = _legacy_relative_path(entry.path)
                expected = (parent.rstrip("/") + "/" + name).lstrip("/")
                if relative != expected or relative in seen:
                    raise ValueError(
                        "inconsistent/duplicate rebuild entry path: %r" % entry.path
                    )
                if len(seen) >= self.MAX_ENTRIES:
                    raise ValueError("rebuild tree exceeds the entry limit")
                seen.add(relative)
                if entry.deleted:
                    raise ValueError("rebuild refuses deleted entries: %s" % entry.path)
                if entry.mode is not None:
                    kind = stat.S_IFMT(entry.mode)
                    expected_kind = stat.S_IFDIR if entry.is_dir else stat.S_IFREG
                    if kind not in (0, expected_kind):
                        raise PermissionError(
                            "rebuild refuses links/special entries: %s" % entry.path
                        )
                destination = _legacy_path(stage, relative)
                if entry.is_dir:
                    if entry.inode and entry.inode in directory_inodes:
                        raise ValueError(
                            "rebuild directory cycle/alias: %s" % entry.path
                        )
                    if entry.inode:
                        directory_inodes.add(entry.inode)
                    destination.mkdir()
                    pending.append(("/" + relative, depth + 1))
                else:
                    if (
                        not isinstance(entry.size, int)
                        or isinstance(entry.size, bool)
                        or entry.size < 0
                    ):
                        raise ValueError("invalid rebuild file size: %s" % entry.path)
                    total_bytes += entry.size
                    if total_bytes > image_size:
                        raise ValueError(
                            "rebuild contents exceed the image-size budget"
                        )
                    try:
                        data = self.reader.read(entry.path)
                    except Exception as error:
                        raise OSError(
                            "rebuild could not read %s: %s" % (entry.path, error)
                        ) from error
                    if not isinstance(data, bytes) or len(data) != entry.size:
                        raise OSError("rebuild short/invalid read for %s" % entry.path)
                    _legacy_write_file(stage, relative, data, exclusive=True)

    def _refresh_reader(self) -> None:
        reopen = getattr(self.reader, "reopen", None)
        if callable(reopen):
            reopen()
            self._reader_stale = False

    def _rebuild_with(self, fn) -> None:
        self._assert_profile()
        original = self.image
        parent = original.parent.resolve()
        with _exclusive_image_lock(original):
            identity = self._identity(original)
            original_hash = _sha256_file(original)
            workspace = Path(tempfile.mkdtemp(prefix=".mountfile-rebuild-", dir=parent))
            stage = workspace / "tree"
            stage.mkdir()
            new_img = workspace / "staged.img"
            rollback = workspace / "original.img"
            preserve_workspace = False
            try:
                self._unpack(stage, identity[2])
                fn(stage)
                list(_legacy_tree(stage))
                with new_img.open("xb") as handle:
                    handle.truncate(identity[2])
                result = subprocess.run(
                    self.MKFS[self.fs_type] + [str(new_img)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        "mkfs failed: %s" % (result.stderr or result.stdout)
                    )
                self._fill(stage, new_img)
                if new_img.stat().st_size != identity[2]:
                    raise OSError("mkfs changed the staged image size")
                shutil.copystat(original, new_img)
                _fsync_file(new_img)
                # Cleanup failures, just like detach/read/copy failures, precede commit.
                shutil.rmtree(stage)
                if (
                    self._identity(original) != identity
                    or _sha256_file(original) != original_hash
                ):
                    raise RuntimeError(
                        "source image changed during rebuild; refusing replacement"
                    )
                shutil.copy2(original, rollback)
                _fsync_file(rollback)
                if _sha256_file(rollback) != original_hash:
                    raise OSError("rebuild rollback snapshot does not match the source")
                if (
                    self._identity(original) != identity
                    or _sha256_file(original) != original_hash
                ):
                    raise RuntimeError(
                        "source image changed while creating rollback snapshot"
                    )
                self._reader_stale = True
                self.reader.close()
                committed = False
                try:
                    os.replace(new_img, original)
                    committed = True
                    _fsync_directory(parent)
                    self._refresh_reader()
                except BaseException as commit_error:
                    if committed:
                        try:
                            os.replace(rollback, original)
                            _fsync_directory(parent)
                            if _sha256_file(original) != original_hash:
                                raise OSError("restored rebuild image hash mismatch")
                        except BaseException as rollback_error:
                            preserve_workspace = True
                            self._quarantine_marker.write_text(
                                "workspace=%s\ncommit_error=%r\nrollback_error=%r\n"
                                % (workspace, commit_error, rollback_error),
                                encoding="utf-8",
                            )
                            raise RuntimeError(
                                "rebuild commit/rollback failed; evidence retained at %s"
                                % workspace
                            ) from rollback_error
                    self._refresh_reader()
                    raise
            finally:
                preserve_workspace = (
                    preserve_workspace
                    or getattr(self, "_retained_mount_point", None) is not None
                )
                if preserve_workspace:
                    self._retained_workspace = workspace
                    log.error("rebuild evidence retained at %s", workspace)
                else:
                    try:
                        shutil.rmtree(workspace)
                    except Exception as error:
                        self._retained_workspace = workspace
                        log.error(
                            "rebuild workspace cleanup failed at %s: %s",
                            workspace,
                            error,
                        )
                        raise

    def _fill(self, stage: Path, image: Path) -> None:
        # Preflight before mounting. Revalidate every copied component as well.
        paths = list(_legacy_tree(stage))
        mp = Path(tempfile.mkdtemp(prefix="mnt_"))
        try:
            result = subprocess.run(
                ["mount", "-o", "loop,rw", str(image), str(mp)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "mount failed: %s" % (result.stderr or result.stdout)
                )
        except BaseException as error:
            self._retain_mount(mp, error)
            raise
        try:
            for item in paths:
                if item == stage:
                    continue
                relative = item.relative_to(stage).as_posix()
                source = _legacy_path(stage, relative)
                destination = _legacy_path(mp, relative)
                if stat.S_ISDIR(source.lstat().st_mode):
                    destination.mkdir(exist_ok=True)
                else:
                    data = _legacy_read_file(stage, relative)
                    _legacy_write_file(mp, relative, data, exclusive=True)
                    if _legacy_read_file(mp, relative) != data:
                        raise OSError(
                            "rebuild mounted read-back mismatch: %s" % relative
                        )
            result = subprocess.run(
                ["sync"], capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError("sync failed: %s" % (result.stderr or result.stdout))
        finally:
            self._unmount(mp)

    def write_file(self, path: str, data: bytes) -> None:
        relative = _legacy_relative_path(path)

        def apply(stage: Path):
            destination = _legacy_path(stage, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _legacy_write_file(stage, relative, data)

        self._rebuild_with(apply)

    def mkdir(self, path: str) -> None:
        relative = _legacy_relative_path(path)
        self._rebuild_with(
            lambda stage: _legacy_path(stage, relative).mkdir(parents=True)
        )

    def delete(self, path: str, recursive: bool = False) -> None:
        relative = _legacy_relative_path(path)

        def apply(stage: Path):
            target = _legacy_path(stage, relative)
            if target.is_dir():
                if recursive:
                    list(_legacy_tree(stage, relative))
                shutil.rmtree(target) if recursive else target.rmdir()
            else:
                target.unlink()

        self._rebuild_with(apply)

    def rename(self, src: str, dst: str) -> None:
        source = _legacy_relative_path(src)
        destination = _legacy_relative_path(dst)

        def apply(stage: Path):
            old = _legacy_path(stage, source)
            new = _legacy_path(stage, destination)
            if old == new:
                return
            if old.is_dir() and old in new.parents:
                raise ValueError(
                    "cannot move a rebuild directory into itself/descendant"
                )
            if new.exists():
                raise FileExistsError(new)
            old.rename(new)

        self._rebuild_with(apply)


# =============================================================================
# 10. ФАСАД: UniversalFSRW
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
        self,
        image: Path,
        offset: int | None = None,
        auto_backup: bool = True,
        allow_legacy_mount: bool = False,
        volume_size: int | None = None,
        allow_libguestfs: bool = False,
    ):
        self.image = Path(image)
        self.offset = offset
        self.backend: RWBackend | None = None
        self.journal = RWJournal(self.image)
        self.backup = ImageBackup(self.image)
        self._auto_backup = auto_backup
        self.allow_legacy_mount = bool(allow_legacy_mount)
        self.allow_libguestfs = bool(allow_libguestfs)
        self._requested_volume_size = volume_size
        self._backup_path: Path | None = None
        self._closed = False
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
        if self._requested_volume_size is not None:
            if not isinstance(self._requested_volume_size, int) or isinstance(
                self._requested_volume_size, bool
            ):
                raise ValueError("volume_size must be an integer byte count")
            volume_size = self._requested_volume_size
            if (
                volume_size <= 0
                or off < 0
                or off + volume_size < off
                or off + volume_size > image_size
            ):
                raise ValueError("explicit volume_size exceeds image bounds")
            if (
                candidate is not None
                and candidate.size
                and volume_size > int(candidate.size)
            ):
                raise ValueError("explicit volume_size exceeds detected extent")
        elif candidate is not None and candidate.size:
            volume_size = int(candidate.size)
        elif off == 0:
            # A partitionless whole-image filesystem can use its own validated
            # on-disk length; incidental backup boot-sector signatures are not
            # partition boundaries.
            volume_size = image_size
        else:
            raise RuntimeError(
                "exact volume_size required for nonzero offsets without a "
                "partition-table extent"
            )
        if off + volume_size > image_size:
            raise ValueError("selected volume extent exceeds image bounds")
        self.volume_size = volume_size
        inferred_whole_image_fat = (
            self._requested_volume_size is None
            and off == 0
            and (candidate is None or not candidate.size)
        )
        # ``probe_filesystem`` returns display names such as ``FAT32`` or
        # ``NTFS`` while the backend table is lowercase, so the match below
        # must be case-insensitive.
        fs_type_lower = str(fs_type).lower()
        if self.allow_libguestfs and fs_type_lower.startswith(("ntfs", "ext")):
            try:
                self.backend = LibguestfsRW(
                    self.image,
                    off,
                    volume_size=volume_size,
                    fs_type=fs_type_lower,
                )
                return
            except Exception as exc:
                raise RuntimeError(
                    "explicit libguestfs backend was requested but could not open "
                    "%s: %s" % (fs_type, exc)
                ) from exc
        writer_types = [
            (NativeExfatRW, ("exfat",)),
            (Fat1216RW, ("fat12", "fat16")),
            (Fat32RW, ("fat", "fat12", "fat16", "fat32")),
            (Ext234RW, ("ext2", "ext3", "ext4")),
        ]
        if self.allow_legacy_mount:
            writer_types.append((NtfsRW, ("ntfs",)))
        for cls, types in writer_types:
            if fs_type_lower.startswith(types):
                try:
                    if cls in (Ext234RW, NativeExfatRW):
                        self.backend = cls(self.image, off, volume_size=volume_size)
                    elif cls in (Fat32RW, Fat1216RW):
                        backend = cls(self.image, off, volume_size=volume_size)
                        if (
                            inferred_whole_image_fat
                            and backend.volume_size != volume_size
                        ):
                            backend.close()
                            raise ValueError(
                                "FAT BPB must cover the inferred whole-image extent"
                            )
                        self.backend = backend
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
                        profile = str(fs_type).lower()
                        if profile.startswith(("ext3", "ext4")):
                            raise RuntimeError(
                                "%s native write profile is unsupported; "
                                "no subprocess or legacy fallback was attempted: %s"
                                % (profile, e)
                            ) from e
                        raise RuntimeError(
                            "refusing unsupported ext2 profile without an explicit "
                            "legacy compatibility backend: %s" % e
                        ) from e
                    raise RuntimeError(
                        "refusing filesystem write because the selected native "
                        "profile was declined: %s" % e
                    ) from e
        if fs_type_lower.startswith("ntfs") and not self.allow_legacy_mount:
            raise RuntimeError(
                "native NTFS writes are unsupported and not implemented; pass "
                "allow_legacy_mount=True only for the explicit compatibility backend"
            )
        if not self.allow_legacy_mount:
            raise RuntimeError(
                "no native write profile for %s; legacy mount fallback is disabled"
                % fs_type
            )
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
        if not self._closed and self.backend:
            self.backend.close()
        self._closed = True

    def _require_open(self) -> None:
        if getattr(self, "_closed", False) or self.backend is None:
            raise RuntimeError("filesystem facade is closed")

    def mount(self, options: str = "", mountpoint: str = "/") -> None:
        """Mount an external backend filesystem inside its isolated runtime."""
        method = getattr(self.backend, "mount_options", None)
        if not callable(method):
            raise RuntimeError("selected backend does not expose isolated mounting")
        method(options, mountpoint)

    def unmount(self, path_or_device: str = "/") -> None:
        """Unmount one filesystem from the backend runtime, never the host OS."""
        method = getattr(self.backend, "unmount", None)
        if not callable(method):
            raise RuntimeError("selected backend does not expose isolated unmounting")
        method(path_or_device)

    def umount_all(self) -> None:
        """Unmount all filesystems in the backend runtime."""
        method = getattr(self.backend, "umount_all", None)
        if not callable(method):
            raise RuntimeError("selected backend does not expose isolated unmounting")
        method()

    def shutdown(self) -> None:
        """Flush and stop the backend runtime before closing its handle."""
        method = getattr(self.backend, "shutdown", None)
        if not callable(method):
            self.flush()
            return
        method()

    # ---- read/write -------------------------------------------------------
    def info(self) -> FSInfo:
        self._require_open()
        info = self.backend.info()
        # Keep the backend's identity, while making operation support explicit
        # to CLI, GUI, and WriteEngine callers.  Browsability never implies RW.
        capabilities = self.backend_status(info)
        return FSInfo(
            **{
                **info.to_dict(),
                "read_capabilities": tuple(capabilities["read_capabilities"]),
                "write_capabilities": tuple(capabilities["write_capabilities"]),
                "write_rejection_reason": capabilities["write_rejection_reason"],
                "write_rejection_code": capabilities["write_rejection_code"],
            }
        )

    @staticmethod
    def backend_status(info: FSInfo) -> dict:
        """Return a serialisable, conservative operation capability status."""
        read = list(RW_READ_CAPABILITIES)
        writable = bool(info.writable)
        strategy = str(info.write_strategy or "")
        native_profile = NATIVE_WRITE_PROFILES_BY_STRATEGY.get(strategy)
        native_profile_matches = bool(
            native_profile
            and (
                str(info.fs_type).casefold() == native_profile.filesystem.casefold()
                or (
                    strategy == "native-fat12-16"
                    and str(info.fs_type).casefold() in {"fat12", "fat16"}
                )
            )
            and str(info.backend) == native_profile.backend
        )
        if native_profile_matches:
            rejection_reason = None
            rejection_code = None
            if not writable:
                rejection_reason = (
                    "native write strategy does not match the selected writable "
                    "filesystem/backend profile"
                )
                rejection_code = RefusalCode.OPERATION_NOT_SUPPORTED
            status = get_native_capability_status(
                native_profile,
                backend=info.backend,
                write_enabled=writable,
                write_rejection_reason=rejection_reason,
                write_rejection_code=rejection_code,
            )
            if strategy == "native-fat12-16":
                # The matrix has one shared profile for both variants. Publish
                # the validated selection, not the profile's representative FAT12.
                status["filesystem"] = str(info.fs_type).casefold().upper()
            return status

        rejection_code = None
        if strategy in NATIVE_WRITE_PROFILES_BY_STRATEGY:
            write = []
            reason = (
                "native write strategy does not match the selected writable "
                "filesystem/backend profile"
            )
            rejection_code = RefusalCode.UNSUPPORTED_PROFILE.value
        elif writable:
            write = list(RW_WRITE_CAPABILITIES)
            reason = ""
        else:
            write = []
            reason = "write operations are unsupported for profile %s (%s)" % (
                info.fs_type,
                strategy or "read-only",
            )
            rejection_code = RefusalCode.OPERATION_NOT_SUPPORTED.value
        status = {
            "backend": info.backend,
            "mode": "read-write" if write else "read-only",
            "read_capabilities": read,
            "write_capabilities": write,
            "write_enabled": bool(write),
            "write_rejection_reason": reason,
            "write_rejection_code": rejection_code,
            "write_rejection": (
                None
                if rejection_code is None
                else RefusalRuntimeError(reason, RefusalCode(rejection_code)).to_dict()
            ),
        }
        return status

    def capability_status(self) -> dict:
        """Expose the selected backend's bounded operation support."""
        self._require_open()
        return self.backend_status(self.backend.info())

    def _require_operation(self, operation: str) -> None:
        status = self.capability_status()
        if operation not in status["write_capabilities"]:
            reason = status["write_rejection_reason"] or (
                "operation is not advertised by the selected profile"
            )
            code = RefusalCode.OPERATION_NOT_SUPPORTED
            canonical = canonical_operation_for_api_operation(
                "universal-fs-rw", operation
            )
            if canonical is not None:
                refusal = status.get("operations", {}).get(canonical, {}).get("refusal")
                if isinstance(refusal, dict) and refusal.get("code"):
                    code = RefusalCode(refusal["code"])
            raise RefusalRuntimeError(
                "%s is unsupported for %s: %s" % (operation, status["backend"], reason),
                code,
            )

    def list(self, path: str = "/") -> list[FSEntry]:
        self._require_open()
        return self.backend.list(path)

    def read(self, path: str) -> bytes:
        self._require_open()
        return self.backend.read(path)

    def read_range(
        self, path: str, offset: int = 0, length: int = RW_READ_RANGE_LIMIT
    ) -> bytes:
        """Read a bounded byte range through the RW facade.

        Native readers may provide a direct range method; compatibility backends
        fall back to their existing whole-file read and slice the validated
        result.  The public contract is still bounded and never mutates an
        image.
        """
        self._require_open()
        entry = self.backend.stat(path)
        if entry is None:
            raise FileNotFoundError(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        size = int(entry.size)
        checked_range(offset, 0, upper=size, context="RW file read offset")
        checked_range(
            0, length, upper=RW_READ_RANGE_LIMIT, context="RW file read length"
        )
        length = min(length, size - offset)
        _, end = checked_range(offset, length, upper=size, context="RW file read range")
        if offset >= size:
            return b""
        reader = getattr(self.backend, "_reader", None)
        direct = getattr(reader, "read_file_range", None)
        if callable(direct):
            return direct(path, offset, end - offset)
        return self.backend.read(path)[offset:end]

    def stat(self, path: str) -> FSEntry | None:
        self._require_open()
        return self.backend.stat(path)

    def exists(self, path: str) -> bool:
        self._require_open()
        return self.backend.exists(path)

    def extract(
        self,
        path: str,
        dest: Path,
        *,
        limits: ExtractionLimits | None = None,
    ) -> Path:
        self._require_open()
        return self.backend.extract(path, dest, limits=limits)

    def walk(self, path: str = "/") -> Iterator[FSEntry]:
        self._require_open()
        yield from self.backend.walk(path)

    def _audit_and_backup(self, op: WriteOperation) -> None:
        if self._auto_backup and self._backup_path is None:
            self._backup_path = self.backup.create()
        self.journal.log(op)

    def write_file(self, path: str, data: bytes) -> None:
        self._require_operation("write_file")
        op = WriteOperation("write", path, {"size": len(data)})
        self._audit_and_backup(op)
        self.backend.write_file(path, data)

    def mkdir(self, path: str) -> None:
        self._require_operation("mkdir")
        op = WriteOperation("mkdir", path)
        self._audit_and_backup(op)
        self.backend.mkdir(path)

    def delete(self, path: str, recursive: bool = False) -> None:
        self._require_operation("delete")
        op = WriteOperation("delete", path, {"recursive": recursive})
        self._audit_and_backup(op)
        self.backend.delete(path, recursive)

    def rename(self, src: str, dst: str) -> None:
        self._require_operation("rename")
        op = WriteOperation("rename", src, {"dst": dst})
        self._audit_and_backup(op)
        self.backend.rename(src, dst)

    def link(self, source: str, destination: str) -> None:
        self._require_operation("link")
        op = WriteOperation("hard-link", source, {"dst": destination})
        self._audit_and_backup(op)
        self.backend.link(source, destination)

    # ---- byte-range read/write -------------------------------------------
    def write_range(self, path: str, offset: int, data: bytes) -> None:
        """Write bytes at an offset inside an existing file (grows if needed)."""
        self._require_operation("write_file")
        payload = bytes(data)
        op = WriteOperation("write", path, {"size": len(payload), "offset": offset})
        self._audit_and_backup(op)
        self.backend.write_range(path, offset, payload)

    def append(self, path: str, data: bytes) -> None:
        """Append bytes to an existing file."""
        self._require_operation("write_file")
        payload = bytes(data)
        op = WriteOperation("write", path, {"size": len(payload), "offset": "append"})
        self._audit_and_backup(op)
        self.backend.append(path, payload)

    def truncate(self, path: str, size: int) -> None:
        """Resize an existing file to exactly ``size`` bytes."""
        self._require_operation("write_file")
        op = WriteOperation("write", path, {"size": int(size), "offset": "truncate"})
        self._audit_and_backup(op)
        self.backend.truncate(path, size)

    # ---- maintenance ------------------------------------------------------
    def flush(self) -> dict:
        """Read-only integrity check + sync; never repair image bytes implicitly."""
        result = {"fsck": None, "sync": None}
        info = self.info()
        fs_type = info.fs_type
        offset = self.offset if self.offset is not None else info.offset
        if getattr(info, "write_strategy", "").startswith("native-"):
            result["fsck"] = {
                "skipped": "native metadata was validated after each staged "
                "mutation; no external repair tool was run"
            }
        elif fs_type.startswith(("ext",)) and shutil.which("e2fsck"):
            if offset:
                result["fsck"] = {
                    "skipped": "e2fsck cannot enforce selected partition bounds"
                }
            else:
                r = subprocess.run(
                    ["e2fsck", "-f", "-n", str(self.image)],
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
                    ["fsck.fat", "-n", str(self.image)],
                    capture_output=True,
                    text=True,
                )
                result["fsck"] = {"exit": r.returncode, "out": r.stdout[-1000:]}
        if hasattr(os, "sync"):
            os.sync()
            result["sync"] = "ok"
        return result

    def restore_from_backup(self) -> bool:
        self._require_open()
        latest = self.backup.latest()
        if not latest:
            return False
        with _exclusive_image_lock(self.image):
            self.backend.close()
            self.backend = None
            self._closed = True
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.image.name}.", suffix=".restore", dir=self.image.parent
            )
            os.close(descriptor)
            staged = Path(name)
            try:
                shutil.copy2(latest, staged)
                _fsync_file(staged)
                if _sha256_file(staged) != _sha256_file(latest):
                    raise OSError("restored backup bytes did not match")
                os.replace(staged, self.image)
                _fsync_directory(self.image.parent)
                self._open()
                self._closed = False
            finally:
                staged.unlink(missing_ok=True)
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
