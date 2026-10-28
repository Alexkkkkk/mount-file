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
import struct
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
_FIXTURE_TIME = 1_700_000_000
_FIXTURE_UUID = bytes.fromhex("6d6f756e7466696c652d62656e636831")
_FIXTURE_HASH_SEED = bytes.fromhex("00112233445566778899aabbccddeeff")


def _is_power_of(value: int, base: int) -> bool:
    if value < 1:
        return False
    while value > 1:
        if value % base:
            return False
        value //= base
    return True


def _has_super_backup(group: int, sparse_super: bool) -> bool:
    if group in (0, 1) or not sparse_super:
        return True
    return any(_is_power_of(group, base) for base in (3, 5, 7))


def _normalise_ext2_metadata(image_path: Path) -> None:
    """Fix mke2fs/writer clock and random metadata for a stable fixture.

    This touches only fixture metadata after all native writer operations. It
    is intentionally local to the benchmark generator and never changes the
    production writer's timestamp or UUID behavior.
    """
    image = bytearray(image_path.read_bytes())
    super_offset = 1024
    if len(image) < super_offset + 1024:
        raise RuntimeError("mke2fs produced a truncated ext2 superblock")

    def u32(offset: int) -> int:
        return struct.unpack_from("<I", image, offset)[0]

    def put_u32(offset: int, value: int) -> None:
        struct.pack_into("<I", image, offset, value)

    inodes_count = u32(super_offset)
    blocks_count = u32(super_offset + 4)
    first_data_block = u32(super_offset + 20)
    block_size = 1024 << u32(super_offset + 24)
    blocks_per_group = u32(super_offset + 32)
    inodes_per_group = u32(super_offset + 40)
    inode_size = struct.unpack_from("<H", image, super_offset + 88)[0] or 128
    sparse_super = bool(u32(super_offset + 100) & 0x0001)
    if not all((blocks_count, block_size, blocks_per_group, inodes_per_group)):
        raise RuntimeError("mke2fs produced invalid ext2 geometry")
    if inode_size < 128 or inode_size % 4:
        raise RuntimeError("mke2fs produced an unsupported ext2 inode size")

    groups = (
        blocks_count - first_data_block + blocks_per_group - 1
    ) // blocks_per_group

    def normalise_super(offset: int) -> None:
        if offset < 0 or offset + 1024 > len(image):
            raise RuntimeError("ext2 superblock backup is outside the fixture")
        for relative in (44, 48, 64, 264):
            put_u32(offset + relative, _FIXTURE_TIME)
        image[offset + 104 : offset + 120] = _FIXTURE_UUID
        image[offset + 236 : offset + 252] = _FIXTURE_HASH_SEED

    normalise_super(super_offset)
    for group in range(1, groups):
        if _has_super_backup(group, sparse_super):
            normalise_super(group * blocks_per_group * block_size)

    gdt_offset = (first_data_block + 1) * block_size
    for group in range(groups):
        descriptor = gdt_offset + group * 32
        inode_table = u32(descriptor + 8)
        inode_base = inode_table * block_size
        inode_count = min(inodes_per_group, inodes_count - group * inodes_per_group)
        for index in range(inode_count):
            inode = inode_base + index * inode_size
            if inode < 0 or inode + inode_size > len(image):
                raise RuntimeError("ext2 inode table is outside the fixture")
            timestamps = (8, 12, 16, 144) if inode_size >= 148 else (8, 12, 16)
            for relative in timestamps:
                put_u32(inode + relative, _FIXTURE_TIME)

    image_path.write_bytes(image)


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
    _normalise_ext2_metadata(output)
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
