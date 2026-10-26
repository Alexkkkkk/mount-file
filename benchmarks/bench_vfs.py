#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible bounded VFS benchmark for issue #182.

The image is opened read-only. The benchmark records list/stat/range-read and
extract timings for the native provider and, when installed, the explicit
pytsk3 baseline. It never guesses a partition extent.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from mountfile.extract import extract_file
from mountfile.vfs import TskError, TskImage, open_image_provider


def _measure(provider, paths: list[str], extract_root: Path) -> dict:
    started = time.perf_counter()
    listed = 0
    stats = 0
    bytes_read = 0
    extracted = 0
    for path in paths:
        parent = str(Path(path).parent).replace(".", "/") or "/"
        if parent == "//":
            parent = "/"
        listed += len(provider.list_dir(parent))
        stats += 1
        size = int(provider.file_size(path))
        amount = min(size, 1 << 20)
        if amount:
            bytes_read += len(provider.read_file_range(path, 0, amount))
        destination = extract_root / (path.strip("/").replace("/", "_") or "root")
        extract_file(provider, path, destination)
        extracted += 1
    return {
        "elapsed_seconds": time.perf_counter() - started,
        "paths": len(paths),
        "listed_entries": listed,
        "stat_operations": stats,
        "range_read_bytes": bytes_read,
        "extracted_files": extracted,
        "backend": getattr(provider, "backend_name", type(provider).__name__),
    }


def _run_one(factory, image: Path, offset: int, volume_size: int, paths: list[str]) -> dict:
    provider = factory(image, offset, volume_size)
    try:
        with tempfile.TemporaryDirectory(prefix="mountfile-bench-") as tmp:
            return _measure(provider, paths, Path(tmp))
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("paths", nargs="+", help="regular files to benchmark")
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--volume-size", type=int, required=True)
    parser.add_argument(
        "--baseline",
        choices=("native", "pytsk3", "both"),
        default="both",
    )
    args = parser.parse_args()
    if args.offset < 0 or args.volume_size <= 0:
        parser.error("offset must be non-negative and volume-size must be positive")

    results = {
        "image": str(args.image),
        "offset": args.offset,
        "volume_size": args.volume_size,
        "paths": args.paths,
        "results": {},
    }
    if args.baseline in ("native", "both"):
        try:
            results["results"]["native"] = _run_one(
                lambda image, offset, size: open_image_provider(
                    image, offset=offset, volume_size=size
                ),
                args.image,
                args.offset,
                args.volume_size,
                args.paths,
            )
        except (OSError, TskError, ValueError) as exc:
            results["results"]["native_error"] = str(exc)
    if args.baseline in ("pytsk3", "both"):
        try:
            results["results"]["pytsk3"] = _run_one(
                lambda image, offset, _size: TskImage(image, offset=offset),
                args.image,
                args.offset,
                args.volume_size,
                args.paths,
            )
        except (OSError, TskError, ValueError) as exc:
            results["results"]["pytsk3_error"] = str(exc)
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
