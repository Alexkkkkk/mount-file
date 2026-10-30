# SPDX-License-Identifier: MIT
"""Launch the optional Windows Ollama setup workflow after explicit consent."""

from __future__ import annotations

import importlib.resources as resources
import os
import re
import subprocess

SCRIPT_NAME = "install-ollama.ps1"
DEFAULT_MODEL = "llama3.2:3b"
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class OllamaSetupError(RuntimeError):
    """The Ollama setup workflow could not be started safely."""


def is_windows() -> bool:
    """Return whether the current process is running on Windows."""
    return os.name == "nt"


def windows_ollama_script_path() -> str:
    """Return the packaged setup script's absolute path."""
    try:
        resource = resources.files("mountfile").joinpath("data", SCRIPT_NAME)
        path = os.path.abspath(os.fspath(resource))
    except (AttributeError, FileNotFoundError, TypeError) as exc:
        raise OllamaSetupError(
            "Не найден встроенный установщик Ollama. Пересоберите MountFile "
            "из полного дистрибутива."
        ) from exc
    if not os.path.isfile(path):
        raise OllamaSetupError(f"Не найден встроенный установщик Ollama: {path}")
    return path


def launch_windows_ollama_setup(model: str = DEFAULT_MODEL):
    """Start the visible PowerShell installer and model pull on Windows."""
    if not is_windows():
        raise OllamaSetupError("Автоустановка Ollama доступна только в Windows.")
    model = str(model or DEFAULT_MODEL).strip()
    if not _MODEL_RE.fullmatch(model):
        raise OllamaSetupError(
            "Некорректное имя модели. Используйте буквы, цифры, '.', ':', '/', '_' или '-'."
        )
    script = windows_ollama_script_path()
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script,
        "-Model",
        model,
    ]
    try:
        return subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except OSError as exc:
        raise OllamaSetupError(
            f"Не удалось открыть PowerShell для установки Ollama: {exc}"
        ) from exc
