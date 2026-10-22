# SPDX-License-Identifier: MIT
"""Optional system-component installation helpers.

The GUI deliberately only *starts* the Windows installer after the user has
confirmed it.  It never waits for its exit code: the PowerShell window remains
open so its UAC, download and reboot messages are visible to the user.
"""

from __future__ import annotations

import importlib.resources as resources
import os
import subprocess

WINDOWS_INSTALLER = "install-windows-components.ps1"


class ComponentInstallError(RuntimeError):
    """The optional component installer could not be started."""


def is_windows() -> bool:
    """Return whether this process is running on Windows."""
    return os.name == "nt"


def windows_installer_path() -> str:
    """Return the absolute filesystem path of the packaged PowerShell script.

    ``powershell.exe -File`` needs a real path, rather than a Python module or
    ``sys.executable``.  MountFile's Windows builds unpack package data, so the
    :mod:`importlib.resources` traversable is expected to be filesystem-backed.
    """
    try:
        resource = resources.files("mountfile").joinpath("data", WINDOWS_INSTALLER)
        path = os.path.abspath(os.fspath(resource))
    except (AttributeError, FileNotFoundError, TypeError) as exc:
        raise ComponentInstallError(
            "Не найден встроенный установщик компонентов Windows. "
            "Переустановите MountFile из полного дистрибутива."
        ) from exc
    if not os.path.isfile(path):
        raise ComponentInstallError(
            f"Не найден встроенный установщик компонентов Windows: {path}"
        )
    return path


def windows_install_command(
    *, skip_imdisk: bool = False, skip_ext: bool = False
) -> list[str]:
    """Build the visible, non-interactive-launcher PowerShell command.

    The script itself requests elevation, owns all installs and returns either
    ``0`` or ``3010``.  ``-NoExit`` intentionally leaves its console open for
    the user to read those messages.  Calling it with ``&`` from ``-Command``
    keeps an explicit ``exit`` in the script scoped to that invocation.
    """
    switches = []
    if skip_imdisk:
        switches.append("-SkipImDisk")
    if skip_ext:
        switches.append("-SkipExt")
    script = windows_installer_path().replace("'", "''")
    arguments = " ".join(switches)
    command = (
        f"& '{script}'{' ' + arguments if arguments else ''}; "
        "Write-Host ('MountFile installer exit code: ' + $LASTEXITCODE)"
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-NoExit",
        "-Command",
        command,
    ]


def launch_windows_component_installer(
    *, skip_imdisk: bool = False, skip_ext: bool = False
):
    """Start the Windows installer in a separate console without waiting."""
    if not is_windows():
        raise ComponentInstallError("Установщик компонентов доступен только в Windows.")
    command = windows_install_command(skip_imdisk=skip_imdisk, skip_ext=skip_ext)
    try:
        return subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except OSError as exc:
        raise ComponentInstallError(
            f"Не удалось запустить PowerShell для установки компонентов: {exc}"
        ) from exc


def installation_hint() -> str:
    """Return a safe, platform-specific next step for a read-only backend."""
    if is_windows():
        return (
            "Установите компоненты через «Конфигурация → Установить компоненты…», "
            "затем переоткройте образ или перезапустите MountFile."
        )
    return (
        "Для установки системных компонентов выполните в терминале "
        "`python3 install_mountfile.py`; команда из MountFile не запускалась."
    )
