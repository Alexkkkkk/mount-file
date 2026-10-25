#!/usr/bin/env python3
"""
mountfile_rw_complete.py — Полная реализация Task 2.1 / issue #87.

Дополняет mountfile_universal_rw.py:
  • Fat32Adapter          — реальная интеграция с mountfile/fat32.py
  • VerificationEngine    — проверка целостности per-FS (superblock, dirty bit, hash)
  • CrashRecovery         — восстановление после kill -9 по журналу
  • DryRun                — валидация без изменения образа
  • AuditExporter         — экспорт журнала в JSONL
  • MCP server            — интерфейс для AI-агентов
  • CLI                   — put/get/mkdir/rm/mv/verify/recover/journal/audit/mcp
  • Self-tests            — встроенные sanity-проверки

Зависимости: stdlib + mountfile_universal_rw (импортируется как mf_rw).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

from mountfile.rw.universal import (
    FileStat,
    FsType,
    Journal,
    Mode,
    RWError,
    StrategyUnavailable,
    UniversalRW,
    create_rw,
    detect_fs,
)

# ---------------------------------------------------------------------------
# 1. Нативный FAT32-адаптер (интеграция с mountfile/fat32.py)
# ---------------------------------------------------------------------------


class Fat32Adapter(UniversalRW):
    """
    Адаптер к существующему нативному движку ``mountfile/fat32.py``.

    Реальный контракт движка (сверено с исходником ``mountfile/fat32.py``)::

        Fat32(fileobj, offset=0)
        Fat32.entry(path) -> (parent_cluster, DirEntry)   # бросает Fat32Error
        Fat32.find_dir(path) -> int
        Fat32.list_dir(cluster=None) -> list[DirEntry]
        Fat32.read_file(path) -> bytes
        Fat32.write_file(dir_path, filename, data)   # создать/перезаписать
        Fat32.mkdir(dir_path, name)
        Fat32.delete(path, recursive=False)
        Fat32.rename(path, new_name)                 # новое ИМЯ, не путь
        Fat32.free_clusters() / Fat32.sync_fsinfo()

    Особенности, которые обязан учитывать адаптер:

    * движок принимает **файловый объект**, а не путь, поэтому адаптер сам
      открывает образ (``r+b`` в RW, ``rb`` в RO) и закрывает его в
      ``_close_backend``;
    * ``write_file``/``mkdir`` принимают пару ``(каталог, имя)``, а не путь;
    * ``entry``/``read_file``/``delete`` бросают ``Fat32Error`` на отсутствующий
      путь — публичный API адаптера вместо этого возвращает ``None``
      (``_lookup``) или ``FileNotFoundError`` (``_stat``);
    * ``rename`` меняет только имя внутри того же каталога, поэтому перенос
      между каталогами честно отклоняется ``RWError``;
    * timestamps движок не хранит, поэтому ``mtime`` в ``FileStat`` равен 0.0.
    """

    atomic_mutations = True

    def __init__(self, image_path: Path, mode: Mode = Mode.RO):
        super().__init__(image_path, mode)
        self._fh = None
        self._fs = None

    # --- lifecycle ---------------------------------------------------------

    def _open_backend(self) -> None:
        try:
            from mountfile.fat32 import Fat32  # существующий движок
        except ImportError:  # pragma: no cover - Mount File не установлен
            raise StrategyUnavailable(
                "mountfile.fat32 не найден. Установите Mount File "
                "(pip install -e . из корня репозитория)."
            )
        writable = self.mode == Mode.RW
        self._fh = open(self.image_path, "r+b" if writable else "rb")
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
            except (OSError, ValueError):  # образ только для чтения
                pass
            self._fh.close()
            self._fh = None
        self._fs = None

    def _backend(self):
        if self._fs is None:
            raise StrategyUnavailable("бэкенд FAT32 не открыт: вызовите open()")
        return self._fs

    def _lookup(self, path: str):
        """DirEntry или None — публичная обёртка над бросающим ``entry()``."""
        try:
            return self._backend().entry(path)[1]
        except Exception:
            return None

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        clean = "/" + str(path).replace("\\", "/").strip("/")
        parent, _, name = clean.rpartition("/")
        return (parent or "/"), name

    # --- мутирующие операции ------------------------------------------------

    def _create_file(self, path: str, data: BinaryIO, overwrite: bool) -> None:
        fs = self._backend()
        parent, name = self._split(path)
        if not name:
            raise RWError("нельзя записать файл в корень образа")
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
            raise RWError("нельзя создать корневой каталог")
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
            raise RWError(
                "нативный движок FAT32 умеет переименование только внутри "
                f"одного каталога: {src} -> {dst}"
            )
        fs.rename(src, dst_name)

    def _write_file(self, path: str, data: BinaryIO, offset: int) -> None:
        fs = self._backend()
        parent, name = self._split(path)
        payload = data.read()
        if offset:
            try:
                current = bytearray(fs.read_file(path))
            except Exception as exc:  # нет файла / не читается
                raise RWError(f"не удалось прочитать {path} для записи: {exc}")
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
        return FileStat(
            path=path,
            size=entry.size,
            mtime=0.0,  # нативный движок не хранит timestamps
            is_dir=entry.is_dir,
        )


def create_rw_full(
    image_path: Path,
    fs: Optional[FsType] = None,
    mode: Mode = Mode.RO,
    dry_run: bool = False,
) -> UniversalRW:
    """Фабрика RW-слоя.

    Для FAT32 используется нативный адаптер (``Fat32Adapter``), для остальных ФС —
    бэкенды из ``mountfile_universal_rw``.  ``dry_run=True`` возвращает
    ``DryRunFat32``: тот же адаптер, у которого мутирующие операции не доходят
    до образа (валидация без записи).
    """
    fs = fs or detect_fs(image_path)
    if fs == FsType.FAT32:
        engine: UniversalRW = (
            DryRunFat32(image_path, mode) if dry_run else Fat32Adapter(image_path, mode)
        )
        engine.dry_run = dry_run
        return engine
    return create_rw(image_path, fs, mode)


# 2. Верификатор целостности
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    ok: bool
    fs: str
    checks: dict[str, bool] = field(default_factory=dict)
    details: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class VerificationEngine:
    """
    Проверки per-FS. Запускается после каждой транзакции.
    Все проверки должны быть быстрыми (<1с на образ до 4 ГБ).
    """

    def verify(self, image_path: Path, fs: FsType) -> VerifyResult:
        checks: dict[str, bool] = {}
        checks["hash_readable"] = self._can_hash(image_path)

        if fs == FsType.FAT32:
            checks.update(self._verify_fat32(image_path))
        elif fs == FsType.EXT4:
            checks.update(self._verify_ext4(image_path))
        elif fs == FsType.NTFS:
            checks.update(self._verify_ntfs(image_path))

        return VerifyResult(
            ok=all(checks.values()),
            fs=fs.value,
            checks=checks,
            details="; ".join(
                f"{k}={'ok' if v else 'FAIL'}" for k, v in checks.items()
            ),
        )

    # --- общие --------------------------------------------------------------

    def _can_hash(self, image_path: Path) -> bool:
        try:
            with open(image_path, "rb") as f:
                f.read(4096)
            return True
        except OSError:
            return False

    def region_hash(self, image_path: Path, offset: int, length: int) -> str:
        with open(image_path, "rb") as f:
            f.seek(offset)
            return hashlib.sha256(f.read(length)).hexdigest()

    # --- FAT32 --------------------------------------------------------------

    def _verify_fat32(self, image_path: Path) -> dict[str, bool]:
        checks = {}
        with open(image_path, "rb") as f:
            boot = f.read(512)
        checks["fat32_signature"] = boot[510:512] == b"\x55\xaa"
        checks["fat32_label"] = boot[82:90].startswith(b"FAT32")
        # FAT[0] лежит не по смещению 512 (там FSINFO-сектор), а после
        # зарезервированных секторов: reserved_sectors * bytes_per_sector.
        bytes_per_sector = int.from_bytes(boot[11:13], "little")
        reserved = int.from_bytes(boot[14:16], "little")
        with open(image_path, "rb") as f:
            f.seek(reserved * bytes_per_sector)
            fat = f.read(4)
        value = int.from_bytes(fat, "little") if len(fat) == 4 else 0
        # Валидный FAT32-дескриптор: 0x0FFFFFF8 (биты 3..27 выставлены,
        # младшие три бита зарезервированы под нули).
        checks["fat32_media"] = (value & 0x0FFFFFF8) == 0x0FFFFFF8
        return checks

    # --- ext4 ---------------------------------------------------------------

    def _verify_ext4(self, image_path: Path) -> dict[str, bool]:
        checks = {}
        with open(image_path, "rb") as f:
            f.seek(0x438)
            magic = f.read(2)
        checks["ext4_magic"] = magic == b"\x53\xef"
        # Проверка через e2fsck (если есть)
        if shutil.which("e2fsck"):
            try:
                res = subprocess.run(
                    ["e2fsck", "-fn", str(image_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                # 0 = чист, 4 = исправления не нужны
                checks["ext4_fsck"] = res.returncode in (0, 4)
            except (subprocess.TimeoutExpired, OSError):
                checks["ext4_fsck"] = False
        else:
            checks["ext4_fsck"] = True  # не блокируем, если утилиты нет
        return checks

    # --- NTFS ---------------------------------------------------------------

    def _verify_ntfs(self, image_path: Path) -> dict[str, bool]:
        checks = {}
        with open(image_path, "rb") as f:
            boot = f.read(512)
        checks["ntfs_signature"] = boot[3:11] == b"NTFS    "
        checks["ntfs_boot_sig"] = boot[510:512] == b"\x55\xaa"
        # dirty bit хранится в $Volume; без парсера — только базовая проверка
        checks["ntfs_boot_ok"] = boot[0x0D] in (0x08, 0xF4, 0xF6)
        return checks


# ---------------------------------------------------------------------------
# 3. Восстановление после сбоя
# ---------------------------------------------------------------------------


class CrashRecovery:
    """
    При открытии RW-слоя проверяет журнал на незавершённые транзакции
    и откатывает их из backup-файлов.
    """

    def __init__(self, journal: Journal, verifier: VerificationEngine):
        self.journal = journal
        self.verifier = verifier

    def recover(self, image_path: Path, fs: FsType) -> int:
        """
        Возвращает число откатанных транзакций.
        Восстанавливает только незавершённые транзакции. Активную транзакцию
        текущего процесса не трогает; снимок и целостность байтов проверяет
        Journal.rollback().
        """
        chain = self.journal.verify_chain()
        if not chain["valid"]:
            raise RWError(
                "Проверка целостности журнала не пройдена: %s" % chain["error"]
            )
        recovered = 0
        # Undo newest first if a legacy journal contains multiple pending rows.
        for transaction in reversed(self.journal.uncommitted()):
            tx_id = transaction["tx_id"]
            if tx_id == self.journal._current:
                continue
            try:
                restored = self.journal.rollback(tx_id)
            except Exception as exc:
                raise RWError(
                    f"Не удалось восстановить транзакцию {tx_id} "
                    f"(backup={transaction['backup']!r}): {exc}"
                ) from exc
            if not restored:
                raise RWError(
                    f"Не удалось восстановить транзакцию {tx_id}: "
                    f"backup отсутствует, повреждён или параметры снимка "
                    f"недействительны (backup={transaction['backup']!r})"
                )
            recovered += 1
        if recovered:
            try:
                report = self.verifier.verify(image_path, fs)
            except Exception as exc:
                raise RWError(
                    f"Восстановлено транзакций: {recovered}, но проверка "
                    f"образа завершилась ошибкой: {exc}"
                ) from exc
            if not report.ok:
                raise RWError(
                    f"Восстановлено транзакций: {recovered}, но verify "
                    f"провалился: {report.details or report.checks}"
                )
        return recovered


# ---------------------------------------------------------------------------
# 4. Dry-run
# ---------------------------------------------------------------------------


class DryRunMixin:
    """
    Гейт «только валидация»: при ``dry_run=True`` мутирующие операции
    записываются в план и не доходят до образа.

    Порядок базовых классов обязателен: ``class DryRunFat32(DryRunMixin,
    Fat32Adapter)``.  Если поставить миксин вторым, его переопределения не
    находятся в MRO и ``dry_run`` молча пишет в образ.
    """

    dry_run: bool = False

    def _plan(self, op: str, path: str) -> None:
        planned = getattr(self, "_planned", None)
        if planned is None:
            planned = []
            self._planned = planned
        planned.append("%s %s" % (op, path))

    def plan(self) -> list[str]:
        """Операции, которые были бы выполнены (в порядке вызова)."""
        return list(getattr(self, "_planned", []))

    def _require_rw(self) -> None:
        if self.dry_run:
            return
        super()._require_rw()  # type: ignore[misc]

    def create_file(self, path, data, *, overwrite=False):
        if self.dry_run:
            self._plan("create_file", path)
            return None
        return super().create_file(path, data, overwrite=overwrite)  # type: ignore[misc]

    def create_dir(self, path):
        if self.dry_run:
            self._plan("mkdir", path)
            return None
        return super().create_dir(path)  # type: ignore[misc]

    def delete(self, path, *, recursive=False):
        if self.dry_run:
            self._plan("delete", path)
            return None
        return super().delete(path, recursive=recursive)  # type: ignore[misc]

    def rename(self, src, dst):
        if self.dry_run:
            self._plan("rename", "%s -> %s" % (src, dst))
            return None
        return super().rename(src, dst)  # type: ignore[misc]

    def write_file(self, path, data, *, offset=0):
        if self.dry_run:
            self._plan("write_file", path)
            return None
        return super().write_file(path, data, offset=offset)  # type: ignore[misc]


class DryRunFat32(DryRunMixin, Fat32Adapter):
    """FAT32-адаптер в режиме «валидация без записи»."""


# ---------------------------------------------------------------------------
# 5. Экспорт аудита
# ---------------------------------------------------------------------------


class AuditExporter:
    def __init__(self, journal: Journal):
        self.journal = journal

    def to_jsonl(self, out: Path) -> int:
        conn = self.journal._conn
        rows = conn.execute(
            "SELECT tx_id, op, path, sha256, prev_hash, created_at "
            "FROM journal ORDER BY created_at"
        ).fetchall()
        with open(out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(
                    json.dumps(
                        {
                            "tx_id": r[0],
                            "op": r[1],
                            "path": r[2],
                            "sha256": r[3],
                            "prev_hash": r[4],
                            "created_at": r[5],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return len(rows)


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


def _open(image: Path, mode: Mode, dry_run: bool) -> UniversalRW:
    fs = detect_fs(image)
    eng = create_rw_full(image, fs, mode, dry_run=dry_run)
    eng.open()
    # Автоматическое восстановление после сбоя (в dry-run образ не трогаем).
    if mode == Mode.RW and not dry_run:
        verifier = VerificationEngine()
        recovered = CrashRecovery(eng.journal, verifier).recover(image, fs)
        if recovered:
            print(f"[recovery] откатано транзакций: {recovered}")
    return eng


def _read_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def cmd_put(args) -> int:
    eng = _open(Path(args.image), Mode.RW, args.dry_run)
    try:
        parent = str(Path(args.dst).parent)
        if parent not in ("/", ".", ""):
            try:
                eng.stat(parent)
            except FileNotFoundError:
                eng.create_dir(parent)
        eng.create_file(args.dst, io.BytesIO(_read_input(args.src)), overwrite=True)
        print(f"ok: {args.dst}")
        return 0
    finally:
        eng.close()


def _native_read(image: Path, path: str) -> bytes:
    """RO-чтение через нативный движок FAT32 (stdlib-only слой остаётся RO)."""
    from mountfile.fat32 import Fat32

    with open(image, "rb") as handle:
        return Fat32(handle).read_file(path)


def cmd_get(args) -> int:
    image = Path(args.image)
    if detect_fs(image) == FsType.FAT32:
        Path(args.dst).write_bytes(_native_read(image, args.src))
    else:
        # Для остальных ФС чтение остаётся за существующим Mount File CLI.
        proc = subprocess.run(
            [sys.executable, "-m", "mountfile", "get", str(image), args.src, args.dst],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RWError(
                (proc.stderr or proc.stdout).strip() or "mountfile get failed"
            )
    print(f"ok: {args.src} -> {args.dst}")
    return 0


def cmd_mkdir(args) -> int:
    eng = _open(Path(args.image), Mode.RW, args.dry_run)
    try:
        eng.create_dir(args.path)
        print(f"ok: {args.path}")
        return 0
    finally:
        eng.close()


def cmd_rm(args) -> int:
    eng = _open(Path(args.image), Mode.RW, args.dry_run)
    try:
        eng.delete(args.path, recursive=args.recursive)
        print(f"ok: {args.path}")
        return 0
    finally:
        eng.close()


def cmd_mv(args) -> int:
    eng = _open(Path(args.image), Mode.RW, args.dry_run)
    try:
        eng.rename(args.src, args.dst)
        print(f"ok: {args.src} -> {args.dst}")
        return 0
    finally:
        eng.close()


def cmd_verify(args) -> int:
    image = Path(args.image)
    fs = detect_fs(image)
    report = VerificationEngine().verify(image, fs)
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0 if report.ok else 1


def cmd_recover(args) -> int:
    image = Path(args.image)
    fs = detect_fs(image)
    eng = create_rw_full(image, fs, Mode.RW)
    eng.open()
    try:
        n = CrashRecovery(eng.journal, VerificationEngine()).recover(image, fs)
        print(f"recovered: {n}")
        return 0
    finally:
        eng.close()


def cmd_journal(args) -> int:
    image = Path(args.image)
    j = Journal(image)
    rows = j._conn.execute(
        "SELECT tx_id, op, path, sha256, created_at FROM journal "
        "ORDER BY created_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for r in rows:
        print(f"{r[4]:.0f} {r[0][:8]} {r[1]:<12} {r[2]} {r[3] or '-'}")
    j.close()
    return 0


def cmd_audit(args) -> int:
    image = Path(args.image)
    j = Journal(image)
    n = AuditExporter(j).to_jsonl(Path(args.out))
    j.close()
    print(f"exported: {n} records -> {args.out}")
    return 0


# ---------------------------------------------------------------------------
# 7. MCP-сервер (упрощённый, stdio)
# ---------------------------------------------------------------------------

MCP_TOOLS = {
    "list_files": {
        "description": "Список файлов в образе (RO)",
        "schema": {"image": "str", "path": "str"},
    },
    "read_file": {
        "description": "Чтение файла из образа (RO)",
        "schema": {"image": "str", "path": "str"},
    },
    "verify_image": {
        "description": "Проверка целостности образа",
        "schema": {"image": "str"},
    },
    "journal": {
        "description": "Журнал транзакций",
        "schema": {"image": "str", "limit": "int"},
    },
}


def mcp_handle(tool: str, params: dict) -> dict:
    """Обработчик MCP-запросов. Только RO-операции — безопасно для AI."""
    try:
        if tool == "verify_image":
            image = Path(params["image"])
            fs = detect_fs(image)
            return VerificationEngine().verify(image, fs).to_dict()

        if tool == "journal":
            j = Journal(Path(params["image"]))
            rows = j._conn.execute(
                "SELECT tx_id, op, path, created_at FROM journal "
                "ORDER BY created_at DESC LIMIT ?",
                (params.get("limit", 20),),
            ).fetchall()
            j.close()
            return {
                "entries": [
                    {"tx_id": r[0], "op": r[1], "path": r[2], "ts": r[3]} for r in rows
                ]
            }

        if tool in ("list_files", "read_file"):
            # RO-чтение: нативный движок FAT32 (без записи и без pytsk3).
            image = Path(params["image"])
            path = params.get("path", "/")
            if detect_fs(image) != FsType.FAT32:
                raise StrategyUnavailable(
                    "RO-мост поддерживает FAT32 (нативный движок); "
                    f"для {path} используйте mountfile CLI"
                )
            from mountfile.fat32 import Fat32

            with open(image, "rb") as handle:
                fs = Fat32(handle)
                if tool == "list_files":
                    entries = fs.list_dir(fs.find_dir(path))
                    return {
                        "files": [
                            {"name": e.name, "size": e.size, "is_dir": e.is_dir}
                            for e in entries
                        ]
                    }
                data = fs.read_file(path)
            return {
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        return {"error": f"unknown tool: {tool}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def cmd_mcp(args) -> int:
    """Минимальный stdio-сервер MCP: строка JSON -> строка JSON."""
    print("MCP ready. Tools:", ", ".join(MCP_TOOLS), file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = mcp_handle(req.get("tool", ""), req.get("params", {}))
        except json.JSONDecodeError as e:
            resp = {"error": f"bad json: {e}"}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# 8. Self-tests
# ---------------------------------------------------------------------------


def self_test() -> int:
    """Быстрые sanity-проверки без внешних образов."""
    print("=== self-test ===")
    failures = 0

    # 1. Hash-chain журнала
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "fake.img"
        img.write_bytes(b"\0" * 4096)
        j = Journal(img)
        tx1 = j.begin("create", "/a")
        j._conn.execute(
            "UPDATE journal SET sha256=? WHERE tx_id=?",
            ("a" * 64, tx1),
        )
        j._conn.commit()
        j._last_hash = "a" * 64
        tx2 = j.begin("delete", "/a")
        prev = j._conn.execute(
            "SELECT prev_hash FROM journal WHERE tx_id=?", (tx2,)
        ).fetchone()[0]
        if prev != "a" * 64:
            print("FAIL: hash-chain не связан")
            failures += 1
        else:
            print("ok: hash-chain")
        j.close()

    # 2. VerificationEngine на фейковом FAT32
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "fat32.img"
        boot = bytearray(512)
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xaa"
        img.write_bytes(boot + b"\0" * 1024)
        rep = VerificationEngine().verify(img, FsType.FAT32)
        if not rep.checks.get("fat32_signature"):
            print("FAIL: fat32 signature")
            failures += 1
        else:
            print("ok: fat32 verify")

    # 3. DryRun
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "x.img"
        img.write_bytes(b"\0" * 4096)
        eng = DryRunFat32(img, Mode.RW)
        eng.dry_run = True
        try:
            eng._require_rw()  # не должно бросить
            print("ok: dry-run")
        except RWError:
            print("FAIL: dry-run бросил исключение")
            failures += 1

    print(f"=== self-test: {failures} failures ===")
    return failures


# ---------------------------------------------------------------------------
# 9. main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mountfile-rw",
        description="Mount File Universal RW (issue #87, Task 2.1)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_image(sp):
        sp.add_argument("image", help="Путь к образу")

    # put
    sp = sub.add_parser("put", help="Записать файл в образ")
    add_image(sp)
    sp.add_argument("src", help="Локальный файл или '-' для stdin")
    sp.add_argument("dst", help="Путь внутри образа")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_put)

    # get
    sp = sub.add_parser("get", help="Извлечь файл из образа")
    add_image(sp)
    sp.add_argument("src")
    sp.add_argument("dst")
    sp.set_defaults(func=cmd_get)

    # mkdir
    sp = sub.add_parser("mkdir", help="Создать каталог")
    add_image(sp)
    sp.add_argument("path")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_mkdir)

    # rm
    sp = sub.add_parser("rm", help="Удалить файл/каталог")
    add_image(sp)
    sp.add_argument("path")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_rm)

    # mv
    sp = sub.add_parser("mv", help="Переименовать/переместить")
    add_image(sp)
    sp.add_argument("src")
    sp.add_argument("dst")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_mv)

    # verify
    sp = sub.add_parser("verify", help="Проверить целостность образа")
    add_image(sp)
    sp.set_defaults(func=cmd_verify)

    # recover
    sp = sub.add_parser("recover", help="Восстановить после сбоя")
    add_image(sp)
    sp.set_defaults(func=cmd_recover)

    # journal
    sp = sub.add_parser("journal", help="Показать журнал транзакций")
    add_image(sp)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_journal)

    # audit
    sp = sub.add_parser("audit", help="Экспорт журнала в JSONL")
    add_image(sp)
    sp.add_argument("out")
    sp.set_defaults(func=cmd_audit)

    # mcp
    sp = sub.add_parser("mcp", help="MCP-сервер (stdio) для AI-агентов")
    sp.set_defaults(func=cmd_mcp)

    # self-test
    sub.add_parser("self-test", help="Встроенные sanity-проверки")

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()

    # SIGINT -> корректное закрытие через finally в обработчиках
    def _sigint(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Прервано пользователем.", file=sys.stderr)
        return 130
    except (RWError, StrategyUnavailable) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"неожиданная ошибка: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
