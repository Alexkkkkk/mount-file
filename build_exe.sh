#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Build a standalone Linux/macOS binary with the selected Python environment.
# Usage: PYTHON=python3.12 NAME=MountFile-linux-x86-64 ./build_exe.sh
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
if [ -z "${NAME:-}" ]; then
    case "$(uname -s):$(uname -m)" in
        Linux:x86_64) NAME=MountFile-linux-x86-64 ;;
        Linux:aarch64|Linux:arm64) NAME=MountFile-linux-arm64 ;;
        Darwin:arm64) NAME=MountFile-macos-arm64 ;;
        Darwin:x86_64) NAME=MountFile-macos-x86-64 ;;
        *) echo "Unsupported build platform; set NAME explicitly." >&2; exit 2 ;;
    esac
fi
case "$NAME" in
    *[!a-zA-Z0-9._-]*|''|.|..) echo "NAME must be a plain output filename." >&2; exit 2 ;;
esac
# Namespace-only imports do not detect missing Qt shared libraries. Fail
# before building a binary that cannot create its mandatory interface.
"$PYTHON" -c 'from PyQt6 import QtCore, QtGui, QtWidgets; import mountfile'
# Reuse an installed builder: repeat/offline builds must not require a network
# connection just to upgrade tooling that already works.
if ! "$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1; then
    "$PYTHON" -m pip install 'pyinstaller>=6.22.3'
fi
EXTRA=(--hidden-import mountfile_dfir_tools)
if "$PYTHON" -c 'import pytsk3' >/dev/null 2>&1; then
    EXTRA+=(--collect-submodules pytsk3)
else
    echo 'Note: pytsk3 is absent; native readers remain available, legacy fallback is not bundled.'
fi
ICON="$("$PYTHON" -m mountfile.appicon)"
if [ -n "$ICON" ] && [ -f "$ICON" ]; then
    EXTRA+=(--icon "$ICON")
fi
# Keep generated specs out of the repository's hand-maintained root spec.
"$PYTHON" -m PyInstaller --noconfirm --clean --onefile --windowed --name "$NAME" \
    --specpath build/spec --collect-data mountfile "${EXTRA[@]}" main.py
if [ ! -s "dist/$NAME" ]; then
    echo 'Build did not produce a nonempty executable.' >&2
    exit 1
fi
echo "Done: dist/$NAME"
