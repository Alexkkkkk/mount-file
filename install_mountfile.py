# -*- coding: utf-8 -*-
"""
Mount File — универсальная автоустановка зависимостей.
ОС: Windows / Linux / macOS.

Запуск:
    python install_mountfile.py
    python install_mountfile.py --minimal     # без AI/API/DFIR
    python install_mountfile.py --win7        # Windows 7 (Python 3.8 + PyQt5)
    python install_mountfile.py --no-sys      # не ставить системные пакеты
    python install_mountfile.py --dry-run     # показать команды без запуска
"""

import os
import sys
import subprocess
import platform
import argparse
import shutil
from pathlib import Path

# ------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
VENV_DIR = BASE_DIR / ".venv"

SYSTEM = platform.system()          # Windows / Linux / Darwin
IS_WINDOWS = SYSTEM == "Windows"
IS_LINUX = SYSTEM == "Linux"
IS_MAC = SYSTEM == "Darwin"
PY_VER = f"{sys.version_info.major}.{sys.version_info.minor}"

# Базовые зависимости
BASE_DEPS = [
    "PyQt6>=6.11.0; python_version>='3.10'",
    "PyQt5==5.15.11; python_version<'3.10'",
    "requests>=2.31.0",
    "packaging>=23.0",
    "tomli>=2.0.1; python_version<'3.11'",
]

# Опциональные: AI
AI_DEPS = ["httpx>=0.27.0"]

# Опциональные: REST API
API_DEPS = ["fastapi>=0.110.0", "uvicorn>=0.29.0", "pydantic>=2.0"]

# Опциональные: DFIR
DFIR_DEPS = [
    "python-dateutil>=2.8.2",
    "yara-python>=4.5.0; platform_system!='Windows' or python_version>='3.10'",
]

# Windows 7 (Python 3.8 + PyQt5)
WIN7_DEPS = [
    "PyQt5==5.15.11",
    "requests>=2.25.0",
    "packaging>=20.0",
    "tomli>=1.2.0",
]

# Системные пакеты
LINUX_APT = [
    "libgl1", "libegl1", "libxkbcommon-x11-0", "libdbus-1-3",
    "libxcb-cursor0", "libxcb-icccm4", "libxcb-image0",
    "libxcb-keysyms1", "libxcb-randr0", "libxcb-render-util0",
    "libxcb-shape0", "dosfstools", "mtools", "e2fsprogs",
]
MAC_BREW = ["qt@6", "e2fsprogs"]

