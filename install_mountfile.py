"""Source-checkout installer for MountFile on Windows, Linux, and macOS.

The installer is deliberately usable from any current working directory:
every project and helper-script path is derived from this file.  It returns
process-style status codes from :func:`main`; the module entry point turns
that return value into the process exit code.  This script is intended to be
run from a source checkout and is not published as a wheel entry point: its
editable install and system-helper paths require the checkout to be present.

Examples::

    python install_mountfile.py
    python install_mountfile.py --minimal
    python install_mountfile.py --dev
    python install_mountfile.py --system-only --skip-ext
    python install_mountfile.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# Exit code returned by the Windows component script when Windows must be
# rebooted before WSL is ready.  It is intentionally not converted to zero.
REBOOT_REQUIRED = 3010
FAILURE = 1
USAGE_ERROR = 2
MINIMUM_PYTHON = (3, 10)

# Do not use Path.cwd(): users often launch this file from Downloads, a shell
# shortcut, or an elevated PowerShell that has another current directory.
PROJECT_DIR = Path(__file__).resolve().parent
BASE_DIR = PROJECT_DIR  # Kept as a public compatibility alias.
VENV_DIR = PROJECT_DIR / ".venv"
WINDOWS_COMPONENTS_SCRIPT = PROJECT_DIR / "scripts" / "install-windows-components.ps1"

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_LINUX = SYSTEM == "Linux"
IS_MAC = SYSTEM == "Darwin"

# These are also project dependencies.  Installing them first gives a clear
# failure point for a source-built dependency such as pytsk3.
BASE_DEPS = ["PyQt6>=6.11.0", "pytsk3>=20260715"]
RUNTIME_EXTRAS = ("api", "ai", "dfir")

# Qt runtime libraries are needed for PyQt6 on a stock Debian/Ubuntu desktop.
# The compiler/Python headers are required when a dependency has no wheel.
LINUX_APT = [
    "build-essential",
    "python3-dev",
    "python3-venv",
    "pkg-config",
    "libgl1",
    "libegl1",
    "libxkbcommon-x11-0",
    "libdbus-1-3",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "dosfstools",
    "mtools",
    "e2fsprogs",  # supplies debugfs for ext2/ext3/ext4
    "ntfs-3g",
]
MAC_BREW = ["qt@6", "e2fsprogs"]


def format_command(command: Sequence[object]) -> str:
    """Render a command only for a human-readable dry-run/log line."""

    return " ".join(str(part) for part in command)


def run(
    command: Sequence[object],
    *,
    dry_run: bool = False,
    capture: bool = False,
    quiet: bool = False,
) -> int:
    """Run *command* and return its real exit code.

    A missing executable is represented by the conventional code 127.  No
    subprocess exception is allowed to make a failed installation look like a
    success.  In dry-run mode subprocess is never started.
    """

    argv = [str(part) for part in command]
    if not quiet:
        print(f"  → {format_command(argv)}")
    if dry_run:
        return 0

    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError:
        print(f"  ❌ Команда не найдена: {argv[0]}")
        return 127
    except OSError as error:
        print(f"  ❌ Не удалось запустить команду: {error}")
        return FAILURE

    if result.returncode != 0:
        print(f"  ❌ Ошибка команды (код {result.returncode})")
        if capture and result.stderr:
            print(f"     {result.stderr.strip()[:800]}")
    return result.returncode


def have(command: str) -> bool:
    """Return whether an executable can be located in PATH."""

    return shutil.which(command) is not None


def is_admin() -> bool:
    """Return whether this process can install system packages directly."""

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


def is_venv_active() -> bool:
    return sys.prefix != sys.base_prefix


def venv_python() -> Path:
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv(*, dry_run: bool = False) -> str | None:
    """Create/reuse the project venv and return its interpreter path.

    ``None`` means creation failed or produced no usable interpreter.  This is
    intentionally not ``sys.exit`` so callers and unit tests receive a real
    failure result instead of continuing to pip with a broken environment.
    """

    if is_venv_active():
        print(f"✅ Уже в виртуальном окружении: {sys.prefix}")
        return sys.executable

    py = venv_python()
    if VENV_DIR.exists():
        print(f"✅ Найдено .venv: {VENV_DIR}")
    else:
        print("📦 Создаю .venv...")
        code = run([sys.executable, "-m", "venv", VENV_DIR], dry_run=dry_run)
        if code != 0:
            print("❌ Не удалось создать .venv; зависимости не будут устанавливаться.")
            return None

    # A dry run must neither create nor inspect a planned virtual environment
    # as a prerequisite for printing the remaining commands.
    if dry_run:
        return str(py)
    if not py.exists():
        print(f"❌ Не найден интерпретатор виртуального окружения: {py}")
        return None
    return str(py)


def install_system_linux(*, dry_run: bool = False, skip_ext: bool = False) -> int:
    """Install Debian/Ubuntu runtime and build dependencies."""

    if not have("apt-get"):
        print(
            "❌ apt-get не найден: автоматическая установка поддерживает Debian/Ubuntu."
        )
        return FAILURE

    if is_admin():
        sudo: list[str] = []
    elif have("sudo"):
        sudo = ["sudo"]
    else:
        print("❌ Нет прав root и sudo; системные пакеты не установлены.")
        return FAILURE

    packages = list(LINUX_APT)
    if skip_ext:
        packages.remove("e2fsprogs")
        print("⏭  debugfs/e2fsprogs пропущен (--skip-ext)")

    print("🐧 Установка системных пакетов (apt)...")
    code = run(sudo + ["apt-get", "update", "-qq"], dry_run=dry_run)
    if code != 0:
        return code
    return run(
        sudo + ["apt-get", "install", "-y", "-qq"] + packages,
        dry_run=dry_run,
    )


def install_system_mac(*, dry_run: bool = False, skip_ext: bool = False) -> int:
    """Install macOS packages that Homebrew provides reliably."""

    if not have("brew"):
        print("❌ Homebrew не найден. Установите Homebrew: https://brew.sh")
        return FAILURE

    packages = list(MAC_BREW)
    if skip_ext:
        packages.remove("e2fsprogs")
        print("⏭  e2fsprogs/debugfs пропущен (--skip-ext)")
    # Do not advertise a package whose availability depends on the current
    # Homebrew/tap state and macOS release.
    print(
        "ℹ️  ntfs-3g на macOS не устанавливается автоматически: пакет может быть недоступен."
    )
    print("🍺 Установка системных пакетов (brew)...")
    return run(["brew", "install"] + packages, dry_run=dry_run)


def install_system_windows(
    *, dry_run: bool = False, skip_ext: bool = False, skip_imdisk: bool = False
) -> int:
    """Run the repository's elevated Windows component installer.

    Its 3010 is significant: WSL/e2fsprogs may not be usable until reboot.
    The caller keeps installing Python dependencies, then returns that same
    code instead of printing a misleading successful completion banner.
    """

    command: list[object] = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(WINDOWS_COMPONENTS_SCRIPT),
    ]
    if skip_ext:
        command.append("-SkipExt")
    if skip_imdisk:
        command.append("-SkipImDisk")

    print("🪟 Установка WSL/e2fsprogs (debugfs) и ImDisk...")
    code = run(command, dry_run=dry_run)
    if code == REBOOT_REQUIRED:
        print("⚠️  Компоненты установлены, но Windows требуется перезагрузить (3010).")
    elif code != 0:
        print("❌ Установка Windows-компонентов завершилась ошибкой.")
    return code


def install_system_components(
    *, dry_run: bool = False, skip_ext: bool = False, skip_imdisk: bool = False
) -> int:
    """Dispatch installation for the current operating system."""

    if IS_LINUX:
        return install_system_linux(dry_run=dry_run, skip_ext=skip_ext)
    if IS_MAC:
        return install_system_mac(dry_run=dry_run, skip_ext=skip_ext)
    if IS_WINDOWS:
        return install_system_windows(
            dry_run=dry_run, skip_ext=skip_ext, skip_imdisk=skip_imdisk
        )
    print(f"⚠️  Для {SYSTEM} автоматический системный установщик отсутствует.")
    return FAILURE


def pip_install(py: str, packages: Sequence[str], *, dry_run: bool = False) -> int:
    """Upgrade pip tooling and install packages, stopping at the first error."""

    if not packages:
        return 0
    print(f"📦 pip install ({len(packages)} пакетов)...")
    code = run(
        [py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        dry_run=dry_run,
    )
    if code != 0:
        if IS_WINDOWS:
            print(
                "❌ pip завершился ошибкой. Если сбойная зависимость собирается "
                "из исходного кода и для неё нет wheel, MountFile намеренно не "
                "устанавливает Microsoft Visual Studio Build Tools автоматически. "
                "Установите совместимый wheel либо C++ Build Tools вручную."
            )
        return code

    code = run([py, "-m", "pip", "install", *packages], dry_run=dry_run)
    if code != 0 and IS_WINDOWS:
        print(
            "❌ pip не смог установить зависимость. Вероятно, для исходной "
            "зависимости нет готового wheel. MountFile не устанавливает Microsoft "
            "Visual Studio Build Tools автоматически; установите совместимый wheel "
            "либо C++ Build Tools вручную и повторите запуск."
        )
    return code


def selected_extras(*, minimal: bool, dev: bool) -> list[str]:
    """Return extras for the requested install profile.

    Default full installs contain runtime API/AI/DFIR functionality, not the
    developer toolchain.  ``--dev`` is an explicit opt-in.
    """

    extras = [] if minimal else list(RUNTIME_EXTRAS)
    if dev:
        extras.append("dev")
    return extras


def project_requirement(extras: Sequence[str]) -> str:
    """Build an absolute editable requirement independent of the CWD."""

    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"{PROJECT_DIR}{suffix}"


def install_project(py: str, *, extras: Sequence[str], dry_run: bool = False) -> int:
    """Install the local checkout in editable mode using its absolute path."""

    print("📦 Установка MountFile в editable-режиме...")
    code = run(
        [py, "-m", "pip", "install", "-e", project_requirement(extras)],
        dry_run=dry_run,
    )
    if code != 0 and IS_WINDOWS:
        print(
            "❌ Установка MountFile из исходного кода не прошла. См. сообщение "
            "pip выше; C++ Build Tools намеренно не устанавливаются автоматически."
        )
    return code


def checks_for_profile(*, minimal: bool, dev: bool) -> list[tuple[str, str]]:
    """Return import probes required by the selected installation profile."""

    checks = [
        ("PyQt6", "import PyQt6"),
        ("mountfile", "import mountfile"),
        ("pytsk3", "import pytsk3"),
    ]
    if not minimal:
        checks.extend(
            [
                ("fastapi", "import fastapi"),
                ("mcp", "import mcp"),
                ("pydantic", "import pydantic"),
                ("openai", "import openai"),
                ("yara", "import yara"),
            ]
        )
    if dev:
        checks.append(("pytest", "import pytest"))
    return checks


def verify_installation(
    py: str, *, minimal: bool, dev: bool, dry_run: bool = False
) -> int:
    """Run ``pip check`` and imports for the actual selected profile."""

    print("\n" + "=" * 64)
    print("  Проверка установки")
    print("=" * 64)
    code = run([py, "-m", "pip", "check"], dry_run=dry_run)
    if code != 0:
        print("❌ pip check обнаружил несовместимые зависимости.")
        return code
    print("  ✅ pip check")

    for name, import_code in checks_for_profile(minimal=minimal, dev=dev):
        code = run(
            [py, "-c", import_code],
            dry_run=dry_run,
            capture=True,
            quiet=True,
        )
        if code != 0:
            print(f"❌ {name}: импорт недоступен.")
            return code
        print(f"  ✅ {name}" + (" (план)" if dry_run else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Установщик зависимостей MountFile")
    parser.add_argument(
        "--minimal", action="store_true", help="Только базовый Python-профиль"
    )
    parser.add_argument("--dev", action="store_true", help="Также установить extra dev")
    parser.add_argument(
        "--win7",
        action="store_true",
        help="Legacy-профиль (не поддерживается текущим релизом)",
    )
    system_group = parser.add_mutually_exclusive_group()
    system_group.add_argument(
        "--no-sys", action="store_true", help="Не устанавливать системные пакеты"
    )
    system_group.add_argument(
        "--system-only",
        action="store_true",
        help="Установить только системные компоненты и завершиться",
    )
    parser.add_argument(
        "--skip-imdisk", action="store_true", help="Не устанавливать ImDisk в Windows"
    )
    parser.add_argument(
        "--skip-ext",
        action="store_true",
        help="Не устанавливать e2fsprogs/debugfs (WSL в Windows)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать команды без изменений"
    )
    return parser


def print_header(args: argparse.Namespace) -> None:
    mode = "Minimal" if args.minimal else "Полный (api, ai, dfir)"
    if args.dev:
        mode += " + dev"
    print("=" * 64)
    print("  MountFile — Автоустановка зависимостей")
    print(f"  ОС: {SYSTEM} {platform.release()}")
    print(
        f"  Python: {sys.version_info.major}.{sys.version_info.minor} ({sys.executable})"
    )
    print(f"  Режим: {mode}")
    print("=" * 64)


def python_is_supported() -> bool:
    return sys.version_info >= MINIMUM_PYTHON


def main(argv: Sequence[str] | None = None) -> int:
    """Install MountFile and return a process-compatible status code."""

    args = build_parser().parse_args(argv)
    print_header(args)

    if args.win7:
        print(
            "❌ Windows 7/legacy Python не поддерживается: текущий релиз требует Python 3.10+."
        )
        return USAGE_ERROR
    if not python_is_supported():
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(f"❌ Требуется Python 3.10+ (сейчас {current}).")
        return USAGE_ERROR

    reboot_pending = False
    should_install_system = not args.no_sys and (args.system_only or not args.minimal)
    if should_install_system:
        code = install_system_components(
            dry_run=args.dry_run,
            skip_ext=args.skip_ext,
            skip_imdisk=args.skip_imdisk,
        )
        if code == REBOOT_REQUIRED:
            reboot_pending = True
        elif code != 0:
            return code
    elif args.no_sys:
        print("⏭  Системные пакеты пропущены (--no-sys)")
    else:
        print("⏭  Системные пакеты не нужны для --minimal")

    if args.system_only:
        if reboot_pending:
            print(
                "⚠️  Нужна перезагрузка Windows; установка Python-зависимостей не запускалась (--system-only)."
            )
            return REBOOT_REQUIRED
        print("\n✅ Системные компоненты установлены.")
        return 0

    py = ensure_venv(dry_run=args.dry_run)
    if py is None:
        return FAILURE

    code = pip_install(py, BASE_DEPS, dry_run=args.dry_run)
    if code != 0:
        return code

    extras = selected_extras(minimal=args.minimal, dev=args.dev)
    code = install_project(py, extras=extras, dry_run=args.dry_run)
    if code != 0:
        return code

    code = verify_installation(
        py, minimal=args.minimal, dev=args.dev, dry_run=args.dry_run
    )
    if code != 0:
        return code

    if reboot_pending:
        print(
            "\n⚠️  Python-зависимости установлены, но Windows требуется перезагрузить (3010)."
        )
        return REBOOT_REQUIRED

    print("\n" + "=" * 64)
    print("  Готово!")
    print("=" * 64)
    if is_venv_active():
        print("🚀 Запуск: python main.py")
    elif IS_WINDOWS:
        print(r"🚀 Запуск: .venv\Scripts\python.exe main.py")
    else:
        print("🚀 Запуск: .venv/bin/python main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
