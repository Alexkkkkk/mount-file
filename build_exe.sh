#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
# Build a standalone Linux x86-64 binary with PyInstaller.
#
# Usage:
#   ./build_exe.sh                  (uses python3 from PATH)
#   PYTHON=python3.12 ./build_exe.sh
#   NAME=MountFile ./build_exe.sh   (custom output name)
#
# pytsk3 is optional: when it is missing the binary still browses and writes
# FAT32 images through the built-in engine, so the build continues.
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
NAME="${NAME:-MountFile-linux-x86-64}"

"$PYTHON" -m pip install --quiet --upgrade pyinstaller

EXTRA=()
if "$PYTHON" -c "import pytsk3" >/dev/null 2>&1; then
    EXTRA+=(--collect-submodules pytsk3)
else
    echo "Note: pytsk3 not installed - building with the built-in FAT32 engine only."
fi

# App icon: platform-specific file, taken from the bundled artwork
# (mountfile/data). icon.png is also what the window/taskbar icon uses at
# run time. Collecting package data also ships the XOR tables required by
# RT809H decoding in frozen builds.
ICON="$("$PYTHON" -m mountfile.appicon 2>/dev/null || true)"
if [ -n "$ICON" ] && [ -f "$ICON" ]; then
    EXTRA+=(--icon "$ICON")
    echo "Icon: $ICON"
else
    echo "Note: no app icon found - building with the default PyInstaller icon."
fi

"$PYTHON" -m PyInstaller --onefile --windowed --name "$NAME" \
    --collect-data mountfile \
    "${EXTRA[@]}" main.py

if [ -f "dist/$NAME" ]; then
    echo "Done: dist/$NAME"
else
    echo "Build failed, see messages above." >&2
    exit 1
fi
