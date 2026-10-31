#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mountfile_all.py — единый скрипт для запуска ВСЕГО функционала Mount File.

Что делает:
  1)  Проверяет окружение и версию Mount File
  2)  Открывает GUI (если есть Qt и есть дисплей)
  3)  Прогоняет CLI-команды: volumes, info, ls, get, put, mkdir, mv, rm, decode, split
  4)  Демонстрирует Python API: partition, fat32, vfs/extract, updater, rt809
  5)  Запускает тесты (pytest + smoke + test_fat32)
  6)  Собирает бинарник (build_exe.sh / build_exe.bat) по флагу --build
  7)  Показывает карту команд и переменных окружения

Запуск (примеры):
  python mountfile_all.py                          # всё: CLI + API + тесты + GUI
  python mountfile_all.py --img dump.img           # указать образ
  python mountfile_all.py --dump dump.bin --udev udev.txt
  python mountfile_all.py --no-gui --no-tests      # только CLI + API
  python mountfile_all.py --cli-only               # только read-only CLI-команды
  python mountfile_all.py --api-only               # только read-only Python API
  python mountfile_all.py --allow-write            # разрешить демонстрационные записи в образ
  python mountfile_all.py --tests-only             # только тесты
  python mountfile_all.py --build                  # + сборка бинарника
  python mountfile_all.py --check-updates          # только проверка обновлений
  python mountfile_all.py --diag                   # штатная диагностика mountfile
  python mountfile_all.py --help

Windows 7: используйте Python 3.8 + requirements-win7.txt.
           pytsk3 недоступен → работает только движок FAT32.

Скрипт согласован с фактическим кодом репозитория (v1.3.0):
  * CLI-команды и их флаги          — mountfile/cli.py (COMMANDS, build_parser)
  * Разбор таблицы разделов         — mountfile.partition.list_partitions()
  * Запись на FAT32                 — mountfile.fat32.Fat32(fileobj, offset)
  * Чтение через Sleuth Kit         — mountfile.vfs.TskImage + mountfile.extract
  * RT809H decode/split             — mountfile.rt809.decode_dump/split_by_udev
  * Проверка обновлений             — mountfile.updater.check(); код 10 = есть апдейт
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import traceback
from pathlib import Path

# ---------- Константы путей ----------
ROOT = Path(__file__).resolve().parent
DEFAULT_IMG = "dump.img"
DEFAULT_DUMP = "dump.bin"
DEFAULT_UDEV = "udev.txt"
OUT_DIR = "./out"
PARTS_DIR = "./parts"
VFS_OUT = "./vfs_out"

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

#: Коды возврата main.py --check-updates
RC_UPDATE_AVAILABLE = 10
RC_OK = 0


# ============================================================
# УТИЛИТЫ ВЫВОДА
# ============================================================
def hr(title: str = "") -> None:
    line = "═" * 60
    print(f"\n{line}")
    if title:
        print(f"  {title}")
        print(line)


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def err(msg: str) -> None:
    print(f"  ❌ {msg}")


def info(msg: str) -> None:
    print(f"  ℹ️  {msg}")


def _as_dict(obj, keys) -> dict:
    """Универсально достаёт поля из объекта/словаря."""
    if isinstance(obj, dict):
        return {k: obj.get(k) for k in keys}
    return {k: getattr(obj, k, None) for k in keys}


# ============================================================
# 1. ОКРУЖЕНИЕ И ВЕРСИЯ
# ============================================================
def show_environment() -> None:
    hr("1. Окружение")
    info(f"Python: {sys.version.split()[0]} ({platform.python_implementation()})")
    info(f"Платформа: {platform.system()} {platform.release()} [{platform.machine()}]")
    info(f"Рабочий каталог: {ROOT}")

    try:
        from mountfile import __version__  # noqa: WPS433

        ok(f"Mount File версия: {__version__}")
    except Exception as exc:  # noqa: BLE001
        warn(f"Не удалось определить версию mountfile: {exc}")
        info("Проверьте, что вы запускаете скрипт из корня репозитория mount-file.")

    # Необязательные зависимости — их наличие определяет доступные движки.
    for name in ("pytsk3", "PyQt6", "PyQt5"):
        try:
            __import__(name)
            ok(f"{name}: установлен")
        except ImportError:
            warn(f"{name}: не установлен")


