#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate a deterministic synthetic ext2 fixture image for VFS benchmarks.

This is a fixture formatter for benchmark reproducibility only (issue #182).
It formats a fresh image with ``mke2fs -t ext2`` and populates it through the
project's own ``NativeExt2ImageWriter`` (staged, hash-verified writes), so the
generation path keeps the same integrity gates as the rest of the project.
No binaries are committed; run this script to rebuild the image locally.

Usage:
    python scripts/make_bench_ext2_image.py --output /tmp/bench.ext2
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mountfile.ext2_native import NativeExt2ImageWriter  # noqa: E402

# (relative path under /bench, size in bytes)
BENCH_FILES: list[tuple[str, int]] = [
    ("tiny_1k.dat", 1 << 10),
    ("small_16k.dat", 16 << 10),
    ("small_64k.dat", 64 << 10),
    ("medium_512k.dat", 512 << 10),
    ("large_1m.dat", 1 << 20),
    # 4 MiB is the practical maximum: the native ext2 writer stores files in
    # direct blocks plus one single-indirect block (~4 MiB at 4 KiB blocks).
    ("large_4m.dat", 4 << 20),
]
MANY_DIR_FILES = 24
MANY_FILE_SIZE = 4 << 10
DEFAULT_IMAGE_MIB = 32
DEFAULT_BLOCK_SIZE = 4096


def deterministic_payload(seed: str, size: int) -> bytes:
    """Incompressible-ish, reproducible content derived from a named seed."""
    chunks = []
    counter = 0
    while sum(len(c) for c in chunks) < size:
        chunks.append(hashlib.sha256(f"{seed}:{counter}".encode()).digest() * 64)
        counter += 1
    return b"".join(chunks)[:size]


def make_image(output: Path, image_mib: int, block_size: int) -> Path:
    if shutil.which("mke2fs") is None:
        raise SystemExit("mke2fs is required to format the fixture image")
    output = output.expanduser().resolve()
    if output.exists():
        output.unlink()
    output.write_bytes(b"\x00" * (image_mib << 20))
    subprocess.run(
        [
            "mke2fs",
            "-q",
            "-F",
            "-t",
            "ext2",
            "-b",
            str(block_size),
            "-O",
            "none,filetype,sparse_super,large_file",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    writer = NativeExt2ImageWriter(output)
    try:
        writer.mkdir("/bench")
        writer.mkdir("/bench/many")
        for name, size in BENCH_FILES:
            writer.write_file(f"/bench/{name}", deterministic_payload(name, size))
        for index in range(MANY_DIR_FILES):
            name = f"file_{index:03d}.dat"
            writer.write_file(
                f"/bench/many/{name}",
                deterministic_payload(name, MANY_FILE_SIZE),
            )
    finally:
        writer.close()
    return output


def bench_paths() -> list[str]:
    paths = [f"/bench/{name}" for name, _size in BENCH_FILES]
    paths += [
        f"/bench/many/file_{index:03d}.dat" for index in range(0, MANY_DIR_FILES, 8)
    ]
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size-mib", type=int, default=DEFAULT_IMAGE_MIB)
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument("--print-paths", action="store_true")
    args = parser.parse_args()

    image = make_image(args.output, args.size_mib, args.block_size)
    print(f"image: {image} ({args.size_mib} MiB, block {args.block_size})")
    print("paths:")
    for path in bench_paths():
        print(f"  {path}")
    if args.print_paths:
        print(" ".join(bench_paths()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
