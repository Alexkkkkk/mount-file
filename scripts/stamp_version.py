#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Synchronise the package and project versions with a stable release tag.

Usage::

    python scripts/stamp_version.py v1.2.3
    python scripts/stamp_version.py --check v1.2.3

Only ``mountfile/__init__.py``'s ``__version__`` assignment and the
``version`` key in the exact ``[project]`` TOML table are ever changed.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import sys
import tempfile
from typing import List, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Keep TARGET as the historical public module constant used by callers/tests.
TARGET = ROOT / "mountfile" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

# Release tags are deliberately stable SemVer only.  Restricting the input
# prevents a tag from injecting TOML/Python syntax into either source file.
VERSION_RE = re.compile(
    r"^(?:v)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
PACKAGE_VERSION_RE = re.compile(
    r"^(?P<prefix>\s*__version__\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"\r\n]*)(?P=quote)(?P<suffix>\s*(?:#.*)?)$"
)
PROJECT_HEADER_RE = re.compile(r"^\s*\[\s*project\s*\]\s*(?:#.*)?$")
TABLE_HEADER_RE = re.compile(r"^\s*\[\[?[^\]\r\n]+\]\]?\s*(?:#.*)?$")
PROJECT_VERSION_RE = re.compile(
    r"^(?P<prefix>\s*version\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"\r\n]*)(?P=quote)(?P<suffix>\s*(?:#.*)?)$"
)


class StampError(Exception):
    """Raised when either required version target cannot be handled safely."""


def _normalise_version(raw_version: str) -> str:
    match = VERSION_RE.fullmatch(raw_version.strip())
    if not match:
        raise StampError("refusing to stamp a malformed stable version/tag: %r" % raw_version)
    return ".".join(match.groups())


def _line_ending(line: str) -> Tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _replace_one_line(
    lines: List[str], indexes: List[int], pattern: re.Pattern, version: str, label: str
) -> Tuple[str, str]:
    if len(indexes) != 1:
        raise StampError("expected exactly one %s, found %d" % (label, len(indexes)))
    index = indexes[0]
    content, ending = _line_ending(lines[index])
    match = pattern.fullmatch(content)
    # The caller obtained index by matching this same pattern.  Keep a
    # defensive check so future changes cannot silently modify an unexpected
    # line.
    if match is None:
        raise StampError("could not read %s" % label)
    old_version = match.group("value")
    lines[index] = (
        match.group("prefix")
        + match.group("quote")
        + version
        + match.group("quote")
        + match.group("suffix")
        + ending
    )
    return "".join(lines), old_version


def _prepare_package(text: str, version: str) -> Tuple[str, str]:
    lines = text.splitlines(keepends=True)
    indexes = []
    for index, line in enumerate(lines):
        content, _ = _line_ending(line)
        if PACKAGE_VERSION_RE.fullmatch(content):
            indexes.append(index)
    return _replace_one_line(lines, indexes, PACKAGE_VERSION_RE, version, "__version__ assignment")


def _project_table_range(lines: List[str]) -> Tuple[int, int]:
    starts = []
    for index, line in enumerate(lines):
        content, _ = _line_ending(line)
        if PROJECT_HEADER_RE.fullmatch(content):
            starts.append(index)
    if len(starts) != 1:
        raise StampError("expected exactly one [project] table, found %d" % len(starts))

    start = starts[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        content, _ = _line_ending(lines[index])
        if TABLE_HEADER_RE.fullmatch(content):
            end = index
            break
    return start, end


def _prepare_project(text: str, version: str) -> Tuple[str, str]:
    lines = text.splitlines(keepends=True)
    start, end = _project_table_range(lines)
    indexes = []
    for index in range(start, end):
        content, _ = _line_ending(lines[index])
        if PROJECT_VERSION_RE.fullmatch(content):
            indexes.append(index)
    return _replace_one_line(
        lines, indexes, PROJECT_VERSION_RE, version, "version in [project]"
    )


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StampError("could not read %s: %s" % (path, exc))


def _atomic_write(path: pathlib.Path, content: str) -> None:
    """Replace one file atomically while retaining its permission bits."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
    except OSError as exc:
        raise StampError("could not prepare %s for writing: %s" % (path, exc))

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, str(path))
    except OSError as exc:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise StampError("could not write %s: %s" % (path, exc))


def _synchronise(version: str, check: bool) -> int:
    # Read and validate *both* targets before any write.  This prevents a
    # missing/misplaced TOML field from leaving the repository half-stamped.
    package_before = _read_text(TARGET)
    project_before = _read_text(PYPROJECT)
    package_after, package_version = _prepare_package(package_before, version)
    project_after, project_version = _prepare_project(project_before, version)

    if check:
        mismatches = []
        if package_version != version:
            mismatches.append("mountfile/__init__.py=%s" % package_version)
        if project_version != version:
            mismatches.append("pyproject.toml [project].version=%s" % project_version)
        if mismatches:
            print(
                "version check failed for %s: %s" % (version, ", ".join(mismatches)),
                file=sys.stderr,
            )
            return 1
        print("version check passed: both targets are %s" % version)
        return 0

    changes = []
    if package_after != package_before:
        changes.append((TARGET, package_before, package_after))
    if project_after != project_before:
        changes.append((PYPROJECT, project_before, project_after))
    if not changes:
        print("version %s is already stamped in both targets" % version)
        return 0

    written = []
    try:
        for path, _before, after in changes:
            _atomic_write(path, after)
            written.append((path, _before))
    except StampError as exc:
        # Cross-file replacement cannot be one filesystem transaction.  Make
        # the normal failure path restore any target already replaced.
        rollback_errors = []
        for path, before in reversed(written):
            try:
                _atomic_write(path, before)
            except StampError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        message = str(exc)
        if rollback_errors:
            message += "; rollback failed: " + "; ".join(rollback_errors)
        raise StampError(message)

    print("stamped mountfile/__init__.py and pyproject.toml with version %s" % version)
    return 0


def main(argv):
    """Run the script using the historical ``main(sys.argv)`` calling shape."""
    arguments = list(argv[1:])
    check = False
    if "--check" in arguments:
        if arguments.count("--check") != 1:
            print("usage: stamp_version.py [--check] <version|tag>", file=sys.stderr)
            return 2
        arguments.remove("--check")
        check = True
    if len(arguments) != 1:
        print("usage: stamp_version.py [--check] <version|tag>", file=sys.stderr)
        return 2

    try:
        version = _normalise_version(arguments[0])
    except StampError as exc:
        print("stamp_version.py: %s" % exc, file=sys.stderr)
        return 2
    try:
        return _synchronise(version, check)
    except StampError as exc:
        print("stamp_version.py: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