def run_diag() -> None:
    """Штатная диагностика Mount File (main.py --diag)."""
    hr("Диагностика mountfile (main.py --diag)")
    run_cli([sys.executable, "main.py", "--diag"])


# ============================================================
# 2. GUI
# ============================================================
def launch_gui(image: str | None = None) -> None:
    hr("2. GUI (Mount File)")
    try:
        import mountfile.qt_compat  # noqa: F401,WPS433
    except Exception as exc:  # noqa: BLE001
        warn(f"Qt-слой недоступен ({exc}) — GUI пропущен.")
        info("Установите зависимости: pip install -r requirements.txt")
        return

    if not os.environ.get("DISPLAY") and IS_LINUX:
        warn("DISPLAY не задан — GUI на Linux может не запуститься.")

    cmd = [sys.executable, "main.py"]
    if image:
        cmd.append(image)
    info("Команда: " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        warn("GUI прерван пользователем.")


# ============================================================
# 3. CLI-КОМАНДЫ (соответствуют mountfile/cli.py)
# ============================================================
def run_cli(cmd: list[str], check: bool = False) -> int:
    info("$ " + " ".join(cmd))
    try:
        res = subprocess.run(cmd, check=check)
        return res.returncode
    except subprocess.CalledProcessError as exc:
        err(f"Команда завершилась с кодом {exc.returncode}")
        return exc.returncode
    except FileNotFoundError:
        err("Python не найден?")
        return 127


def run_all_cli(img: str, dump: str, udev: str, *, allow_write: bool = False) -> None:
    hr("3. CLI-команды Mount File")
    main = [sys.executable, "main.py"]

    if Path(img).exists():
        run_cli(main + ["volumes", img])
        run_cli(main + ["info", img])
        run_cli(main + ["ls", img, "/"])

        Path(OUT_DIR).mkdir(exist_ok=True)
        run_cli(main + ["get", img, "/", "-o", OUT_DIR])

        if allow_write:
            # Explicit opt-in: these commands modify the image.
            demo = Path("hello.txt")
            if not demo.exists():
                demo.write_text("Hello Mount File!\n", encoding="utf-8")
            run_cli(main + ["put", img, str(demo), "--dest", "/", "--yes"])
            run_cli(main + ["mkdir", img, "/new", "--yes"])
            # mv renames within the same directory; its second path is a name.
            run_cli(main + ["mv", img, "/hello.txt", "renamed.txt", "--yes"])
            run_cli(main + ["rm", img, "/new", "--recursive", "--yes"])
        else:
            info(
                "Запись в образ пропущена; для демонстрации записи укажите --allow-write."
            )
    else:
        warn(
            f"Образ {img} не найден — команды volumes/ls/get/put/mkdir/mv/rm пропущены."
        )

    if Path(dump).exists():
        run_cli(main + ["decode", dump])
    else:
        warn(f"Дамп {dump} не найден — decode пропущен.")

    if Path(dump).exists() and Path(udev).exists():
        Path(PARTS_DIR).mkdir(exist_ok=True)
        run_cli(main + ["split", dump, udev, "-o", PARTS_DIR])
    else:
        warn(f"Нужны {dump} и {udev} для split — пропущено.")


def cli_only(img: str, dump: str, udev: str, *, allow_write: bool = False) -> None:
    hr("CLI-only режим")
    run_all_cli(img, dump, udev, allow_write=allow_write)


# ============================================================
# 4. PYTHON API (демонстрация)
# ============================================================
def demo_api(img: str, dump: str, udev: str, *, allow_write: bool = False) -> None:
    hr("4. Python API — демонстрация")

    # --- 4.1. Разделы: mountfile.partition.list_partitions() ---
    try:
        from mountfile.partition import list_partitions  # noqa: WPS433

        if Path(img).exists():
            info("list_partitions():")
            parts = list_partitions(img)
            if not parts:
                warn("Таблица разделов не найдена в образе.")
            for i, p in enumerate(parts):
                d = _as_dict(p, ("name", "ptype", "start_lba", "sectors"))
                print(
                    f"    [{i}] {d['name']}  type={p.type_name}  "
                    f"offset=0x{p.offset:X}  size={p.size_mb:.1f} MB"
                )
            ok(f"Прочитано разделов: {len(parts)}.")
        else:
            warn(f"{img} не найден — list_partitions пропущен.")
    except Exception as exc:  # noqa: BLE001
        warn(f"partition API недоступен: {exc}")

    # --- 4.2. FAT32: чтение + запись (Fat32 принимает ОТКРЫТЫЙ файловый объект) ---
    try:
        from mountfile.fat32 import Fat32  # noqa: WPS433

        if Path(img).exists():
            with open(img, "r+b" if allow_write else "rb") as fh:
                fs = Fat32(fh, offset=0)

                info("FAT32: листинг корня")
                try:
                    root_cluster = fs.find_dir("/")
                    for entry in fs.list_dir(root_cluster):
                        kind = "<DIR>" if entry.is_dir else "FILE"
                        print(f"    {kind:>4}  {entry.size:>10}  {entry.name}")
                except Exception as exc:  # noqa: BLE001
                    warn(f"list_dir не сработал: {exc}")

                if allow_write:
                    try:
                        # mkdir(dir_path, name) and write_file(dir_path, filename, data)
                        fs.mkdir("/", "api_test")
                        fs.write_file("/api_test", "hello.txt", b"Hello from API!\n")
                        fs.rename("/api_test/hello.txt", "renamed.txt")
                        fs.delete("/api_test/renamed.txt")
                        fs.delete("/api_test")
                        ok("FAT32: запись/переименование/удаление выполнены.")
                    except Exception as exc:  # noqa: BLE001
                        warn(f"Операции записи FAT32 не удались: {exc}")
                else:
                    info(
                        "FAT32 открыт только для чтения; для теста записи укажите --allow-write."
                    )
        else:
            warn(f"{img} не найден — FAT32 API пропущен.")
    except Exception as exc:  # noqa: BLE001
        warn(f"FAT32 API недоступен: {exc}")

    # --- 4.3. Извлечение: TskImage (pytsk3) с откатом на FAT32Provider ---
    try:
        from mountfile.extract import extract_path  # noqa: WPS433

        if Path(img).exists():
            Path(VFS_OUT).mkdir(exist_ok=True)
            provider = None
            backend = None
            try:
                from mountfile.vfs import TskImage  # noqa: WPS433

                provider, backend = TskImage(img, offset=0), "tsk"
            except Exception as exc:  # noqa: BLE001
                info(f"pytsk3/TskImage недоступен ({exc}); беру FAT32Provider.")
                from mountfile.fat32 import Fat32Provider  # noqa: WPS433

                provider, backend = Fat32Provider(img, offset=0), "fat32"

            try:
                root = "/"
                names = [it["name"] for it in provider.list_dir(root)]
                info(f"Бэкенд {backend}: в корне {len(names)} записей.")
                count = extract_path(provider, root, VFS_OUT)
                ok(f"extract_path: извлечено файлов → {count} (в {VFS_OUT})")
            finally:
                if hasattr(provider, "close"):
                    provider.close()
        else:
            warn(f"{img} не найден — извлечение пропущено.")
    except Exception as exc:  # noqa: BLE001
        warn(f"extract/VFS API недоступен: {exc}")

    # --- 4.4. RT809H: decode_dump() + split_by_udev() ---
    try:
        from mountfile.rt809 import decode_dump  # noqa: WPS433
        from mountfile.rt809 import Rt809Error, detect, split_by_udev

        if Path(dump).exists():
            try:
                kind, encrypted = detect(dump)
                info(f"detect(): kind={kind} encrypted={encrypted}")
            except Exception as exc:  # noqa: BLE001
                warn(f"rt809.detect не сработал: {exc}")

            try:
                out = decode_dump(dump)
                if out is None:
                    warn("Дамп не зашифрован — decode ничего не записал.")
                else:
                    ok(f"RT809H decode → {out}")
            except Rt809Error as exc:
                warn(f"rt809.decode_dump: {exc}")

            if Path(udev).exists():
                Path(PARTS_DIR).mkdir(exist_ok=True)
                try:
                    # split_by_udev возвращает кортеж (parts, written)
                    result = split_by_udev(dump, udev, PARTS_DIR)
                    parts, written = (
                        result if isinstance(result, tuple) else (result, [])
                    )
                    for p in parts:
                        print(f"    {p}")
                    ok(f"rt809.split_by_udev: записано файлов — {len(written)}")
                except Exception as exc:  # noqa: BLE001
                    warn(f"rt809.split_by_udev не сработал: {exc}")
            else:
                warn(f"{udev} не найден — split_by_udev пропущен.")
        else:
            warn(f"{dump} не найден — RT809H API пропущен.")
    except Exception as exc:  # noqa: BLE001
        warn(f"Модуль rt809 недоступен: {exc}")

    # --- 4.5. Updater: check() возвращает dict или None ---
    try:
        from mountfile import __version__, updater  # noqa: WPS433

        info(f"Репозиторий релизов: {updater.repo()}")
        info(f"URL latest.json: {updater.update_url()}")
        if updater.disabled():
            warn("Проверка обновлений отключена (MOUNTFILE_NO_UPDATE).")
        else:
            res = updater.check(__version__)
            if res:
                ok(
                    "Доступно обновление: v%s (у вас v%s)"
                    % (res.get("version"), __version__)
                )
                if res.get("asset"):
                    ok("  asset: %s" % res["asset"].get("name"))
                ok("  страница: %s" % res.get("url"))
            else:
                ok(f"updater.check → обновлений нет (v{__version__} актуален)")
    except Exception as exc:  # noqa: BLE001
        warn(f"updater API недоступен: {exc}")


def api_only(img: str, dump: str, udev: str, *, allow_write: bool = False) -> None:
    hr("API-only режим")
    demo_api(img, dump, udev, allow_write=allow_write)


# ============================================================
# 5. ТЕСТЫ
# ============================================================
def run_tests() -> None:
    hr("5. Тесты Mount File")
    py = sys.executable

    # smoke
    smoke = ROOT / "tests" / "smoke_startup.py"
    if smoke.exists():
        run_cli([py, str(smoke)])
    else:
        warn("tests/smoke_startup.py не найден.")

    # standalone FAT32
    fat32 = ROOT / "tests" / "test_fat32.py"
    if fat32.exists():
        run_cli([py, str(fat32)])
    else:
        warn("tests/test_fat32.py не найден.")

    # pytest по всему каталогу
    tests_dir = ROOT / "tests"
    if tests_dir.exists():
        run_cli([py, "-m", "pytest", str(tests_dir), "-v", "--maxfail=5"])
    else:
        warn("Каталог tests/ не найден.")


def tests_only() -> None:
    hr("Tests-only режим")
    run_tests()


# ============================================================
# 6. СБОРКА
# ============================================================
def build_binary() -> None:
    hr("6. Сборка бинарника")
    if IS_WINDOWS:
        script = ROOT / "build_exe.bat"
        if script.exists():
            subprocess.run(["cmd", "/c", str(script)], check=False)
        else:
            warn("build_exe.bat не найден.")
    else:
        script = ROOT / "build_exe.sh"
        if script.exists():
            subprocess.run(["bash", str(script)], check=False)
        else:
            warn("build_exe.sh не найден.")


# ============================================================
# 7. ПРОВЕРКА ОБНОВЛЕНИЙ
# ============================================================
def check_updates() -> int:
    hr("Проверка обновлений")
    res = subprocess.run([sys.executable, "main.py", "--check-updates"], check=False)
    verdict = (
        "есть обновление" if res.returncode == RC_UPDATE_AVAILABLE else "обновлений нет"
    )
    print(f"  Код возврата: {res.returncode} ({verdict})")
    return res.returncode


# ============================================================
# 8. КАРТА КОМАНД И ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================
def print_cheatsheet() -> None:
    hr("Карта команд Mount File")
    print(
        """
  GUI:
    python main.py                       # открыть интерфейс
    python main.py dump.img              # открыть образ сразу
    python main.py --diag                # диагностика окружения и выход
    python main.py --version             # версия и выход

  CLI (mountfile/cli.py, COMMANDS):
    python main.py volumes <img>         # список разделов
    python main.py info <img>            # сводка по образу
    python -m mountfile capabilities     # JSON матрица профилей чтения/записи
    python main.py ls <img> /            # листинг каталога
    python main.py stat <img> /path      # метаданные пути
    python main.py read-range <img> /file --file-offset 0 --length 4096
                                          # bounded read; output bytes or -o file
    python main.py get <img> / -o out    # извлечение файла/каталога
    python main.py put <img> f --dest / --yes    # импорт в FAT32
    python main.py mkdir <img> /new
    python main.py mv <img> /a b         # переименование в том же каталоге
    python main.py rm <img> /new -r --yes
    python main.py decode <dump.bin>     # RT809H (MST/MTK)
    python main.py split <dump.bin> <udev.txt> -o parts
    python main.py autopick <img>        # авто-подбор рабочего тома (offset + ФС)
    python main.py autopick <img> --json --extract out
    python main.py ls <img> / --auto     # авто-подбор для любой команды
    python main.py grep <img> "password=" --encoding utf-16le
    python main.py hash <img>            # SHA-256 identity (read-only)
    python -m mountfile evidence-manifest <img> --output <img>.manifest.json
                                          # read-only byte hash; no format detection
    python main.py timeline <img> --csv timeline.csv
    python main.py timestomp <img> --csv timeline.csv
    python main.py timeline-export --csv timeline.csv --output utc.csv
    python main.py yara <img> --rules user.yar  # optional yara-python

  DFIR-команды из эпика #87 (read-only):
    python -m mountfile timeline-build <img> -o timeline.db --csv timeline.csv
    python -m mountfile timeline-query --db timeline.db --start 2024-03-12T14:00 --end 2024-03-12T15:00
    python -m mountfile search <img> "password=" [--regex] [--encoding utf-16le] [--files]
    python -m mountfile yara-scan <img> --rules rules/ [--targets files unallocated] [--json]
    python -m mountfile timestomp-scan <img> [--skew 2.0] [--json]
    python -m mountfile slack-extract <img> -o slack/
    python -m mountfile live-diff before.img after.img -o diff.md
    python -m mountfile unallocated-scan <img> --ranges ranges.json
    python -m mountfile slack <img> --slack-only \\
      --last-cluster-offset 0x1000 --file-size 123 \\
      --cluster-size 4096 -o slack.bin
    python main.py --check-updates       # 10=есть апдейт, 0=нет

  Общие флаги у команд с образом:
    -V/--volume N     номер раздела (1-based, как в `volumes`)
    -O/--offset 0x…   смещение файловой системы вручную
    --scan-limit N    сколько байт сканировать на сигнатуры
    --backup          копия <img>.bak перед записью
    --auto            авто-подбор тома: MBR/GPT + udev + сигнатуры ФС,
                      победитель подтверждается реальным монтированием

  Переменные окружения:
    MOUNTFILE_NO_UPDATE=1                # отключить проверку обновлений
    MOUNTFILE_AUTO_UPDATE=1              # ставить без подтверждения
    MOUNTFILE_UPDATE_REPO=owner/repo     # другой репозиторий релизов
    MOUNTFILE_GIT_PULL=1                 # git pull при запуске из исходников
    MOUNTFILE_UPDATE_URL=…               # прямой URL latest.json

  Python API:
    mountfile.partition.list_partitions(path)  -> [Partition]  (offset, size_mb, type_name)
    mountfile.fat32.Fat32(open(img,"r+b"), offset)  # нужен ОТКРЫТЫЙ файловый объект
        .list_dir(cluster) / .find_dir(path) / .read_file(path)
        .mkdir(dir_path, name) / .write_file(dir_path, filename, data)
        .rename(path, new_name) / .delete(path, recursive=False)
    mountfile.fat32.Fat32Provider(image_path, offset)  # read-only, list_dir(path)
    mountfile.vfs.TskImage(image_path, offset)         # pytsk3, list_dir/read_file
    mountfile.extract.extract_path(provider, src, dst_dir) -> int
    mountfile.rt809.decode_dump(dump, table_dir=None)
    mountfile.rt809.split_by_udev(dump, udev, out_dir) -> (parts, written)
    mountfile.rt809.detect(dump) -> (kind, encrypted)
    mountfile.updater.check(version) -> dict | None

  Тесты:
    python -m pytest tests/ -v
    python tests/test_fat32.py

  Сборка:
    Linux:     ./build_exe.sh   → dist/MountFile-linux-x86-64
    Win10/11:  build_exe.bat    → dist\\MountFile.exe
    Win7:      py -3.8 … build_exe.bat (requirements-win7.txt)
"""
    )


# ============================================================
# MAIN
# ============================================================
def _setup_console() -> None:
    """Force UTF-8 on stdout/stderr.

    The status markers below are emoji; a Windows console (or a redirected
    pipe) defaults to cp1252 and printing them would abort the whole run with
    UnicodeEncodeError. Reconfiguring is a no-op on streams without the method
    and never raises when the stream is already detached.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # detached or already closed stream
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Единый скрипт запуска всего функционала Mount File.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--img", default=DEFAULT_IMG, help="Путь к raw-образу (по умолчанию dump.img)"
    )
    p.add_argument(
        "--dump",
        default=DEFAULT_DUMP,
        help="Путь к дампу RT809H (по умолчанию dump.bin)",
    )
    p.add_argument(
        "--udev",
        default=DEFAULT_UDEV,
        help="Файл разметки udev (по умолчанию udev.txt)",
    )

    p.add_argument("--no-gui", action="store_true", help="Не открывать GUI")
    p.add_argument("--no-cli", action="store_true", help="Не выполнять CLI-команды")
    p.add_argument(
        "--no-api", action="store_true", help="Не демонстрировать Python API"
    )
    p.add_argument("--no-tests", action="store_true", help="Не запускать тесты")
    p.add_argument(
        "--allow-write",
        action="store_true",
        help="Разрешить демонстрационные операции записи в указанный образ",
    )
    p.add_argument("--build", action="store_true", help="Собрать бинарник")
    p.add_argument(
        "--cheatsheet", action="store_true", help="Показать карту команд и выйти"
    )
    p.add_argument("--diag", action="store_true", help="Диагностика mountfile и выход")

    p.add_argument(
        "--check-updates", action="store_true", help="Только проверка обновлений"
    )
    p.add_argument("--cli-only", action="store_true", help="Только CLI-команды")
    p.add_argument("--api-only", action="store_true", help="Только Python API")
    p.add_argument("--tests-only", action="store_true", help="Только тесты")
    return p.parse_args()


def main() -> int:
    _setup_console()
    args = parse_args()

    if args.cheatsheet:
        print_cheatsheet()
        return 0

    if args.check_updates:
        return check_updates()

    if args.diag:
        run_diag()
        return 0

    if args.cli_only:
        cli_only(args.img, args.dump, args.udev, allow_write=args.allow_write)
        print_cheatsheet()
        return 0

    if args.api_only:
        api_only(args.img, args.dump, args.udev, allow_write=args.allow_write)
        return 0

    if args.tests_only:
        tests_only()
        return 0

    # Полный прогон
    show_environment()

    if not args.no_cli:
        run_all_cli(args.img, args.dump, args.udev, allow_write=args.allow_write)

    if not args.no_api:
        demo_api(args.img, args.dump, args.udev, allow_write=args.allow_write)

    if not args.no_tests:
        run_tests()

    if args.build:
        build_binary()

    if not args.no_gui:
        launch_gui(args.img)

    print_cheatsheet()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⏹  Прервано пользователем.")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        print("\n💥 Непредвиденная ошибка:")
        traceback.print_exc()
        sys.exit(1)
