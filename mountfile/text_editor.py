# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Opening text files in the system editor («Блокнот»).

Total Commander opens a text file in the system editor, and Mount File does
the same through this module - the single place where the editor is chosen:

* Windows  - ``notepad.exe`` (Блокнот);
* macOS    - ``open -t`` (the default text editor, i.e. TextEdit);
* Linux    - the first editor found on ``PATH`` (gedit, kate, xed, pluma,
  mousepad, leafpad, featherpad), else ``xdg-open``.

``$MOUNTFILE_EDITOR`` / ``$VISUAL`` / ``$EDITOR`` are honoured on **every**
POSIX host - macOS included - and win over the per-OS default above.

``.txt`` is the primary extension; the other plain-text kinds listed in
:data:`TEXT_EXTENSIONS` take the same route.

A file that lives inside an image cannot be handed to an external editor
directly, so :func:`prepare_image_text` writes a temporary copy on the host
first. The GUI can request the editor process and, after it exits, safely
import changed bytes back into the image. Text bytes that are not valid UTF-8
are re-encoded to UTF-8 with a BOM for the copy (CP1251 / CP866 are tried),
otherwise Блокнот shows mojibake instead of Cyrillic.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "TEXT_EXTENSIONS",
    "decode_for_editor",
    "editor_command",
    "is_text_file",
    "open_in_editor",
    "prepare_image_text",
    "safe_name",
]

#: Extensions Mount File sends to the system editor instead of the hex viewer.
TEXT_EXTENSIONS = (
    ".txt",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".md",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
    ".bat",
    ".cmd",
    ".sh",
    ".py",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".reg",
)

#: GUI editors tried on Linux, in order, when no ``$EDITOR`` is configured.
LINUX_EDITORS = (
    "gedit",
    "kate",
    "xed",
    "pluma",
    "mousepad",
    "leafpad",
    "featherpad",
)

#: Encodings tried for a non-UTF-8 copy, most likely first.
FALLBACK_ENCODINGS = ("cp1251", "cp866")


def is_text_file(name) -> bool:
    """True when `name` is one of the plain-text kinds we open in the editor."""
    return os.path.splitext(str(name))[1].lower() in TEXT_EXTENSIONS


def safe_name(name) -> str:
    """Turn an on-image name into a name that is safe on the host FS."""
    cleaned = str(name).replace("/", "_").replace("\\", "_").replace(":", "_")
    cleaned = cleaned.replace(os.sep, "_")
    if cleaned in ("", ".", ".."):
        cleaned = "file.txt"
    return cleaned


def _editor_from_env():
    for var in ("MOUNTFILE_EDITOR", "VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if value:
            return shlex.split(value)
    return None


def editor_command(path):
    """The argv that opens `path` in the system text editor.

    ``None`` means no editor was found; the caller decides what to report.
    """
    if os.name == "nt":  # pragma: no cover - Windows only
        exe = shutil.which("notepad.exe") or shutil.which("notepad") or "notepad.exe"
        return [exe, str(path)]
    # An explicit MOUNTFILE_EDITOR / VISUAL / EDITOR wins on every POSIX host,
    # macOS included - otherwise "$EDITOR" would be silently ignored there.
    from_env = _editor_from_env()
    if from_env:
        return from_env + [str(path)]
    if sys.platform == "darwin":  # pragma: no cover - macOS only
        return ["open", "-t", str(path)]
    for candidate in LINUX_EDITORS:
        exe = shutil.which(candidate)
        if exe:
            return [exe, str(path)]
    xdg = shutil.which("xdg-open")
    if xdg:
        return [xdg, str(path)]
    return None


def open_in_editor(path, *, return_process=False):
    """Start the system editor on `path` and return the argv used.

    The historical return value is an argv list.  With ``return_process=True``
    return ``(argv, Popen)`` so a GUI can observe editor exit without blocking
    its event loop. Raises :class:`RuntimeError` when no editor is available.
    """
    argv = editor_command(path)
    if argv is None:
        raise RuntimeError(
            "текстовый редактор не найден: задайте MOUNTFILE_EDITOR "
            "(например, MOUNTFILE_EDITOR=notepad.exe)"
        )
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
    return (argv, process) if return_process else argv


def decode_for_editor(data):
    """``(bytes, encoding)`` - the text bytes an editor copy should contain.

    Valid UTF-8 is returned untouched (so the editor writes back exactly what
    was in the image). Otherwise the bytes are decoded as CP1251 / CP866 and
    re-encoded to UTF-8 with a BOM, which is what Блокнот needs to show
    Cyrillic correctly. Bytes that look binary (embedded NUL) are returned
    verbatim with the encoding ``"binary"``.
    """
    if b"\x00" in data:
        return data, "binary"
    try:
        data.decode("utf-8")
        return data, "utf-8"
    except UnicodeDecodeError:
        pass
    for encoding in FALLBACK_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return ("\ufeff" + text).encode("utf-8"), encoding
    return data, "binary"


def prepare_image_text(provider, path, dest_dir):
    """Extract `path` out of an image into `dest_dir` for the editor.

    Returns ``(host_path, encoding)``. The provider only needs ``read_file``.
    """
    data = provider.read_file(path)
    name = str(path).rstrip("/").rsplit("/", 1)[-1] or "file.txt"
    payload, encoding = decode_for_editor(data)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name(name)
    dest.write_bytes(payload)
    return dest, encoding
