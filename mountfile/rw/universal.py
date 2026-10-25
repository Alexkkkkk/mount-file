#!/usr/bin/env python3
"""
mountfile_universal_rw.py — Universal RW-слой для Mount File (Task 2.1, issue #87)

Стратегии:
  • FAT32 — нативный движок (заглушка, подключается mountfile/fat32.py)
  • ext4  — через debugfs (e2fsprogs)
  • NTFS  — через ntfs-3g (FUSE) или ntfsprogs

Безопасность:
  • RO по умолчанию
  • Полный исходный backup перед первой записью
  • Перед каждой неатомарной mutation — byte-exact full-image snapshot
  • Проверяемая hash-linked SQLite-журнализация и crash recovery

Зависимости: только стандартная библиотека.
Внешние утилиты (опционально): debugfs, ntfs-3g / ntfsmount.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Optional

from mountfile.rw.journal import Journal as _VerifiedJournal

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


# ---------------------------------------------------------------------------
# 1. Hash-chain журнал транзакций
# ---------------------------------------------------------------------------


# All public RW adapters share the byte-exact, verifiable journal.
Journal = _VerifiedJournal


# ---------------------------------------------------------------------------
# 2. Абстрактный RW-слой
# ---------------------------------------------------------------------------


class UniversalRW(ABC):
    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        self.image_path = Path(image_path)
        self.mode = mode
        self.journal = Journal(self.image_path)
        self._backup_made = False
        self._backend_open = False

    # --- публичный API -----------------------------------------------------

    def open(self) -> None:
        if self._backend_open:
            return
        if self.mode == Mode.RW and not self._backup_made:
            self._make_full_backup()
        self._open_backend()
        self._backend_open = True

    def close(self) -> None:
        try:
            if self._backend_open:
                self._close_backend()
                self._backend_open = False
        finally:
            self.journal.close()

    def _rollback_failed_transaction(self, tx_id: str, original_error: Exception):
        reopen_backend = self._backend_open and not getattr(
            self, "atomic_mutations", False
        )
        if reopen_backend:
            try:
                # Mounted filesystems may cache and later flush the partial
                # mutation. Detach before restoring raw image bytes.
                self._close_backend()
                self._backend_open = False
            except Exception as close_error:
                raise RWError(
                    "operation failed (%s); backend could not be detached before "
                    "rollback (%s), so transaction %s remains pending"
                    % (original_error, close_error, tx_id)
                ) from original_error
        try:
            restored = self.journal.rollback(tx_id)
        except Exception as rollback_error:
            raise RWError(
                "operation failed (%s); transaction rollback also failed (%s)"
                % (original_error, rollback_error)
            ) from original_error
        if restored and reopen_backend:
            try:
                self._open_backend()
                self._backend_open = True
            except Exception as reopen_error:
                raise RWError(
                    "transaction %s was restored, but backend could not be "
                    "reopened (%s)" % (tx_id, reopen_error)
                ) from original_error
        if not restored:
            if getattr(self, "atomic_mutations", False) and not getattr(
                original_error, "rollback_failed", False
            ):
                self.journal.cancel(tx_id)
                return
            raise RWError(
                "operation failed (%s); no valid byte backup was available, "
                "so transaction %s remains pending for recovery"
                % (original_error, tx_id)
            ) from original_error

    def _begin_transaction(self, op: str, path: str) -> str:
        tx_id = self.journal.begin(op, path)
        if getattr(self, "atomic_mutations", False):
            return tx_id
        try:
            self.journal.backup_region(0, self.image_path.stat().st_size)
        except Exception:
            # No backend mutation has happened yet, so do not attempt recovery.
            # Preserve the snapshot error even if journal cleanup itself fails.
            try:
                self.journal.cancel(tx_id, snapshot_failed=True)
            except Exception:
                pass
            raise
        return tx_id

    def create_file(
        self, path: str, data: BinaryIO, *, overwrite: bool = False
    ) -> None:
        self._require_rw()
        tx = self._begin_transaction("create_file", path)
        try:
            self._create_file(path, data, overwrite)
            self.journal.commit()
        except Exception as exc:
            self._rollback_failed_transaction(tx, exc)
            raise

    def create_dir(self, path: str) -> None:
        self._require_rw()
        tx = self._begin_transaction("mkdir", path)
        try:
            self._create_dir(path)
            self.journal.commit()
        except Exception as exc:
            self._rollback_failed_transaction(tx, exc)
            raise

    def delete(self, path: str, *, recursive: bool = False) -> None:
        self._require_rw()
        tx = self._begin_transaction("delete", path)
        try:
            self._delete(path, recursive)
            self.journal.commit()
        except Exception as exc:
            self._rollback_failed_transaction(tx, exc)
            raise

    def rename(self, src: str, dst: str) -> None:
        self._require_rw()
        tx = self._begin_transaction("rename", f"{src} -> {dst}")
        try:
            self._rename(src, dst)
            self.journal.commit()
        except Exception as exc:
            self._rollback_failed_transaction(tx, exc)
            raise

    def write_file(self, path: str, data: BinaryIO, *, offset: int = 0) -> None:
        self._require_rw()
        tx = self._begin_transaction("write_file", path)
        try:
            self._write_file(path, data, offset)
            self.journal.commit()
        except Exception as exc:
            self._rollback_failed_transaction(tx, exc)
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
    """UniversalRW adapter over the package's native FAT32 implementation."""

    atomic_mutations = True

    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        super().__init__(image_path, mode)
        self._fh = None
        self._fs = None

    def _open_backend(self) -> None:
        from mountfile.fat32 import Fat32

        self._fh = open(self.image_path, "r+b" if self.mode == Mode.RW else "rb")
        try:
            self._fs = Fat32(self._fh)
        except Exception:
            self._fh.close()
            self._fh = None
            raise

    def _close_backend(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                pass
            self._fh.close()
            self._fh = None
        self._fs = None

    def _backend(self):
        if self._fs is None:
            raise StrategyUnavailable("FAT32 backend is not open; call open()")
        return self._fs

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        clean = "/" + str(path).replace("\\", "/").strip("/")
        parent, _, name = clean.rpartition("/")
        return parent or "/", name

    def _lookup(self, path: str):
        from mountfile.fat32 import Fat32Error

        try:
            return self._backend().entry(path)[1]
        except Fat32Error:
            return None

    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None:
        fs = self._backend()
        parent, name = self._split(path)
        if not name:
            raise RWError("cannot create a file at the FAT32 root")
        existing = self._lookup(path)
        if existing is not None:
            if existing.is_dir:
                raise IsADirectoryError(path)
            if not overwrite:
                raise FileExistsError(path)
        fs.write_file(parent, name, data.read())

    def _create_dir(self, path: str) -> None:
        fs = self._backend()
        parent, name = self._split(path)
        if not name:
            raise RWError("cannot create the FAT32 root directory")
        if self._lookup(path) is not None:
            raise FileExistsError(path)
        fs.mkdir(parent, name)

    def _delete(self, path: str, recursive: bool) -> None:
        self._backend().delete(path, recursive=recursive)

    def _rename(self, src: str, dst: str) -> None:
        fs = self._backend()
        src_parent, _ = self._split(src)
        dst_parent, dst_name = self._split(dst)
        if src_parent != dst_parent:
            raise RWError("FAT32 rename is supported only within one directory")
        fs.rename(src, dst_name)

    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None:
        fs = self._backend()
        parent, name = self._split(path)
        if offset < 0:
            raise RWError("negative write offset")
        payload = data.read()
        if offset:
            try:
                current = bytearray(fs.read_file(path))
            except Exception as exc:
                raise RWError(f"cannot read {path} for offset write: {exc}") from exc
            end = offset + len(payload)
            if end > len(current):
                current.extend(b"\x00" * (end - len(current)))
            current[offset:end] = payload
            payload = bytes(current)
        fs.write_file(parent, name, payload)

    def _stat(self, path: str) -> FileStat:
        entry = self._lookup(path)
        if entry is None:
            raise FileNotFoundError(path)
        return FileStat(path, entry.size, 0.0, entry.is_dir)


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
            try:
                subprocess.run(
                    ["ntfs-3g", "-o", "rw", str(self.image_path), str(mnt)],
                    check=True,
                )
            except Exception:
                try:
                    subprocess.run(["umount", str(mnt)], check=False)
                finally:
                    mnt.rmdir()
                raise
            self._mnt = mnt
        # RO-режим можно не монтировать — чтение остаётся pytsk3.

    def _close_backend(self) -> None:
        if self._mnt:
            result = subprocess.run(
                ["umount", str(self._mnt)], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise RWError("NTFS image could not be safely unmounted: %s" % detail)
            self._mnt.rmdir()
            self._mnt = None

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
