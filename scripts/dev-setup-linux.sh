#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
#
# One-shot Linux host adaptation for Mount File: installs the same system
# package set the CI matrix uses (plus the Qt runtime libraries needed by a
# headless shell), creates .venv, installs the project in editable mode with
# the dev extras and proves the checkout actually runs.
#
#   bash scripts/dev-setup-linux.sh          # full setup + smoke
#   bash scripts/dev-setup-linux.sh --no-tests   # skip the pytest gate
#
# Idempotent: re-running only refreshes packages and the virtualenv.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
RUN_TESTS=1
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n=== %s ===\n' "$*"; }

say "[1/5] system packages (tools/system-packages-debian.txt + Qt runtime)"
if command -v apt-get >/dev/null 2>&1; then
  PKGS="$(grep -vE '^[[:space:]]*(#|$)' tools/system-packages-debian.txt | tr '\n' ' ')"
  sudo apt-get update -qq
  # shellcheck disable=SC2086
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -qq -y $PKGS
else
  echo "apt-get not found: install the packages listed in tools/system-packages-debian.txt manually"
fi

say "[2/5] virtualenv (.venv)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$PY" -m venv "$ROOT/.venv"
fi
# shellcheck disable=SC1091
. "$ROOT/.venv/bin/activate"
python -m pip install --quiet --upgrade pip

say "[3/5] editable install with dev + optional runtime extras"
# api/ai/dfir/images are what the rest of the suite imports; installing them here
# is why a fresh host does not silently skip the REST, AI and DFIR tests.
EXTRAS="${MOUNTFILE_EXTRAS:-dev,api,ai,dfir,images}"
python -m pip install --quiet -e ".[${EXTRAS}]"

say "[4/5] smoke: version, diagnostics, headless GUI construction"
python main.py --version
# Qt needs an offscreen platform plugin on a machine without a display.
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" python main.py --diag
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" python - <<'PY'
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys

from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
from mountfile.mainwindow import MainWindow

window = MainWindow()
print(f"GUI ok: {type(window).__name__}")
app.quit()
PY

say "[5/5] gates"
python -m ruff check . --select E9,F63,F7,F82
python -m ruff check . --select B006,E722,S110,S112,F841
if [ "$RUN_TESTS" = "1" ]; then
  QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" \
    python -m pytest tests/ -q -n "${PYTEST_WORKERS:-auto}" --dist=loadfile \
    -m "not gui and not e2e"
fi

say "done: source .venv/bin/activate, then python main.py"
