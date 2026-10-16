#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MOUNTFILE_FIX — auto-patched + OLMA layer
"""
mountfile_fix.py — единый патч-скрипт для проекта Mount File
с внедрённым OLMA-слоем (Object-Level Memory Access).

Возможности:
  1. Находит корень проекта (mountfile/ + main.py).
  2. Делает резервные копии (*.bak) всех изменяемых файлов.
  3. Создаёт OLMA-модуль mountfile/olma.py со всеми обёртками.
  4. Патчит fat32.py, vfs.py, partition.py, updater.py, main.py,
     qt_compat.py, requirements.txt — с использованием OLMA.
  5. Проверяет синтаксис через py_compile и ast.
  6. Опционально запускает smoke-тест.
  7. Идемпотентен (маркер MOUNTFILE_FIX).

Запуск:
  python mountfile_fix.py                # применить
  python mountfile_fix.py --dry-run      # показать план
  python mountfile_fix.py --revert       # откат из .bak
  python mountfile_fix.py --check        # только проверка синтаксиса
  python mountfile_fix.py --smoke        # применить + smoke-тест
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
BAK_SUFFIX = ".bak"


# =========================================================================== #
# УТИЛИТЫ ПАТЧ-СКРИПТА
# =========================================================================== #

def find_project_root(start: Path) -> Path:
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


def write(p: Path, t: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] изменил бы {p}")
        return
    p.write_text(t, encoding="utf-8")


def backup(p: Path, *, dry_run: bool) -> None:
    if not p.exists():
        return
    bak = Path(str(p) + BAK_SUFFIX)
    if bak.exists():
        return
    if dry_run:
        print(f"  [dry-run] создал бы бэкап {bak}")
        return
    shutil.copy2(p, bak)


def _insert_header(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    at = 0
    for i, ln in enumerate(lines[:3]):
        if ln.startswith("#!") or "coding" in ln or "encoding" in ln:
            at = i + 1
    lines.insert(at, header)
    return "".join(lines)


def patch_file(path, *, dry_run, force=False, transform=None, header=None) -> bool:
    if not path.exists():
        print(f"  [skip] нет файла: {path.name}")
        return False
    orig = read(path)
    if MARKER in orig and not force:
        print(f"  [ok] уже пропатчен: {path.name}")
        return False
    if transform is None:
        return False
    try:
        new = transform(orig)
    except Exception as e:
        print(f"  [err] {path.name}: {e}")
        return False
    if not new or new == orig:
        print(f"  [skip] изменений нет: {path.name}")
        return False
    if header is None:
        header = f"{MARKER} — auto-patched + OLMA\n"
    new = _insert_header(new, header)
    backup(path, dry_run=dry_run)
    write(path, new, dry_run=dry_run)
    print(f"  [fixed] {path.name}")
    return True


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


def syntax_ok(p: Path) -> bool:
    try:
        ast.parse(read(p), filename=str(p))
        return True
    except SyntaxError as e:
        print(f"  [syntax-err] {p}: {e}")
        return False


def compile_file(p: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory() as td:
            py_compile.compile(str(p), cfile=os.path.join(td, "o.pyc"), doraise=True)
        return True
    except py_compile.PyCompileError as e:
        print(f"  [compile-err] {p}: {e}")
        return False


# =========================================================================== #
# ШАБЛОН: mountfile/olma.py
# =========================================================================== #

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


# =========================================================================== #
# ШАБЛОН: qt_compat.py (с OLMA-совместимостью)
# =========================================================================== #

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


# =========================================================================== #
# ПАТЧИ МОДУЛЕЙ — с использованием OLMA
# =========================================================================== #

OLMA_IMPORT_BLOCK = '''
# --- MOUNTFILE_FIX: OLMA-интеграция ---------------------------------------
from mountfile.olma import (  # noqa: E402
    OlmaError, OlmaBoundsError, OlmaIntegrityError, OlmaStateError,
    OlmaHandle, OlmaView, OlmaBuffer,
    OlmaCluster, OlmaChain,
    OlmaPartition, OlmaPartitionTable,
    OlmaTransaction, OlmaXorDecoder,
    safe_download, verify_sha256,
    SECTOR, FAT32_MAX_CLUSTER, FAT32_EOC_MIN, FAT32_BAD,
)
# --- /MOUNTFILE_FIX --------------------------------------------------------
'''


def patch_partition(text: str) -> str:
    """Заменяет валидацию MBR/GPT на использование OlmaPartitionTable."""
    if "OlmaPartitionTable" in text:
        return text

    helper = '''

# --- MOUNTFILE_FIX: OLMA-валидация MBR/GPT ---------------------------------

def _validate_mbr(buf: bytes) -> bool:
    try:
        t = OlmaPartitionTable(OlmaBuffer(buf))
        return t.kind in ("mbr", "gpt")
    except OlmaError:
        return False


def _validate_gpt(buf: bytes) -> bool:
    try:
        t = OlmaPartitionTable(OlmaBuffer(buf))
        return t.kind == "gpt"
    except OlmaError:
        return False


def parse_partitions_safe(buf: bytes) -> list:
    """Возвращает список OlmaPartition из буфера."""
    try:
        return OlmaPartitionTable(OlmaBuffer(buf)).partitions
    except OlmaError:
        return []


def _clamp_partition(start: int, size: int, total_sectors: int):
    p = OlmaPartition(0, start, size).clamp(total_sectors)
    return p.start_sector, p.size_sectors

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    return _insert_after_imports(text, OLMA_IMPORT_BLOCK + helper)


def patch_fat32(text: str) -> str:
    """Заменяет проверки кластеров на OlmaCluster/OlmaChain."""
    if "OlmaChain" in text:
        return text

    helper = '''

# --- MOUNTFILE_FIX: OLMA-обёртки FAT32 -------------------------------------

def _check_cluster_range(cluster: int, max_cluster: int = FAT32_MAX_CLUSTER) -> bool:
    try:
        OlmaCluster(cluster, max_cluster)
        return True
    except OlmaBoundsError:
        return False


def _is_eoc(value: int) -> bool:
    return value >= FAT32_EOC_MIN


def _is_bad(value: int) -> bool:
    return value == FAT32_BAD


def _iter_chain(start: int, fat_get, max_cluster: int = FAT32_MAX_CLUSTER):
    """Безопасный обход цепочки через OlmaChain."""
    for c in OlmaChain(start, fat_get, max_cluster):
        yield c.value


def _safe_free_chain(fat_get, fat_set, start: int,
                     max_cluster: int = FAT32_MAX_CLUSTER) -> int:
    """Безопасное освобождение цепочки через OlmaChain."""
    freed = 0
    try:
        for c in OlmaChain(start, fat_get, max_cluster):
            fat_set(c.value, 0)
            freed += 1
    except (OlmaError, OlmaIntegrityError):
        pass
    return freed

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    return _insert_after_imports(text, OLMA_IMPORT_BLOCK + helper)


def patch_vfs(text: str) -> str:
    """Заменяет safe_read на OlmaView-based реализацию."""
    if "OlmaView" in text:
        return text

    helper = '''

# --- MOUNTFILE_FIX: OLMA-безопасное чтение ---------------------------------

def safe_read(fh, offset: int, size: int, *, chunk: int = 1 << 20) -> bytes:
    """Читает через OlmaHandle, если fh — OlmaHandle; иначе fallback."""
    if isinstance(fh, OlmaHandle):
        return fh.read_upto(offset, size)
    # Fallback для обычных файловых объектов
    if size <= 0 or offset < 0:
        return b""
    out = bytearray()
    fh.seek(offset)
    remaining = size
    while remaining > 0:
        part = fh.read(min(chunk, remaining))
        if not part:
            break
        out.extend(part)
        remaining -= len(part)
    return bytes(out)


def safe_read_exact(fh, offset: int, size: int):
    if isinstance(fh, OlmaHandle):
        try:
            return fh.read_at(offset, size)
        except OlmaError:
            return None
    data = safe_read(fh, offset, size)
    return data if len(data) == size else None

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    return _insert_after_imports(text, OLMA_IMPORT_BLOCK + helper)


def patch_updater(text: str) -> str:
    """Заменяет скачивание/проверку на OLMA-версии."""
    if "safe_download" in text and "OlmaTransaction" in text:
        return text

    helper = '''

# --- MOUNTFILE_FIX: OLMA-безопасное обновление -----------------------------

def _safe_download(url: str, *, timeout: float = 30.0,
                   max_size: int = 512 * 1024 * 1024) -> bytes:
    return safe_download(url, timeout=timeout, max_size=max_size)


def _verify_sha256(data: bytes, expected_hex: str) -> bool:
    return verify_sha256(data, expected_hex)


def _atomic_write(path, data: bytes) -> None:
    """Атомарная запись через OlmaTransaction."""
    with OlmaTransaction(path) as tx:
        tx.write_all(data)

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    return _insert_after_imports(text, OLMA_IMPORT_BLOCK + helper)


def patch_main(text: str) -> str:
    """Добавляет --diag с информацией об OLMA и защиту QApplication."""
    if "_mf_diag" in text:
        return text

    helper = '''

# --- MOUNTFILE_FIX: OLMA-диагностика ---------------------------------------

def _mf_diag() -> int:
    import platform
    import sys as _sys
    try:
        from mountfile import __version__ as _v  # type: ignore
    except Exception:
        _v = "unknown"
    print("MountFile diag (OLMA)")
    print(f"  python:   {_sys.version.split()[0]}")
    print(f"  platform: {platform.platform()}")
    print(f"  version:  {_v}")
    try:
        from mountfile import qt_compat  # type: ignore
        print(f"  qt:       {qt_compat.QT_VERSION}")
    except Exception as e:
        print(f"  qt:       NOT AVAILABLE ({e!r})")
    try:
        from mountfile import olma  # type: ignore
        print(f"  olma:     OK (FAT32_MAX_CLUSTER={olma.FAT32_MAX_CLUSTER:#x})")
    except Exception as e:
        print(f"  olma:     FAIL ({e!r})")
    try:
        import pytsk3  # type: ignore
        print(f"  pytsk3:   {getattr(pytsk3, '__version__', 'ok')}")
    except Exception:
        print("  pytsk3:   NOT AVAILABLE")
    return 0


def _mf_ensure_app():
    from mountfile import qt_compat  # type: ignore
    qt_compat.require_qt()
    app = qt_compat.QtWidgets.QApplication.instance()
    if app is None:
        app = qt_compat.QtWidgets.QApplication([])
    return app

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    text = _insert_after_imports(text, helper)
    if 'if __name__ == "__main__":' in text and '--diag' not in text:
        text = text.replace(
            'if __name__ == "__main__":',
            'if __name__ == "__main__":\n'
            '    import sys as _sys\n'
            '    if "--diag" in _sys.argv:\n'
            '        raise SystemExit(_mf_diag())',
            1,
        )
    return text


def patch_requirements(root: Path, *, dry_run: bool) -> bool:
    p = root / "requirements.txt"
    if not p.exists():
        return False
    t = read(p)
    if MARKER in t:
        print(f"  [ok] уже пропатчен: {p.name}")
        return False
    out = []
    for ln in t.splitlines():
        s = ln.strip()
        if s.startswith("pytsk3"):
            out.append("pytsk3>=20231030 ; python_version >= '3.10'")
        else:
            out.append(ln)
    new = _insert_header("\n".join(out) + "\n", f"{MARKER} — auto-patched + OLMA\n")
    backup(p, dry_run=dry_run)
    write(p, new, dry_run=dry_run)
    print(f"  [fixed] {p.name}")
    return True


# =========================================================================== #
# ПРИМЕНЕНИЕ
# =========================================================================== #

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


def apply_all(root: Path, *, dry_run: bool, force: bool) -> int:
    print(f"Корень проекта: {root}")
    changed = 0

    # 1. OLMA-модуль
    if create_olma_module(root, dry_run=dry_run):
        changed += 1

    # 2. qt_compat.py
    if create_qt_compat(root, dry_run=dry_run):
        changed += 1

    # 3. Текстовые патчи
    text_patches = [
        ("mountfile/partition.py", patch_partition),
        ("mountfile/fat32.py", patch_fat32),
        ("mountfile/vfs.py", patch_vfs),
        ("mountfile/updater.py", patch_updater),
        ("main.py", patch_main),
    ]
    for rel, fn in text_patches:
        p = root / rel
        if patch_file(p, dry_run=dry_run, force=force, transform=fn):
            changed += 1

    # 4. requirements.txt
    try:
        if patch_requirements(root, dry_run=dry_run):
            changed += 1
    except Exception as e:
        print(f"  [err] requirements: {e}")

    print(f"\nИтого изменений: {changed}")
    return changed


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
    print(f"Откат из .bak в: {root}")
    n = 0
    for bak in root.rglob("*" + BAK_SUFFIX):
        orig = Path(str(bak)[:-len(BAK_SUFFIX)])
        try:
            shutil.copy2(bak, orig)
            print(f"  [reverted] {orig.relative_to(root)}")
            n += 1
        except Exception as e:
            print(f"  [err] {bak}: {e}")
    print(f"  Восстановлено: {n}")
    return n


def run_smoke(root: Path) -> int:
    print("Smoke-тест (OLMA)...")
    tests = [
        ["python", "-c", "import mountfile.olma as o; "
                         "b=o.OlmaBuffer(b'\\x55\\xAA'+b'\\x00'*510); "
                         "print('OLMA OK', len(b))"],
        ["python", "main.py", "--diag"],
        ["python", "-m", "mountfile", "--help"],
    ]
    rc = 0
    for cmd in tests:
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True,
                               text=True, timeout=60)
            ok = r.returncode == 0
            print(f"  [{'ok' if ok else 'warn'}] {' '.join(cmd)} -> RC={r.returncode}")
            if not ok and rc == 0:
                rc = r.returncode
                if r.stderr:
                    print("    stderr:", r.stderr.strip().splitlines()[-1])
        except FileNotFoundError:
            print(f"  [skip] python не найден")
        except subprocess.TimeoutExpired:
            print(f"  [timeout] {' '.join(cmd)}")
            rc = 1
    return rc


# =========================================================================== #
# CLI
# =========================================================================== #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Mount File — OLMA-патч-скрипт.",
    )
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else find_project_root(Path.cwd())

    if args.revert:
        return 0 if revert_all(root) >= 0 else 1
    if args.check:
        return 1 if check_all(root) > 0 else 0

    apply_all(root, dry_run=args.dry_run, force=args.force)
    if args.dry_run:
        return 0

    bad = check_all(root)
    if bad > 0:
        print("\nВНИМАНИЕ: синтаксические ошибки. Откат: --revert")
        return 1

    if args.smoke:
        rc = run_smoke(root)
        if rc != 0:
            return rc

    print("\nГотово. Резервные копии: *.bak (откат: --revert).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
