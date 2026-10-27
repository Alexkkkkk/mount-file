#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible bounded VFS benchmark for issue #182.

The image is opened read-only. The benchmark records list/stat/range-read and
extract timings for the native provider and, when installed, the explicit
pytsk3 baseline. It never guesses a partition extent. Repeated samples and
warm-ups make timing variability visible; they do not certify filesystem
correctness or imply support for unimplemented profiles.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
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


def _run_one(
    factory, image: Path, offset: int, volume_size: int, paths: list[str]
) -> dict:
    provider = factory(image, offset, volume_size)
    try:
        with tempfile.TemporaryDirectory(prefix="mountfile-bench-") as tmp:
            return _measure(provider, paths, Path(tmp))
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def _summarize(samples: list[dict]) -> dict:
    durations = [sample["elapsed_seconds"] for sample in samples]
    median = statistics.median(durations)
    deviations = [abs(value - median) for value in durations]
    summary = dict(samples[-1])
    summary.update(
        {
            "elapsed_seconds": median,
            "elapsed_seconds_median": median,
            "elapsed_seconds_mad": statistics.median(deviations),
            "elapsed_seconds_min": min(durations),
            "elapsed_seconds_max": max(durations),
            "sample_count": len(samples),
            "samples": samples,
        }
    )
    return summary


def _benchmark(factory, image, offset, volume_size, paths, warmups, repeats):
    for _ in range(warmups):
        _run_one(factory, image, offset, volume_size, paths)
    samples = [
        _run_one(factory, image, offset, volume_size, paths) for _ in range(repeats)
    ]
    return _summarize(samples)


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
    parser.add_argument(
        "--warmups", type=int, default=1, help="untimed provider runs per baseline"
    )
    parser.add_argument("--repeat", type=int, default=5, help="timed runs per baseline")
    args = parser.parse_args()
    if args.offset < 0 or args.volume_size <= 0:
        parser.error("offset must be non-negative and volume-size must be positive")
    if args.warmups < 0 or args.repeat <= 0:
        parser.error("warmups must be non-negative and repeat must be positive")

    try:
        image_size = args.image.stat().st_size
    except OSError as exc:
        parser.error(f"cannot stat image: {exc}")
    if args.offset + args.volume_size > image_size:
        parser.error("selected volume extent exceeds image size")

    results = {
        "image": str(args.image),
        "image_size_bytes": image_size,
        "offset": args.offset,
        "volume_size": args.volume_size,
        "paths": args.paths,
        "warmups": args.warmups,
        "repeat": args.repeat,
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "results": {},
    }
    if args.baseline in ("native", "both"):
        try:
            results["results"]["native"] = _benchmark(
                lambda image, offset, size: open_image_provider(
                    image, offset=offset, volume_size=size
                ),
                args.image,
                args.offset,
                args.volume_size,
                args.paths,
                args.warmups,
                args.repeat,
            )
        except (OSError, TskError, ValueError) as exc:
            results["results"]["native_error"] = str(exc)
    if args.baseline in ("pytsk3", "both"):
        try:
            results["results"]["pytsk3"] = _benchmark(
                lambda image, offset, _size: TskImage(image, offset=offset),
                args.image,
                args.offset,
                args.volume_size,
                args.paths,
                args.warmups,
                args.repeat,
            )
        except (OSError, TskError, ValueError) as exc:
            results["results"]["pytsk3_error"] = str(exc)
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
