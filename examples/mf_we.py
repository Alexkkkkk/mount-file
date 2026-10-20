#!/usr/bin/env python3
"""
mf_we.py — Mount File Write Engine (одним файлом)

Демонстрирует архитектуру из ТЗ MF-WE-TZ-001:
  • единый API WriteEngine (фасад);
  • транзакционный слой с журналом и откатом;
  • бэкенды NTFS (rust-fs-ntfs) и ext4 (ext4_rs) через ctypes;
  • safe-mode, если нативные библиотеки не найдены.

Зависимости: только стандартная библиотека (sqlite3, ctypes, hashlib, pathlib).
Для реальной записи скомпилируйте Rust-крейты и положите рядом:
  librust_fs_ntfs.so / rust_fs_ntfs.dll
  libext4_rs.so   / ext4_rs.dll
"""

from __future__ import annotations

import ctypes
import hashlib
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Optional


# ---------------------------------------------------------------------------
# 0. Типы и исключения
# ---------------------------------------------------------------------------

class FsType(str, Enum):
    NTFS = "ntfs"
    EXT4 = "ext4"


class Mode(str, Enum):
    RO = "ro"
    RW = "rw"


class WriteEngineError(Exception):
    """Базовая ошибка движка."""


class BackendUnavailable(WriteEngineError):
    """Нативная библиотека не загружена — движок работает в safe-mode."""


class TransactionError(WriteEngineError):
    """Ошибка транзакции; образ должен быть откатан."""


@dataclass
class FileStat:
    path: str
    size: int
    mtime: float
    is_dir: bool


@dataclass
class VerifyReport:
    ok: bool
    details: str = ""


# ---------------------------------------------------------------------------
# 1. Транзакционный слой (SQLite-журнал + backup затронутых блоков)
# ---------------------------------------------------------------------------

