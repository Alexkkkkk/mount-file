#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
# Build a standalone Linux x86-64 binary with PyInstaller.
#
# Usage:
#   ./build_exe.sh          (uses python3 from PATH)
#   PYTHON=python3.12 ./build_exe.sh
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
NAME="MountFile-linux-x86-64"

"$PYTHON" -m pip install --quiet --upgrade pyinstaller
"$PYTHON" -m PyInstaller --onefile --windowed --name "$NAME" \
    --collect-submodules pytsk3 \
    main.py

if [ -f "dist/$NAME" ]; then
    echo "Done: dist/$NAME"
else
    echo "Build failed, see messages above." >&2
    exit 1
fi
