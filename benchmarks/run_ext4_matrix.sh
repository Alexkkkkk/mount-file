#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Multi-size/multi-entry ext4 workload for issue #182.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/benchmarks/results/ext4-matrix-aarch64.json}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v mke2fs >/dev/null || { echo "mke2fs is required" >&2; exit 2; }
command -v debugfs >/dev/null || { echo "debugfs is required" >&2; exit 2; }
command -v e2fsck >/dev/null || { echo "e2fsck is required" >&2; exit 2; }

truncate -s 134217728 "$TMP/bench.img"
# Keep only ext4 structures validated by the built-in read-only provider.
mke2fs -q -F -t ext4 -b 4096 -I 256 \
    -O none,extents,filetype,sparse_super,large_file "$TMP/bench.img"
python3 - "$TMP" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
(root / "tiny.txt").write_bytes(b"MountFile synthetic ext4 benchmark\n")
for filename, size, multiplier, addend in (
    ("1m.bin", 1 << 20, 73, 17),
    ("8m.bin", 8 << 20, 131, 53),
):
    payload = bytes(
        (index * multiplier + (index >> 8) * 19 + addend) & 0xFF
        for index in range(1 << 20)
    )
    with (root / filename).open("wb") as stream:
        remaining = size
        while remaining:
            chunk = payload[: min(remaining, len(payload))]
            stream.write(chunk)
            remaining -= len(chunk)
for index in range(16):
    (root / f"small-{index:02d}.txt").write_text(
        f"entry {index:02d}: deterministic small-file fixture\n", encoding="ascii"
    )
PY
{
    printf 'mkdir /bench\nmkdir /bench/many\n'
    printf 'write %s /bench/tiny.txt\n' "$TMP/tiny.txt"
    printf 'write %s /bench/1m.bin\n' "$TMP/1m.bin"
    printf 'write %s /bench/8m.bin\n' "$TMP/8m.bin"
    for index in $(seq -w 0 15); do
        printf 'write %s /bench/many/small-%s.txt\n' "$TMP/small-$index.txt" "$index"
    done
} | debugfs -w -f - "$TMP/bench.img" >/dev/null 2>&1
e2fsck -fn "$TMP/bench.img" >/dev/null 2>&1
mkdir -p "$(dirname "$OUT")"
ARGS=(/bench/tiny.txt /bench/1m.bin /bench/8m.bin)
for index in $(seq -w 0 15); do ARGS+=("/bench/many/small-$index.txt"); done
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/benchmarks/bench_vfs.py" \
    "$TMP/bench.img" \
    --offset 0 \
    --volume-size 134217728 \
    --baseline both \
    --repeat 7 \
    --warmup 2 \
    --cache-state warm \
    --hash-image \
    --json-out "$OUT" \
    "${ARGS[@]}"
