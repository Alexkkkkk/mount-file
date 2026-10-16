#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mountfile_fix.py — единый патч-скрипт для проекта Mount File.

Что делает:
  1. Находит корень проекта (каталог с mountfile/ и main.py).
  2. Делает резервные копии (*.bak) всех изменяемых файлов.
  3. Применяет набор исправлений (идемпотентно, маркер MOUNTFILE_FIX).
  4. Проверяет синтаксис через py_compile.
  5. Опционально запускает smoke-тест.

Запуск:
  python mountfile_fix.py                # применить
  python mountfile_fix.py --dry-run      # показать план
  python mountfile_fix.py --revert       # откат из .bak
  python mountfile_fix.py --check        # только проверка синтаксиса
  python mountfile_fix.py --smoke        # применить + smoke-тест

Лицензия: MIT. Используйте на свой риск. Сделайте git-коммит перед запуском.
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
import textwrap
from pathlib import Path

MARKER = "# MOUNTFILE_FIX"
BAK_SUFFIX = ".bak"


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def find_project_root(start: Path) -> Path:
    """Ищет корень проекта вверх по дереву."""
    cur = start.resolve()
    for _ in range(10):
        if (cur / "main.py").exists() and (cur / "mountfile").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    # fallback: текущий каталог
    return start.resolve()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write(path: Path, text: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] изменил бы {path}")
        return
    path.write_text(text, encoding="utf-8")


def backup(path: Path, *, dry_run: bool) -> None:
    if not path.exists():
        return
    bak = path.with_suffix(path.suffix + BAK_SUFFIX)
    if bak.exists():
        return  # уже есть — не перезаписываем
    if dry_run:
        print(f"  [dry-run] создал бы бэкап {bak}")
        return
    shutil.copy2(path, bak)


def is_patched(text: str) -> bool:
    return MARKER in text


def patch_file(
    path: Path,
    *,
    dry_run: bool,
    force: bool = False,
    transform=None,
    header: str | None = None,
) -> bool:
    """
    Применяет transform(text) -> text к файлу.
    Возвращает True, если файл был изменён.
    """
    if not path.exists():
        print(f"  [skip] нет файла: {path}")
        return False

    original = read(path)
    if is_patched(original) and not force:
        print(f"  [ok] уже пропатчен: {path.name}")
        return False

    if transform is None:
        return False

    try:
        new_text = transform(original)
    except Exception as e:  # noqa: BLE001
        print(f"  [err] transform упал на {path.name}: {e}")
        return False

    if new_text is None or new_text == original:
        print(f"  [skip] изменений нет: {path.name}")
        return False

    # Добавляем маркер в начало (после shebang/кодировки)
    if header is None:
        header = f"{MARKER} — auto-patched\n"
    new_text = _insert_header(new_text, header)

    backup(path, dry_run=dry_run)
    write(path, new_text, dry_run=dry_run)
    print(f"  [fixed] {path.name}")
    return True