# ------------------------------------------------------------
# УТИЛИТЫ
# ------------------------------------------------------------
def run(cmd, check=False, capture=False, dry_run=False, quiet=False):
    if not quiet:
        print(f"  → {' '.join(map(str, cmd)) if isinstance(cmd, list) else cmd}")
    if dry_run:
        return None
    try:
        return subprocess.run(
            cmd, check=check, shell=isinstance(cmd, str),
            capture_output=capture, text=capture,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Ошибка (код {e.returncode})")
        if capture and e.stderr:
            print(f"     {e.stderr.strip()[:400]}")
        return None
    except FileNotFoundError:
        print(f"  ❌ Команда не найдена: {cmd[0] if isinstance(cmd, list) else cmd}")
        return None


def have(cmd):
    return shutil.which(cmd) is not None


def is_admin():
    if IS_WINDOWS:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def is_venv_active():
    return sys.prefix != sys.base_prefix


def venv_python():
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


# ------------------------------------------------------------
# VENV
# ------------------------------------------------------------
def ensure_venv(dry_run=False):
    if is_venv_active():
        print(f"✅ Уже в виртуальном окружении: {sys.prefix}")
        return sys.executable

    if VENV_DIR.exists():
        print(f"✅ Найдено .venv: {VENV_DIR}")
    else:
        print("📦 Создаю .venv...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True, dry_run=dry_run)
        if dry_run:
            return sys.executable

    py = venv_python()
    if not py.exists():
        print(f"❌ Не найден {py}. Перезапустите скрипт вручную.")
        sys.exit(1)
    return str(py)


# ------------------------------------------------------------
# СИСТЕМНЫЕ ПАКЕТЫ
# ------------------------------------------------------------
def install_system_linux(dry_run=False):
    if not have("apt-get"):
        print("⚠️  apt-get не найден (не Debian/Ubuntu). Пропускаю.")
        return
    sudo = [] if is_admin() else (["sudo"] if have("sudo") else [])
    if not sudo and not is_admin():
        print("⚠️  Нет sudo — пропускаю apt.")
        return
    print("🐧 Установка системных пакетов (apt)...")
    run(sudo + ["apt-get", "update", "-qq"], dry_run=dry_run)
    run(sudo + ["apt-get", "install", "-y", "-qq"] + LINUX_APT, dry_run=dry_run)


def install_system_mac(dry_run=False):
    if not have("brew"):
        print("⚠️  Homebrew не найден. Установите: https://brew.sh")
        return
    print("🍺 Установка системных пакетов (brew)...")
    run(["brew", "install"] + MAC_BREW, dry_run=dry_run)


def install_system_windows():
    if not have("py") and not have("python"):
        print("⚠️  Python не найден в PATH. Скачайте: https://python.org")
    print("ℹ️  Для pytsk3/yara на Windows может понадобиться 'Microsoft C++ Build Tools'.")
    print("    https://visualstudio.microsoft.com/visual-cpp-build-tools/")


# ------------------------------------------------------------
# PIP
# ------------------------------------------------------------
def pip_install(py, packages, dry_run=False):
    if not packages:
        return
    print(f"📦 pip install ({len(packages)} пакетов)...")
    run([py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        dry_run=dry_run)
    run([py, "-m", "pip", "install"] + packages, dry_run=dry_run)


def try_pytsk3(py, dry_run=False):
    print("🔍 pytsk3 (NTFS/EXT) — попытка установки...")
    res = run([py, "-m", "pip", "install", "pytsk3"],
              dry_run=dry_run, capture=True, quiet=True)
    if dry_run:
        return
    if res is None or res.returncode != 0:
        print("⚠️  pytsk3 не установлена. Mount File будет работать только с FAT32.")
        if IS_WINDOWS:
            print("    Поставьте 'Microsoft C++ Build Tools' и повторите.")
    else:
        print("✅ pytsk3 OK")


# ------------------------------------------------------------
# ГЛАВНОЕ
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimal", action="store_true", help="Только базовые зависимости")
    parser.add_argument("--win7", action="store_true", help="Режим Windows 7")
    parser.add_argument("--no-sys", action="store_true", help="Не ставить системные пакеты")
    parser.add_argument("--dry-run", action="store_true", help="Показать команды")
    args = parser.parse_args()

    print("=" * 64)
    print("  Mount File — Автоустановка зависимостей")
    print(f"  ОС: {SYSTEM} {platform.release()}")
    print(f"  Python: {PY_VER}  ({sys.executable})")
    mode = "Win7" if args.win7 else ("Minimal" if args.minimal else "Полный")
    print(f"  Режим: {mode}")
    print("=" * 64)

    # 1. Валидация Python
    if args.win7:
        if sys.version_info[:2] != (3, 8):
            print("❌ Для Win7 нужен Python 3.8.10.")
            sys.exit(1)
    else:
        if sys.version_info < (3, 10):
            print(f"⚠️  Рекомендуется Python 3.10+ (сейчас {PY_VER}).")

    # 2. Системные пакеты
    if not args.minimal and not args.no_sys:
        if IS_LINUX:
            install_system_linux(dry_run=args.dry_run)
        elif IS_MAC:
            install_system_mac(dry_run=args.dry_run)
        elif IS_WINDOWS:
            install_system_windows()
    elif args.no_sys:
        print("⏭  Системные пакеты пропущены (--no-sys)")

    # 3. venv
    py = ensure_venv(dry_run=args.dry_run)

    # 4. Pip-зависимости
    if args.win7:
        pip_install(py, WIN7_DEPS, dry_run=args.dry_run)
    else:
        pkgs = list(BASE_DEPS)
        if not args.minimal:
            pkgs += AI_DEPS + API_DEPS + DFIR_DEPS
        pip_install(py, pkgs, dry_run=args.dry_run)

    # 5. pytsk3 (не критично)
    if not args.win7:
        try_pytsk3(py, dry_run=args.dry_run)

    # 6. Сам проект (editable)
    print("📦 Установка mount-file в editable-режиме...")
    extras = "" if (args.minimal or args.win7) else "[api,dev]"
    run([py, "-m", "pip", "install", "-e", f".{extras}"], dry_run=args.dry_run)

    # 7. Проверка
    print("\n" + "=" * 64)
    print("  Проверка установки")
    print("=" * 64)
    checks = [
        ("PyQt6", "import PyQt6; print('PyQt6 OK')"),
        ("mountfile", "import mountfile; print('mountfile OK')"),
        ("pytsk3", "import pytsk3; print('pytsk3 OK')"),
        ("yara", "import yara; print('yara OK')"),
        ("fastapi", "import fastapi; print('fastapi OK')"),
    ]
    for name, code in checks:
        res = run([py, "-c", code], dry_run=args.dry_run, capture=True, quiet=True)
        if args.dry_run or (res and res.returncode == 0):
            print(f"  ✅ {name}")
        else:
            print(f"  ⚠️  {name}: не установлен/недоступен")

    # 8. Инструкция
    print("\n" + "=" * 64)
    print("  Готово!")
    print("=" * 64)
    if is_venv_active():
        print("🚀 Запуск: python main.py")
    else:
        if IS_WINDOWS:
            print(r"🚀 Запуск: .venv\Scripts\python.exe main.py")
        else:
            print("🚀 Запуск: .venv/bin/python main.py")
        print("   (или активируйте venv и пишите просто python main.py)")


if __name__ == "__main__":
    main()
