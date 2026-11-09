#!/usr/bin/env bash
set -euo pipefail

# Install the complete local checker environment without committing foreign
# binaries to the repository. The same manifests are used by contributors and
# can be consumed by Debian/Ubuntu CI runners.
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

if [[ "${EUID}" -ne 0 ]]; then
  SUDO=${SUDO:-sudo}
else
  SUDO=""
fi

if [[ "${SKIP_SYSTEM_TOOLS:-0}" != "1" ]]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "error: this bootstrap installs system tools only on Debian/Ubuntu" >&2
    echo "set SKIP_SYSTEM_TOOLS=1 to install Python tools only" >&2
    exit 2
  fi
  mapfile -t packages < <(sed -E '/^[[:space:]]*(#|$)/d' tools/system-packages-debian.txt)
  ${SUDO} apt-get update
  ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -r requirements-all.txt

if [[ "${SKIP_VERIFY:-0}" != "1" ]]; then
  verify_args=()
  if [[ "${SKIP_SYSTEM_TOOLS:-0}" == "1" ]]; then
    verify_args+=(--skip-system-tools)
  fi
  "${PYTHON_BIN}" scripts/bootstrap-tools.py "${verify_args[@]}"
fi