def _insert_header(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    insert_at = 0
    for i, ln in enumerate(lines[:3]):
        if ln.startswith("#!") or "coding" in ln or "encoding" in ln:
            insert_at = i + 1
    lines.insert(insert_at, header)
    return "".join(lines)


def syntax_ok(path: Path) -> bool:
    try:
        src = read(path)
        ast.parse(src, filename=str(path))
        return True
    except SyntaxError as e:
        print(f"  [syntax-err] {path}: {e}")
        return False


def compile_file(path: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory() as td:
            py_compile.compile(str(path), cfile=os.path.join(td, "out.pyc"), doraise=True)
        return True
    except py_compile.PyCompileError as e:
        print(f"  [compile-err] {path}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Шаблоны новых файлов
# --------------------------------------------------------------------------- #

QT_COMPAT_TEMPLATE = '''\
"""qt_compat.py — унифицированный слой совместимости PyQt5/PyQt6.

Приоритет: PyQt6 -> PyQt5. Экспортирует единые имена:
    QtCore, QtGui, QtWidgets, Qt, pyqtSignal, pyqtSlot, QKeySequence,
    QT_VERSION, IS_QT6, is_qt_available()
"""

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
        try:
            from PyQt5.QtWidgets import QAction  # type: ignore
        except Exception:  # noqa: BLE001
            from PyQt5.QtWidgets import QAction  # type: ignore
        IS_QT6 = False
        QT_VERSION = "PyQt5"
    except Exception as e5:  # noqa: BLE001
        _IMPORT_ERROR = f"PyQt6: {e6!r}; PyQt5: {e5!r}"


def is_qt_available() -> bool:
    return _IMPORT_ERROR is None


def require_qt():
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

FAT32_GUARDS = {
    "MAX_CLUSTERS": "0x0FFFFFF5",  # FAT32 теоретический максимум
}


# --------------------------------------------------------------------------- #
# Патчи конкретных файлов
# --------------------------------------------------------------------------- #

def patch_qt_compat(root: Path, *, dry_run: bool) -> bool:
    """Полностью заменяет qt_compat.py на каноническую версию."""
    path = root / "mountfile" / "qt_compat.py"
    if dry_run:
        print(f"  [dry-run] перезапишу {path}")
        return True
    backup(path, dry_run=False)
    path.write_text(QT_COMPAT_TEMPLATE, encoding="utf-8")
    print(f"  [fixed] {path.name} (канонический слой Qt)")
    return True


def patch_partition(text: str) -> str | None:
    """Добавляет валидацию MBR/GPT и защиту от битых таблиц."""
    if "def _validate_mbr" in text:
        return None

    helper = '''

# --- MOUNTFILE_FIX: валидация MBR/GPT -------------------------------------

_MAX_PARTITIONS = 128
_SECTOR = 512


def _validate_mbr(buf: bytes) -> bool:
    """Проверяет сигнатуру 0x55AA и границы записей MBR."""
    if len(buf) < _SECTOR:
        return False
    if buf[510:512] != b"\\x55\\xAA":
        return False
    for i in range(4):
        off = 446 + i * 16
        entry = buf[off:off + 16]
        if len(entry) < 16:
            return False
        # Проверка: смещение + размер не выходят за 4G секторов
        start = int.from_bytes(entry[8:12], "little")
        size = int.from_bytes(entry[12:16], "little")
        if start == 0 and size == 0:
            continue  # пустая запись
        if size == 0:
            continue
        if start + size > (1 << 32) - 1:
            return False
    return True


def _validate_gpt(buf: bytes) -> bool:
    """Проверяет сигнатуру GPT 'EFI PART'."""
    if len(buf) < 1024:
        return False
    return buf[512:520] == b"EFI PART"


def _clamp_partition(start: int, size: int, total_sectors: int) -> tuple[int, int]:
    """Обрезает раздел по границам образа, чтобы не читать за пределами."""
    if start < 0:
        start = 0
    if start > total_sectors:
        return 0, 0
    end = start + size
    if end > total_sectors:
        size = total_sectors - start
    if size < 0:
        size = 0
    return start, size

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    # Ищем конец импортов
    lines = text.splitlines(keepends=True)
    last_import = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from ") or s.startswith("#") or not s:
            last_import = i
        else:
            break
    insert_at = last_import + 1
    lines.insert(insert_at, helper)
    return "".join(lines)


def patch_fat32(text: str) -> str | None:
    """Добавляет проверки границ и безопасное освобождение кластеров."""
    if "def _check_cluster_range" in text:
        return None

    guards = '''

# --- MOUNTFILE_FIX: защита FAT32 -------------------------------------------

FAT32_MAX_CLUSTER = 0x0FFFFFF5
FAT32_EOC_MIN = 0x0FFFFFF8
FAT32_BAD = 0x0FFFFFF7


def _check_cluster_range(cluster: int, max_cluster: int) -> bool:
    """Проверяет, что кластер в допустимом диапазоне (2..max_cluster-1)."""
    return 2 <= cluster < max_cluster


def _is_eoc(value: int) -> bool:
    return value >= FAT32_EOC_MIN


def _is_bad(value: int) -> bool:
    return value == FAT32_BAD


def _safe_free_chain(fat_get, fat_set, start: int, max_cluster: int,
                     max_iters: int = 1_000_000) -> int:
    """
    Безопасно освобождает цепочку кластеров.
    Защита от зацикливания и выхода за границы.
    Возвращает число освобождённых кластеров.
    """
    freed = 0
    seen: set[int] = set()
    cur = start
    while _check_cluster_range(cur, max_cluster) and cur not in seen and max_iters > 0:
        seen.add(cur)
        nxt = fat_get(cur)
        fat_set(cur, 0)
        freed += 1
        max_iters -= 1
        if _is_eoc(nxt) or _is_bad(nxt) or nxt == 0:
            break
        cur = nxt
    return freed

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    lines = text.splitlines(keepends=True)
    last_import = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from ") or s.startswith("#") or not s:
            last_import = i
        else:
            break
    insert_at = last_import + 1
    lines.insert(insert_at, guards)
    return "".join(lines)


def patch_vfs(text: str) -> str | None:
    """Добавляет безопасное чтение с проверкой границ."""
    if "def safe_read" in text:
        return None

    helper = '''

# --- MOUNTFILE_FIX: безопасное чтение -------------------------------------

def safe_read(fh, offset: int, size: int, *, chunk: int = 1 << 20) -> bytes:
    """
    Читает не более size байт начиная с offset.
    Защита от отрицательных значений и очень больших size.
    """
    if size <= 0:
        return b""
    if offset < 0:
        offset = 0
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


def safe_read_exact(fh, offset: int, size: int) -> bytes | None:
    """Как safe_read, но возвращает None, если прочитано меньше size."""
    data = safe_read(fh, offset, size)
    if len(data) != size:
        return None
    return data

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    lines = text.splitlines(keepends=True)
    last_import = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from ") or s.startswith("#") or not s:
            last_import = i
        else:
            break
    insert_at = last_import + 1
    lines.insert(insert_at, helper)
    return "".join(lines)


def patch_updater(text: str) -> str | None:
    """Добавляет безопасное скачивание и проверку SHA-256."""
    if "def _safe_download" in text:
        return None

    helper = '''

# --- MOUNTFILE_FIX: безопасное обновление ---------------------------------

import hashlib as _hashlib
import tempfile as _tempfile
import urllib.request as _urlreq


def _safe_download(url: str, *, timeout: float = 30.0,
                   max_size: int = 512 * 1024 * 1024) -> bytes:
    """
    Скачивает URL с таймаутом и ограничением размера.
    Бросает RuntimeError при превышении лимита.
    """
    req = _urlreq.Request(url, headers={"User-Agent": "MountFile-Updater/1.0"})
    with _urlreq.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        length = resp.headers.get("Content-Length")
        if length and int(length) > max_size:
            raise RuntimeError(f"Файл слишком большой: {length} > {max_size}")
        data = bytearray()
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_size:
                raise RuntimeError("Превышен лимит размера при скачивании")
    return bytes(data)


def _verify_sha256(data: bytes, expected_hex: str) -> bool:
    """Проверяет SHA-256. expected_hex может быть пустым — тогда True."""
    if not expected_hex:
        return True
    h = _hashlib.sha256(data).hexdigest().lower()
    return h == expected_hex.strip().lower()


def _atomic_write(path, data: bytes) -> None:
    """Атомарная запись: temp -> rename."""
    import os as _os
    d = _os.path.dirname(_os.path.abspath(path)) or "."
    fd, tmp = _tempfile.mkstemp(dir=d, prefix=".mf-", suffix=".tmp")
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except Exception:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    lines = text.splitlines(keepends=True)
    last_import = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from ") or s.startswith("#") or not s:
            last_import = i
        else:
            break
    insert_at = last_import + 1
    lines.insert(insert_at, helper)
    return "".join(lines)


def patch_main(text: str) -> str | None:
    """Добавляет корректную обработку --diag и защиту QApplication."""
    if "def _mf_diag" in text:
        return None

    helper = '''

# --- MOUNTFILE_FIX: диагностика и защита запуска ---------------------------

def _mf_diag() -> int:
    """Печатает диагностику окружения. Код возврата 0."""
    import platform
    import sys as _sys
    try:
        from mountfile import __version__ as _v  # type: ignore
    except Exception:  # noqa: BLE001
        _v = "unknown"
    print(f"MountFile diag")
    print(f"  python:   {_sys.version.split()[0]}")
    print(f"  platform: {platform.platform()}")
    print(f"  version:  {_v}")
    try:
        from mountfile import qt_compat  # type: ignore
        print(f"  qt:       {qt_compat.QT_VERSION}")
    except Exception as e:  # noqa: BLE001
        print(f"  qt:       NOT AVAILABLE ({e!r})")
    try:
        import pytsk3  # type: ignore
        print(f"  pytsk3:   {getattr(pytsk3, '__version__', 'ok')}")
    except Exception:  # noqa: BLE001
        print(f"  pytsk3:   NOT AVAILABLE")
    return 0


def _mf_ensure_app():
    """Гарантирует существование QApplication."""
    from mountfile import qt_compat  # type: ignore
    qt_compat.require_qt()
    app = qt_compat.QtWidgets.QApplication.instance()
    if app is None:
        app = qt_compat.QtWidgets.QApplication([])
    return app

# --- /MOUNTFILE_FIX --------------------------------------------------------
'''
    lines = text.splitlines(keepends=True)
    last_import = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("import ") or s.startswith("from ") or s.startswith("#") or not s:
            last_import = i
        else:
            break
    insert_at = last_import + 1
    lines.insert(insert_at, helper)

    text = "".join(lines)

    # Обработка --diag в main
    if "if __name__" in text and "--diag" not in text:
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
    path = root / "requirements.txt"
    if not path.exists():
        return False
    text = read(path)
    if MARKER in text:
        print(f"  [ok] уже пропатчен: {path.name}")
        return False

    # Заменяем pytsk3 на совместимую версию с Python 3.10+
    new_lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("pytsk3"):
            new_lines.append("pytsk3>=20231030 ; python_version >= '3.10'")
        else:
            new_lines.append(ln)
    new_text = "\n".join(new_lines) + "\n"
    new_text = _insert_header(new_text, f"{MARKER} — auto-patched\n")

    backup(path, dry_run=dry_run)
    write(path, new_text, dry_run=dry_run)
    print(f"  [fixed] {path.name}")
    return True


# --------------------------------------------------------------------------- #
# Список патчей
# --------------------------------------------------------------------------- #

def apply_all(root: Path, *, dry_run: bool, force: bool) -> int:
    print(f"Корень проекта: {root}")
    changed = 0

    # 1. qt_compat.py — канонический слой
    try:
        if patch_qt_compat(root, dry_run=dry_run):
            changed += 1
    except Exception as e:  # noqa: BLE001
        print(f"  [err] qt_compat: {e}")

    # 2. Текстовые патчи модулей
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

    # 3. requirements.txt
    try:
        if patch_requirements(root, dry_run=dry_run):
            changed += 1
    except Exception as e:  # noqa: BLE001
        print(f"  [err] requirements: {e}")

    print(f"\\nИтого изменений: {changed}")
    return changed


def check_all(root: Path) -> int:
    print(f"Проверка синтаксиса в: {root}")
    bad = 0
    for p in root.rglob("*.py"):
        if p.name == Path(__file__).name:
            continue
        if not syntax_ok(p):
            bad += 1
        elif not compile_file(p):
            bad += 1
    if bad == 0:
        print("  Все файлы синтаксически корректны.")
    else:
        print(f"  Проблемных файлов: {bad}")
    return bad


def revert_all(root: Path) -> int:
    print(f"Откат из .bak в: {root}")
    n = 0
    for bak in root.rglob(f"*{BAK_SUFFIX}"):
        orig = bak.with_suffix("")  # убираем .bak
        if not orig.exists():
            orig = Path(str(bak)[:-len(BAK_SUFFIX)])
        try:
            shutil.copy2(bak, orig)
            print(f"  [reverted] {orig.relative_to(root)}")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [err] {bak}: {e}")
    print(f"  Восстановлено файлов: {n}")
    return n


def run_smoke(root: Path) -> int:
    print("Smoke-тест...")
    tests = [
        ["python", "main.py", "--diag"],
        ["python", "-m", "mountfile", "--help"],
    ]
    rc = 0
    for cmd in tests:
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=60)
            status = "OK" if r.returncode == 0 else f"RC={r.returncode}"
            print(f"  [{'ok' if r.returncode == 0 else 'warn'}] {' '.join(cmd)} -> {status}")
            if r.returncode != 0 and rc == 0:
                rc = r.returncode
        except FileNotFoundError:
            print(f"  [skip] python не найден для {' '.join(cmd)}")
        except subprocess.TimeoutExpired:
            print(f"  [timeout] {' '.join(cmd)}")
            rc = 1
    return rc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Mount File — единый патч-скрипт исправлений.",
    )
    ap.add_argument("--root", default=None, help="корень проекта (по умолчанию — авто)")
    ap.add_argument("--dry-run", action="store_true", help="не изменять файлы")
    ap.add_argument("--force", action="store_true", help="перепатчить даже помеченные")
    ap.add_argument("--check", action="store_true", help="только проверить синтаксис")
    ap.add_argument("--revert", action="store_true", help="откатить из *.bak")
    ap.add_argument("--smoke", action="store_true", help="запустить smoke-тест после патча")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else find_project_root(Path.cwd())

    if args.revert:
        return 0 if revert_all(root) >= 0 else 1

    if args.check:
        return 1 if check_all(root) > 0 else 0

    # Применяем патчи
    apply_all(root, dry_run=args.dry_run, force=args.force)

    if args.dry_run:
        return 0

    # Проверка синтаксиса
    bad = check_all(root)
    if bad > 0:
        print("\\nВНИМАНИЕ: есть синтаксические ошибки. Откатите через --revert.")
        return 1

    if args.smoke:
        rc = run_smoke(root)
        if rc != 0:
            print("\\nSmoke-тест завершился с ошибкой.")
            return rc

    print("\\nГотово. Резервные копии: *.bak (откат: --revert).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
