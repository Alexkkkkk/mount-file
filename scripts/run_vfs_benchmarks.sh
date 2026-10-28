#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
"""Reproduce the read-only VFS benchmark published in docs/benchmarks.md.

Generates the deterministic synthetic ext2 fixture locally (no binaries are
committed) and runs benchmarks/bench_vfs.py against the native ext2 provider
and the pytsk3 baseline. The image is never opened for writing by the
benchmark itself.

Usage:
    scripts/run_vfs_benchmarks.sh [OUTPUT_DIR]

Environment overrides:
    PYTHON_BIN   python interpreter used for generation and the benchmark
                 (default: /home/ubuntu/venv-mf/bin/python when present,
                 otherwise python3)
"""

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${PWD}/vfs-benchmark-results}"
mkdir -p "${OUTPUT_DIR}"

if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x /home/ubuntu/venv-mf/bin/python ]; then
        PYTHON_BIN=/home/ubuntu/venv-mf/bin/python
    else
        PYTHON_BIN=python3
    fi
fi

IMAGE="${OUTPUT_DIR}/bench.ext2"

echo "== Generating synthetic ext2 fixture (deterministic) =="
"${PYTHON_BIN}" "${REPO_ROOT}/scripts/make_bench_ext2_image.py" --output "${IMAGE}"

# 32 MiB volume, offset 0, block size 4096 (see docs/benchmarks.md).
VOLUME_SIZE="$(stat -c %s "${IMAGE}")"

echo "== Running bench_vfs (native + pytsk3 baseline) =="
PYTHONPATH="${REPO_ROOT}" "${PYTHON_BIN}" "${REPO_ROOT}/benchmarks/bench_vfs.py" \
    "${IMAGE}" \
    /bench/tiny_1k.dat \
    /bench/small_16k.dat \
    /bench/small_64k.dat \
    /bench/medium_512k.dat \
    /bench/large_1m.dat \
    /bench/large_4m.dat \
    /bench/many/file_000.dat \
    /bench/many/file_008.dat \
    /bench/many/file_016.dat \
    --offset 0 \
    --volume-size "${VOLUME_SIZE}" \
    --baseline both \
    --repeat 5 \
    --warmup 1 \
    --cache-state warm \
    --hash-image \
    --json-out "${OUTPUT_DIR}/bench_report.json"

echo "== Report saved to ${OUTPUT_DIR}/bench_report.json =="
