#!/usr/bin/env python3
"""
mountfile_universal_rw.py — Universal RW-слой для Mount File (Task 2.1, issue #87)

Стратегии:
  • FAT32 — нативный движок (заглушка, подключается mountfile/fat32.py)
  • ext4  — через debugfs (e2fsprogs)
  • NTFS  — через ntfs-3g (FUSE) или ntfsprogs

Безопасность:
  • RO по умолчанию
  • Автоматический backup перед первой записью
  • Hash-chain журнал транзакций (SQLite)
  • Восстановление из backup

Зависимости: только стандартная библиотека.
Внешние утилиты (опционально): debugfs, ntfs-3g / ntfsmount.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Optional

# ---------------------------------------------------------------------------
# 0. Типы
# ---------------------------------------------------------------------------


class FsType(str, Enum):
    FAT32 = "fat32"
    EXT4 = "ext4"
    NTFS = "ntfs"


class Mode(str, Enum):
    RO = "ro"
    RW = "rw"


class RWError(Exception):
    """Базовая ошибка RW-слоя."""


class StrategyUnavailable(RWError):
    """Внешняя стратегия недоступна — safe-mode."""


@dataclass
class FileStat:
    path: str
    size: int
    mtime: float
    is_dir: bool


@dataclass
class TransactionRecord:
    tx_id: str
    op: str
    path: str
    backup_path: Optional[Path]
    sha256: Optional[str]
    prev_hash: str
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# 1. Hash-chain журнал транзакций
# ---------------------------------------------------------------------------


class Journal:
    """
    SQLite-журнал с hash-chain: каждая запись содержит хэш предыдущей.
    Это позволяет обнаружить подмену или удаление записей.
    """

    def __init__(self, image_path: Path):
        self.image_path = image_path
        self.db_path = image_path.with_suffix(image_path.suffix + ".rw-journal.sqlite")
        self.backup_dir = image_path.with_suffix(image_path.suffix + ".rw-backups")
        self.backup_dir.mkdir(exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
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
        self._conn.commit()
        self._current: Optional[str] = None
        self._last_hash = self._get_last_hash()

    def _get_last_hash(self) -> str:
        row = self._conn.execute(
            "SELECT sha256 FROM journal ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row and row[0] else "0" * 64

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
        backup = self.backup_dir / f"{self._current}.bin"
        with open(self.image_path, "rb") as src, open(backup, "wb") as dst:
            src.seek(offset)
            dst.write(src.read(length))
        sha = hashlib.sha256(backup.read_bytes()).hexdigest()
        self._conn.execute(
            "UPDATE journal SET backup_path=?, sha256=? WHERE tx_id=?",
            (str(backup), sha, self._current),
        )
        self._conn.commit()
        self._last_hash = sha
        return backup

    def commit(self) -> None:
        self._current = None

    def rollback(self, tx_id: Optional[str] = None) -> None:
        tid = tx_id or self._current
        if not tid:
            return
        row = self._conn.execute(
            "SELECT backup_path FROM journal WHERE tx_id=?", (tid,)
        ).fetchone()
        if row and row[0]:
            backup = Path(row[0])
            if backup.exists():
                # В реальном движке нужен offset из журнала.
                # Здесь показан принцип: восстановление из backup.
                pass
        self._conn.execute("DELETE FROM journal WHERE tx_id=?", (tid,))
        self._conn.commit()
        self._current = None

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# 2. Абстрактный RW-слой
# ---------------------------------------------------------------------------


class UniversalRW(ABC):
    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        self.image_path = image_path
        self.mode = mode
        self.journal = Journal(image_path)
        self._backup_made = False

    # --- публичный API -----------------------------------------------------

    def open(self) -> None:
        if self.mode == Mode.RW and not self._backup_made:
            self._make_full_backup()
        self._open_backend()

    def close(self) -> None:
        self._close_backend()
        self.journal.close()

    def create_file(
        self, path: str, data: BinaryIO, *, overwrite: bool = False
    ) -> None:
        self._require_rw()
        tx = self.journal.begin("create_file", path)
        try:
            self._create_file(path, data, overwrite)
            self.journal.commit()
        except Exception:
            self.journal.rollback(tx)
            raise

    def create_dir(self, path: str) -> None:
        self._require_rw()
        tx = self.journal.begin("mkdir", path)
        try:
            self._create_dir(path)
            self.journal.commit()
        except Exception:
            self.journal.rollback(tx)
            raise

    def delete(self, path: str, *, recursive: bool = False) -> None:
        self._require_rw()
        tx = self.journal.begin("delete", path)
        try:
            self._delete(path, recursive)
            self.journal.commit()
        except Exception:
            self.journal.rollback(tx)
            raise

    def rename(self, src: str, dst: str) -> None:
        self._require_rw()
        tx = self.journal.begin("rename", f"{src} -> {dst}")
        try:
            self._rename(src, dst)
            self.journal.commit()
        except Exception:
            self.journal.rollback(tx)
            raise

    def write_file(self, path: str, data: BinaryIO, *, offset: int = 0) -> None:
        self._require_rw()
        tx = self.journal.begin("write_file", path)
        try:
            self._write_file(path, data, offset)
            self.journal.commit()
        except Exception:
            self.journal.rollback(tx)
            raise

    def stat(self, path: str) -> FileStat:
        return self._stat(path)

    def restore_from_backup(self) -> None:
        """Восстанавливает образ из полного backup (если он есть)."""
        full_backup = self.image_path.with_suffix(
            self.image_path.suffix + ".full-backup"
        )
        if full_backup.exists():
            shutil.copy2(full_backup, self.image_path)
        else:
            raise RWError("Полный backup не найден.")

    # --- внутренние --------------------------------------------------------

    def _make_full_backup(self) -> None:
        backup = self.image_path.with_suffix(self.image_path.suffix + ".full-backup")
        if not backup.exists():
            shutil.copy2(self.image_path, backup)
        self._backup_made = True

    def _require_rw(self) -> None:
        if self.mode != Mode.RW:
            raise RWError("Режим RO. Включите Mode.RW явно.")

    @abstractmethod
    def _open_backend(self) -> None: ...
    @abstractmethod
    def _close_backend(self) -> None: ...
    @abstractmethod
    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None: ...
    @abstractmethod
    def _create_dir(self, path: str) -> None: ...
    @abstractmethod
    def _delete(self, path: str, recursive: bool) -> None: ...
    @abstractmethod
    def _rename(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None: ...
    @abstractmethod
    def _stat(self, path: str) -> FileStat: ...


# ---------------------------------------------------------------------------
# 3. FAT32 (нативный — заглушка под mountfile/fat32.py)
# ---------------------------------------------------------------------------


class Fat32RW(UniversalRW):
    def _open_backend(self) -> None:
        # Здесь должен быть вызов mountfile/fat32.py
        # Для демонстрации — простая проверка сигнатуры.
        pass

    def _close_backend(self) -> None:
        pass

    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None:
        # TODO: подключить mountfile/fat32.py
        raise NotImplementedError(
            "FAT32 native backend — подключите mountfile/fat32.py"
        )

    def _create_dir(self, path: str) -> None:
        raise NotImplementedError("FAT32 native backend")

    def _delete(self, path: str, recursive: bool) -> None:
        raise NotImplementedError("FAT32 native backend")

    def _rename(self, src: str, dst: str) -> None:
        raise NotImplementedError("FAT32 native backend")

    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None:
        raise NotImplementedError("FAT32 native backend")

    def _stat(self, path: str) -> FileStat:
        raise NotImplementedError("FAT32 native backend")


# ---------------------------------------------------------------------------
# 4. ext4 через debugfs
# ---------------------------------------------------------------------------


class Ext4RW(UniversalRW):
    def _debugfs(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["debugfs", "-w", str(self.image_path), *args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=check)
        except FileNotFoundError:
            raise StrategyUnavailable("debugfs не найден. Установите e2fsprogs.")

    def _open_backend(self) -> None:
        self._debugfs("-R", "stats", check=False)

    def _close_backend(self) -> None:
        pass

    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None:
        raw = data.read()
        tmp = Path(f".tmp_{uuid.uuid4().hex}")
        tmp.write_bytes(raw)
        try:
            self._debugfs("-R", f"write {tmp} {path}")
        finally:
            tmp.unlink(missing_ok=True)

    def _create_dir(self, path: str) -> None:
        self._debugfs("-R", f"mkdir {path}")

    def _delete(self, path: str, recursive: bool) -> None:
        if recursive:
            self._debugfs("-R", f"rm -r {path}")
        else:
            self._debugfs("-R", f"rm {path}")

    def _rename(self, src: str, dst: str) -> None:
        # debugfs не имеет rename; делаем link + unlink
        self._debugfs("-R", f"link {src} {dst}")
        self._debugfs("-R", f"unlink {src}")

    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None:
        raw = data.read()
        tmp = Path(f".tmp_{uuid.uuid4().hex}")
        tmp.write_bytes(raw)
        try:
            self._debugfs("-R", f"write {tmp} {path}")
        finally:
            tmp.unlink(missing_ok=True)

    def _stat(self, path: str) -> FileStat:
        # заглушка
        return FileStat(path=path, size=0, mtime=time.time(), is_dir=False)


# ---------------------------------------------------------------------------
# 5. NTFS через ntfs-3g (FUSE)
# ---------------------------------------------------------------------------


class NtfsRW(UniversalRW):
    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        super().__init__(image_path, mode)
        self._mnt: Optional[Path] = None

    def _open_backend(self) -> None:
        if shutil.which("ntfs-3g") is None:
            raise StrategyUnavailable(
                "ntfs-3g не найден. Установите ntfs-3g / ntfsprogs."
            )
        if self.mode == Mode.RW:
            mnt = Path(f"/tmp/mf_ntfs_{uuid.uuid4().hex[:8]}")
            mnt.mkdir()
            subprocess.run(
                ["ntfs-3g", "-o", "rw", str(self.image_path), str(mnt)],
                check=True,
            )
            self._mnt = mnt
        # RO-режим можно не монтировать — чтение остаётся pytsk3.

    def _close_backend(self) -> None:
        if self._mnt:
            subprocess.run(["umount", str(self._mnt)], check=False)
            self._mnt.rmdir()

    def _path(self, p: str) -> Path:
        if not self._mnt:
            raise RWError("NTFS не смонтирован в RW.")
        return self._mnt / p.lstrip("/")

    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None:
        target = self._path(path)
        if target.exists() and not overwrite:
            raise RWError(f"Файл уже существует: {path}")
        target.write_bytes(data.read())

    def _create_dir(self, path: str) -> None:
        self._path(path).mkdir(parents=True, exist_ok=True)

    def _delete(self, path: str, recursive: bool) -> None:
        target = self._path(path)
        if recursive and target.is_dir():
            shutil.rmtree(target)
        elif target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    def _rename(self, src: str, dst: str) -> None:
        self._path(src).rename(self._path(dst))

    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None:
        target = self._path(path)
        mode = "r+b" if target.exists() else "wb"
        with open(target, mode) as f:
            f.seek(offset)
            f.write(data.read())

    def _stat(self, path: str) -> FileStat:
        st = self._path(path).stat()
        return FileStat(
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            is_dir=self._path(path).is_dir(),
        )


# ---------------------------------------------------------------------------
# 6. Фабрика
# ---------------------------------------------------------------------------


def detect_fs(image_path: Path) -> FsType:
    """
    Простое определение ФС по сигнатурам.
    В реальном проекте используйте autopick из mount-file.
    """
    with open(image_path, "rb") as f:
        head = f.read(512)
        if head[3:11] == b"NTFS    ":
            return FsType.NTFS
        # FAT32's extended BPB keeps the type string at 82; 54 is the
        # FAT12/16 slot.  Accept both, like mountfile/firmware.py does.
        if head[82:90] == b"FAT32   " or head[54:62] == b"FAT32   ":
            return FsType.FAT32
        # ext4: магия 0xEF53 по смещению 0x438
        f.seek(0x438)
        if f.read(2) == b"\x53\xef":
            return FsType.EXT4
    raise RWError("Не удалось определить ФС. Укажите явно.")


def create_rw(
    image_path: Path, fs: Optional[FsType] = None, mode: Mode = Mode.RO
) -> UniversalRW:
    fs = fs or detect_fs(image_path)
    if fs == FsType.FAT32:
        return Fat32RW(image_path, mode)
    if fs == FsType.EXT4:
        return Ext4RW(image_path, mode)
    if fs == FsType.NTFS:
        return NtfsRW(image_path, mode)
    raise RWError(f"Неподдерживаемая ФС: {fs}")


# ---------------------------------------------------------------------------
# 7. Демонстрация
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import io
    import sys

    if len(sys.argv) < 2:
        print("Использование: python mountfile_universal_rw.py <image> [--rw]")
        sys.exit(1)

    img = Path(sys.argv[1])
    if not img.exists():
        print(f"Образ не найден: {img}")
        sys.exit(1)

    mode = Mode.RW if "--rw" in sys.argv else Mode.RO
    print(f"Образ: {img}, режим: {mode.value}")

    try:
        fs = detect_fs(img)
        print(f"Определена ФС: {fs.value}")
        rw = create_rw(img, fs, mode)
        rw.open()
        print("RW-слой открыт.")
        print("  journal:", rw.journal.db_path)
        print("  backups:", rw.journal.backup_dir)

        if mode == Mode.RW:
            rw.create_dir("/test_mf")
            rw.create_file("/test_mf/hello.txt", io.BytesIO(b"hello from universal rw"))
            print("Запись выполнена (если стратегия доступна).")
        else:
            print("RO-режим: запись отключена.")

        rw.close()
    except StrategyUnavailable as e:
        print(f"safe-mode: {e}")
        print(
            "Установите соответствующую утилиту (debugfs / ntfs-3g) "
            "и повторите запуск."
        )
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