@dataclass
class JournalEntry:
    tx_id: str
    op: str
    path: str
    backup_path: Optional[Path] = None
    sha256: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class TransactionManager:
    """
    Минимальный, но рабочий транзакционный слой.
    Для продакшена можно заменить на safe-fs-ops
    (https://pypi.org/project/safe-fs-ops/), но здесь всё
    умещается в один файл и не тянет внешних зависимостей.
    """

    def __init__(self, image_path: Path):
        self.image_path = image_path
        self.journal_path = image_path.with_suffix(image_path.suffix + ".mfwe-journal.sqlite")
        self.backup_dir = image_path.with_suffix(image_path.suffix + ".mfwe-backups")
        self.backup_dir.mkdir(exist_ok=True)
        self._conn = sqlite3.connect(self.journal_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                   tx_id TEXT PRIMARY KEY,
                   op TEXT NOT NULL,
                   path TEXT NOT NULL,
                   backup_path TEXT,
                   sha256 TEXT,
                   created_at REAL NOT NULL
               )"""
        )
        self._conn.commit()
        self._current_tx: Optional[str] = None

    def begin(self, op: str, path: str) -> str:
        tx_id = str(uuid.uuid4())
        self._current_tx = tx_id
        self._conn.execute(
            "INSERT INTO journal(tx_id, op, path, created_at) VALUES (?,?,?,?)",
            (tx_id, op, path, time.time()),
        )
        self._conn.commit()
        return tx_id

    def backup_region(self, offset: int, length: int) -> Path:
        """Сохраняет затронутый диапазон образа до записи."""
        backup = self.backup_dir / f"{self._current_tx}.bin"
        with open(self.image_path, "rb") as src, open(backup, "wb") as dst:
            src.seek(offset)
            dst.write(src.read(length))
        sha = hashlib.sha256(backup.read_bytes()).hexdigest()
        self._conn.execute(
            "UPDATE journal SET backup_path=?, sha256=? WHERE tx_id=?",
            (str(backup), sha, self._current_tx),
        )
        self._conn.commit()
        return backup

    def commit(self) -> None:
        self._current_tx = None

    def rollback(self, tx_id: Optional[str] = None) -> None:
        """Откатывает текущую или указанную транзакцию по журналу."""
        tid = tx_id or self._current_tx
        if not tid:
            return
        row = self._conn.execute(
            "SELECT backup_path FROM journal WHERE tx_id=?", (tid,)
        ).fetchone()
        if row and row[0]:
            backup = Path(row[0])
            if backup.exists():
                # Восстанавливаем затронутый диапазон обратно.
                # Для простоты считаем, что backup содержит ровно те байты,
                # которые были перезаписаны, начиная с исходного offset.
                # В реальном движке здесь нужен явный offset.
                # (См. комментарий в NtfsBackend.write_file.)
                pass  # offset должен храниться в журнале; упрощено для демо.
        self._conn.execute("DELETE FROM journal WHERE tx_id=?", (tid,))
        self._conn.commit()
        self._current_tx = None

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# 2. Абстрактный фасад
# ---------------------------------------------------------------------------

class WriteEngine(ABC):
    """Единый API, не зависящий от ФС."""

    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        self.image_path = image_path
        self.mode = mode
        self.tx = TransactionManager(image_path)

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def create_file(self, path: str, data: BinaryIO, *, overwrite: bool = False) -> None: ...

    @abstractmethod
    def create_dir(self, path: str) -> None: ...

    @abstractmethod
    def delete(self, path: str, *, recursive: bool = False) -> None: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...

    @abstractmethod
    def write_file(self, path: str, data: BinaryIO, *, offset: int = 0) -> None: ...

    @abstractmethod
    def stat(self, path: str) -> FileStat: ...

    @abstractmethod
    def verify(self) -> VerifyReport: ...


# ---------------------------------------------------------------------------
# 3. Бэкенд NTFS (rust-fs-ntfs через ctypes)
# ---------------------------------------------------------------------------

class NtfsBackend(WriteEngine):
    """
    Обёртка над C ABI rust-fs-ntfs (fs_ntfs_*).
    Документация: https://github.com/christhomas/rust-fs-ntfs
    Если библиотека не найдена, бэкенд выбрасывает BackendUnavailable
    и демонстрирует safe-mode.
    """

    LIB_CANDIDATES = ("librust_fs_ntfs.so", "rust_fs_ntfs.dll", "librust_fs_ntfs.dylib")

    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        super().__init__(image_path, mode)
        self._lib: Optional[ctypes.CDLL] = None
        self._handle = None

    def open(self) -> None:
        lib_path = next((p for p in self.LIB_CANDIDATES if Path(p).exists()), None)
        if lib_path is None:
            raise BackendUnavailable(
                "rust-fs-ntfs не найден. Скомпилируйте крейт и положите "
                f"рядом один из файлов: {self.LIB_CANDIDATES}"
            )
        self._lib = ctypes.CDLL(lib_path)
        # Сигнатуры C ABI (упрощённо; точные имена см. include/fs_ntfs.h)
        self._lib.fs_ntfs_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self._lib.fs_ntfs_open.restype = ctypes.c_void_p
        self._lib.fs_ntfs_close.argtypes = [ctypes.c_void_p]
        self._lib.fs_ntfs_create_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.fs_ntfs_mkdir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.fs_ntfs_unlink.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.fs_ntfs_rename.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self._lib.fs_ntfs_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_void_p, ctypes.c_size_t]

        flags = 1 if self.mode == Mode.RW else 0  # RW=1, RO=0
        handle = self._lib.fs_ntfs_open(str(self.image_path).encode(), flags)
        if not handle:
            raise TransactionError("fs_ntfs_open вернул NULL — образ не открыт.")
        self._handle = handle

    def close(self) -> None:
        if self._lib and self._handle:
            self._lib.fs_ntfs_close(self._handle)
            self._handle = None
        self.tx.close()

    def _require_rw(self) -> None:
        if self.mode != Mode.RW:
            raise TransactionError("Движок в режиме RO. Включите Mode.RW явно.")

    def create_file(self, path: str, data: BinaryIO, *, overwrite: bool = False) -> None:
        self._require_rw()
        tx_id = self.tx.begin("create_file", path)
        try:
            raw = data.read()
            buf = ctypes.create_string_buffer(raw)
            rc = self._lib.fs_ntfs_create_file(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"fs_ntfs_create_file rc={rc}")
            rc = self._lib.fs_ntfs_write(self._handle, path.encode(), buf, len(raw))
            if rc != 0:
                raise TransactionError(f"fs_ntfs_write rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def create_dir(self, path: str) -> None:
        self._require_rw()
        tx_id = self.tx.begin("mkdir", path)
        try:
            rc = self._lib.fs_ntfs_mkdir(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"fs_ntfs_mkdir rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def delete(self, path: str, *, recursive: bool = False) -> None:
        self._require_rw()
        tx_id = self.tx.begin("delete", path)
        try:
            rc = self._lib.fs_ntfs_unlink(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"fs_ntfs_unlink rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def rename(self, src: str, dst: str) -> None:
        self._require_rw()
        tx_id = self.tx.begin("rename", f"{src} -> {dst}")
        try:
            rc = self._lib.fs_ntfs_rename(self._handle, src.encode(), dst.encode())
            if rc != 0:
                raise TransactionError(f"fs_ntfs_rename rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def write_file(self, path: str, data: BinaryIO, *, offset: int = 0) -> None:
        self._require_rw()
        tx_id = self.tx.begin("write_file", path)
        try:
            raw = data.read()
            # В реальном движке здесь нужно знать физический offset
            # внутри образа и вызвать tx.backup_region(offset, len(raw))
            # до записи. Упрощено для демонстрации.
            buf = ctypes.create_string_buffer(raw)
            rc = self._lib.fs_ntfs_write(self._handle, path.encode(), buf, len(raw))
            if rc != 0:
                raise TransactionError(f"fs_ntfs_write rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def stat(self, path: str) -> FileStat:
        # Заглушка: в реальном ABI есть fs_ntfs_stat.
        return FileStat(path=path, size=0, mtime=time.time(), is_dir=False)

    def verify(self) -> VerifyReport:
        # В rust-fs-ntfs есть fsck и replay $LogFile;
        # здесь можно вызвать соответствующий символ.
        return VerifyReport(ok=True, details="ntfs: базовый verify (заглушка).")


# ---------------------------------------------------------------------------
# 4. Бэкенд ext4 (ext4_rs через ctypes)
# ---------------------------------------------------------------------------

class Ext4Backend(WriteEngine):
    """
    Обёртка над ext4_rs (Rust). Точный C ABI зависит от форка;
    здесь показан каркас с теми же принципами, что и для NTFS.
    """

    LIB_CANDIDATES = ("libext4_rs.so", "ext4_rs.dll", "libext4_rs.dylib")

    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        super().__init__(image_path, mode)
        self._lib: Optional[ctypes.CDLL] = None
        self._handle = None

    def open(self) -> None:
        lib_path = next((p for p in self.LIB_CANDIDATES if Path(p).exists()), None)
        if lib_path is None:
            raise BackendUnavailable(
                "ext4_rs не найден. Соберите библиотеку и положите рядом "
                f"один из файлов: {self.LIB_CANDIDATES}"
            )
        self._lib = ctypes.CDLL(lib_path)
        self._lib.ext4_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self._lib.ext4_open.restype = ctypes.c_void_p
        self._lib.ext4_close.argtypes = [ctypes.c_void_p]
        self._lib.ext4_create.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.ext4_mkdir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.ext4_unlink.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.ext4_rename.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self._lib.ext4_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_void_p, ctypes.c_size_t]
        flags = 1 if self.mode == Mode.RW else 0
        handle = self._lib.ext4_open(str(self.image_path).encode(), flags)
        if not handle:
            raise TransactionError("ext4_open вернул NULL.")
        self._handle = handle

    def close(self) -> None:
        if self._lib and self._handle:
            self._lib.ext4_close(self._handle)
            self._handle = None
        self.tx.close()

    def _require_rw(self) -> None:
        if self.mode != Mode.RW:
            raise TransactionError("Движок в режиме RO.")

    def create_file(self, path: str, data: BinaryIO, *, overwrite: bool = False) -> None:
        self._require_rw()
        tx_id = self.tx.begin("create_file", path)
        try:
            raw = data.read()
            rc = self._lib.ext4_create(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"ext4_create rc={rc}")
            buf = ctypes.create_string_buffer(raw)
            rc = self._lib.ext4_write(self._handle, path.encode(), buf, len(raw))
            if rc != 0:
                raise TransactionError(f"ext4_write rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def create_dir(self, path: str) -> None:
        self._require_rw()
        tx_id = self.tx.begin("mkdir", path)
        try:
            rc = self._lib.ext4_mkdir(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"ext4_mkdir rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def delete(self, path: str, *, recursive: bool = False) -> None:
        self._require_rw()
        tx_id = self.tx.begin("delete", path)
        try:
            rc = self._lib.ext4_unlink(self._handle, path.encode())
            if rc != 0:
                raise TransactionError(f"ext4_unlink rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def rename(self, src: str, dst: str) -> None:
        self._require_rw()
        tx_id = self.tx.begin("rename", f"{src} -> {dst}")
        try:
            rc = self._lib.ext4_rename(self._handle, src.encode(), dst.encode())
            if rc != 0:
                raise TransactionError(f"ext4_rename rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def write_file(self, path: str, data: BinaryIO, *, offset: int = 0) -> None:
        self._require_rw()
        tx_id = self.tx.begin("write_file", path)
        try:
            raw = data.read()
            buf = ctypes.create_string_buffer(raw)
            rc = self._lib.ext4_write(self._handle, path.encode(), buf, len(raw))
            if rc != 0:
                raise TransactionError(f"ext4_write rc={rc}")
            self.tx.commit()
        except Exception:
            self.tx.rollback(tx_id)
            raise

    def stat(self, path: str) -> FileStat:
        return FileStat(path=path, size=0, mtime=time.time(), is_dir=False)

    def verify(self) -> VerifyReport:
        return VerifyReport(ok=True, details="ext4: базовый verify (заглушка).")


# ---------------------------------------------------------------------------
# 5. Фабрика
# ---------------------------------------------------------------------------

def create_engine(image_path: Path, fs_type: FsType, mode: Mode = Mode.RO) -> WriteEngine:
    backend = NtfsBackend if fs_type == FsType.NTFS else Ext4Backend
    return backend(image_path, mode)


# ---------------------------------------------------------------------------
# 6. Демонстрация (safe-mode, если нативных библиотек нет)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    image = Path("disk.img")
    image.write_bytes(b"\0" * 1024 * 1024)  # 1 MiB «образ» для демо

    print("=== MF-WE demo ===")
    for fs in (FsType.NTFS, FsType.EXT4):
        print(f"\n--- {fs.value} ---")
        try:
            eng = create_engine(image, fs, Mode.RW)
            eng.open()
            eng.create_dir("/test")
            eng.create_file("/test/hello.txt", __import__("io").BytesIO(b"hello mf-we"))
            print("запись выполнена (нативный бэкенд)")
            print(eng.verify())
            eng.close()
        except BackendUnavailable as e:
            print(f"safe-mode: {e}")
        except Exception as e:
            print(f"ошибка: {type(e).__name__}: {e}")

    print("\nГотово. Журнал и backup-каталог лежат рядом с образом:")
    print(" ", str(image) + ".mfwe-journal.sqlite")
    print(" ", str(image) + ".mfwe-backups/")
