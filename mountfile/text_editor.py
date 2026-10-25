# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Opening text files in the system editor («Блокнот»).

Total Commander opens a text file in the system editor, and Mount File does
the same through this module - the single place where the editor is chosen:

* Windows  - ``notepad.exe`` (Блокнот);
* macOS    - ``open -W -t`` (wait for the default text editor to exit);
* Linux    - the first wait-capable editor found on ``PATH`` (gedit or kate).

``$MOUNTFILE_EDITOR`` / ``$VISUAL`` / ``$EDITOR`` are honoured on **every**
POSIX host - macOS included - and win over the per-OS default above.

``.txt`` is the primary extension; the other plain-text kinds listed in
:data:`TEXT_EXTENSIONS` take the same route.

A file that lives inside an image cannot be handed to an external editor
directly, so :func:`prepare_image_text` writes a temporary copy on the host
first. After the user saves and closes the editor, the GUI can explicitly
offer to import changed bytes back into the image. Text bytes that are not
valid UTF-8 are re-encoded to UTF-8 with a BOM for the copy (CP1251 / CP866
are tried), otherwise Блокнот shows mojibake instead of Cyrillic.
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
    "has_active_editors",
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
#: Their explicit wait switches keep the temporary copy alive for the whole
#: editing session.  Do not fall back to desktop launchers such as xdg-open:
#: those normally exit as soon as the editor is started.
LINUX_EDITORS = (("gedit", "--wait"), ("kate", "--block"))

_DETACHED_LAUNCHERS = {"xdg-open", "gio", "kioclient", "kioclient5", "kioclient6"}
_WAIT_FLAGS = {
    "gedit": "--wait",
    "kate": "--block",
    "xed": "--wait",
    "pluma": "--wait",
    "code": "--wait",
    "code-insiders": "--wait",
    "codium": "--wait",
    "subl": "--wait",
    "sublime_text": "--wait",
}

#: Encodings tried for a non-UTF-8 copy, most likely first.
FALLBACK_ENCODINGS = ("cp1251", "cp866")

# Retain child handles for editor commands that stay alive until their window
# closes (including an explicitly configured ``--wait`` editor).
_EDITOR_PROCESSES = []


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
        executable = os.path.basename(from_env[0]).lower()
        if executable in _DETACHED_LAUNCHERS:
            return None
        if executable == "open" and sys.platform == "darwin":
            options = [option for option in ("-W", "-t") if option not in from_env]
            return [from_env[0], *options, *from_env[1:], str(path)]
        wait_flag = _WAIT_FLAGS.get(executable)
        if wait_flag and wait_flag not in from_env:
            from_env.insert(1, wait_flag)
        return from_env + [str(path)]
    if sys.platform == "darwin":  # pragma: no cover - macOS only
        return ["open", "-W", "-t", str(path)]
    for candidate, wait_flag in LINUX_EDITORS:
        exe = shutil.which(candidate)
        if exe:
            return [exe, wait_flag, str(path)]
    return None


def open_in_editor(path):
    """Start the system editor on `path` and return the argv used.

    The editor runs detached and the GUI never blocks waiting for it to exit.
    Raises :class:`RuntimeError` when no wait-capable editor is available.
    Waiting is required because image-backed files are temporary copies and
    must not be removed while the editor may still be using them.  Custom
    ``$EDITOR`` commands should include their editor's wait/block option.
    """
    argv = editor_command(path)
    if argv is None:
        raise RuntimeError(
            "текстовый редактор не найден: задайте MOUNTFILE_EDITOR "
            "(например, MOUNTFILE_EDITOR=notepad.exe)"
        )
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
    _EDITOR_PROCESSES.append((os.path.abspath(path), process))
    return argv


def has_active_editors(directory):
    """Whether a tracked editor process still uses a file under `directory`.

    Exited processes are pruned as they are checked.  Keeping the child alive
    is important for ``--wait`` editors: deleting an extracted copy while its
    editor is open would discard unsaved work.
    """
    root = os.path.realpath(directory)
    active = []
    still_running = []
    for path, process in _EDITOR_PROCESSES:
        if process.poll() is not None:
            continue
        still_running.append((path, process))
        try:
            if os.path.commonpath((root, os.path.realpath(path))) == root:
                active.append(path)
        except ValueError:  # different Windows drives
            continue
    _EDITOR_PROCESSES[:] = still_running
    return bool(active)


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


def prepare_image_text(provider, path, dest_dir, data=None):
    """Extract `path` out of an image into `dest_dir` for the editor.

    Returns ``(host_path, encoding)``. The provider only needs ``read_file``.
    Callers may pass a previously-read `data` snapshot to avoid a second read.
    """
    if data is None:
        data = provider.read_file(path)
    name = str(path).rstrip("/").rsplit("/", 1)[-1] or "file.txt"
    payload, encoding = decode_for_editor(data)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name(name)
    dest.write_bytes(payload)
    return dest, encoding
