#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Reproducible small ext2 comparison fixture for issue #182.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/benchmarks/results/ext2-10mib-aarch64.json}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v mke2fs >/dev/null || { echo "mke2fs is required" >&2; exit 2; }
command -v debugfs >/dev/null || { echo "debugfs is required" >&2; exit 2; }

truncate -s 67108864 "$TMP/bench.img"
# dir_index is disabled because the built-in ext2 reader deliberately rejects it.
mke2fs -q -F -t ext2 -b 4096 -I 256 -O '^dir_index' "$TMP/bench.img"
printf 'mkdir /bench\n' | debugfs -w -f - "$TMP/bench.img" >/dev/null 2>&1
python3 - "$TMP/payload.bin" <<'PY'
import sys

block = bytes((index * 31 + 7) & 0xFF for index in range(1024 * 1024))
with open(sys.argv[1], "wb") as stream:
    for _ in range(10):
        stream.write(block)
PY
printf 'write %s /bench/payload.bin\n' "$TMP/payload.bin" \
    | debugfs -w -f - "$TMP/bench.img" >/dev/null 2>&1
mkdir -p "$(dirname "$OUT")"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/benchmarks/bench_vfs.py" \
    "$TMP/bench.img" \
    --offset 0 \
    --volume-size 67108864 \
    --baseline both \
    --repeat 7 \
    --warmup 2 \
    --cache-state warm \
    --hash-image \
    --json-out "$OUT" \
    /bench/payload.bin
