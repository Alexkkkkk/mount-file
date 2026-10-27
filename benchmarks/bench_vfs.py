#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible, read-only VFS benchmark for issue #182.

Measures directory listing, file-size/stat, bounded range reads and full-file
extraction against a native provider and/or the explicit pytsk3 baseline. The
source image is never opened for writing. A selected volume extent is mandatory.
The harness does not manipulate the host page cache: cache policy is reported,
not inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from mountfile.extract import extract_file
from mountfile.vfs import TskError, TskImage, open_image_provider

_SCHEMA_VERSION = 2
_RANGE_LIMIT = 1 << 20


def _validate_paths(paths: list[str]) -> list[str]:
    """Require absolute, normalized VFS paths and reject traversal components."""
    normalized = []
    for raw in paths:
        if not raw or "\x00" in raw:
            raise ValueError("benchmark paths must be non-empty and contain no NUL")
        path = PurePosixPath(raw)
        if not path.is_absolute() or any(
            part in (".", "..") for part in raw.split("/")
        ):
            raise ValueError(
                f"benchmark path must be absolute and traversal-free: {raw!r}"
            )
        canonical = path.as_posix()
        if canonical != raw.rstrip("/") and not (raw == "/" and canonical == "/"):
            raise ValueError(f"benchmark path must be normalized: {raw!r}")
        normalized.append(canonical)
    if len(set(normalized)) != len(normalized):
        raise ValueError("benchmark paths must be unique")
    return normalized


def _provider_profile(provider) -> dict:
    profile = getattr(provider, "profile", None)
    fields = (
        "fs_type",
        "block_size",
        "bytes_per_sector",
        "cluster_size",
        "cluster_count",
        "blocks_count",
        "inodes_count",
        "volume_serial",
    )
    result = {}
    for field in fields:
        value = getattr(profile, field, None) if profile is not None else None
        if isinstance(value, (str, int, float, bool)):
            result[field] = value
    result["backend"] = getattr(provider, "backend_name", type(provider).__name__)
    return result


def _quantile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return ordered[index]


def _summarize(samples: list[float]) -> dict:
    if not samples:
        return {
            "samples": 0,
            "min_seconds": None,
            "median_seconds": None,
            "p95_seconds": None,
            "max_seconds": None,
        }
    return {
        "samples": len(samples),
        "min_seconds": min(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds": _quantile(samples, 0.95),
        "max_seconds": max(samples),
    }


def _timed(call: Callable):
    started = time.perf_counter()
    value = call()
    return value, time.perf_counter() - started


def _directory(path: str) -> str:
    return PurePosixPath(path).parent.as_posix() or "/"


def _iteration(provider, paths: list[str]) -> dict:
    iteration_started = time.perf_counter()
    samples: dict[str, list[float]] = {
        "list_dir": [],
        "stat": [],
        "read_range": [],
        "extract": [],
    }
    file_sizes = {}
    range_bytes = 0
    extracted_bytes = 0
    listed_entries = 0

    for directory in sorted({_directory(path) for path in paths}):
        entries, elapsed = _timed(lambda d=directory: provider.list_dir(d))
        samples["list_dir"].append(elapsed)
        listed_entries += len(entries)

    with tempfile.TemporaryDirectory(prefix="mountfile-bench-") as tmp:
        output_root = Path(tmp)
        for index, path in enumerate(paths):
            metadata, elapsed = _timed(lambda p=path: provider.stat(p))
            if metadata.get("is_dir"):
                raise ValueError(
                    f"benchmark path is a directory, not a regular file: {path!r}"
                )
            size = int(metadata["size"])
            if size < 0:
                raise ValueError(f"provider returned negative file size for {path!r}")
            samples["stat"].append(elapsed)
            file_sizes[path] = size

            requested = min(size, _RANGE_LIMIT)
            if requested:
                data, elapsed = _timed(
                    lambda p=path, n=requested: provider.read_file_range(p, 0, n)
                )
                if len(data) != requested:
                    raise ValueError(
                        f"short range read for {path!r}: expected {requested}, got {len(data)}"
                    )
                samples["read_range"].append(elapsed)
                range_bytes += len(data)

            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
            destination = output_root / f"{index:05d}-{digest}.bin"
            written, elapsed = _timed(
                lambda p=path, dst=destination: extract_file(provider, p, dst)
            )
            if written != size or destination.stat().st_size != size:
                raise ValueError(
                    f"extraction size mismatch for {path!r}: expected {size}, got {written}"
                )
            samples["extract"].append(elapsed)
            extracted_bytes += written

    return {
        "operation_seconds": samples,
        "operation_counts": {name: len(values) for name, values in samples.items()},
        "file_sizes_bytes": file_sizes,
        "listed_entries": listed_entries,
        "range_read_bytes": range_bytes,
        "extracted_bytes": extracted_bytes,
        "elapsed_seconds": time.perf_counter() - iteration_started,
    }


def _run_backend(
    factory: Callable,
    image: Path,
    offset: int,
    volume_size: int,
    paths: list[str],
    repeats: int,
    warmups: int,
) -> dict:
    runs = []
    open_samples = []
    provider_info = None
    for run_index in range(warmups + repeats):
        provider, open_elapsed = _timed(lambda: factory(image, offset, volume_size))
        try:
            if provider_info is None:
                provider_info = _provider_profile(provider)
            measured = _iteration(provider, paths)
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        if run_index >= warmups:
            runs.append(measured)
            open_samples.append(open_elapsed)

    operation_summaries = {}
    for operation in ("list_dir", "stat", "read_range", "extract"):
        flat_samples = [
            elapsed for run in runs for elapsed in run["operation_seconds"][operation]
        ]
        operation_summaries[operation] = {
            **_summarize(flat_samples),
            "calls_per_iteration": runs[0]["operation_counts"][operation],
            "median_iteration_total_seconds": statistics.median(
                sum(run["operation_seconds"][operation]) for run in runs
            ),
        }
    return {
        **(provider_info or {}),
        "open_seconds": _summarize(open_samples),
        "median_iteration_seconds": statistics.median(
            run["elapsed_seconds"] for run in runs
        ),
        "operations": operation_summaries,
        "iterations": runs,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _open_native_provider(image: Path, offset: int, volume_size: int):
    provider = open_image_provider(image, offset=offset, volume_size=volume_size)
    if getattr(provider, "backend_name", "") == "pytsk3":
        provider.close()
        raise TskError(
            "native provider unavailable; routing selected the pytsk3 fallback"
        )
    return provider


def build_report(
    image: Path,
    offset: int,
    volume_size: int,
    paths: list[str],
    baseline: str = "both",
    repeats: int = 5,
    warmups: int = 1,
    cache_state: str = "unspecified",
    hash_image: bool = False,
) -> dict:
    """Run requested providers and return a JSON-serializable result document."""
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise ValueError(f"image is not a regular file: {image}")
    if offset < 0 or volume_size <= 0:
        raise ValueError("offset must be non-negative and volume-size positive")
    image_size = image.stat().st_size
    if offset + volume_size > image_size:
        raise ValueError("selected volume extent exceeds image bounds")
    if repeats < 1 or warmups < 0:
        raise ValueError("repeats must be >= 1 and warmups must be >= 0")
    paths = _validate_paths(paths)

    report = {
        "schema_version": _SCHEMA_VERSION,
        "environment": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "image": {
            "path": str(image),
            "size_bytes": image_size,
            "sha256": None,
        },
        "extent": {"offset_bytes": offset, "volume_size_bytes": volume_size},
        "workload": {
            "paths": paths,
            "directories": sorted({_directory(path) for path in paths}),
            "range_read_limit_bytes_per_file": _RANGE_LIMIT,
            "extraction": "complete file, temporary destination, size verified",
            "stat_implementation": "provider.stat; shared adapter may traverse list_dir components",
            "repeats": repeats,
            "warmups": warmups,
        },
        "cache_state": {
            "label": cache_state,
            "managed_by_harness": False,
            "note": "The harness does not flush or control the operating-system page cache.",
        },
        "backends": {},
    }

    if baseline in ("native", "both"):
        try:
            report["backends"]["native"] = _run_backend(
                _open_native_provider,
                image,
                offset,
                volume_size,
                paths,
                repeats,
                warmups,
            )
        except (OSError, TskError, ValueError) as exc:
            report["backends"]["native_error"] = str(exc)
    if baseline in ("pytsk3", "both"):
        try:
            report["backends"]["pytsk3"] = _run_backend(
                lambda path, start, _size: TskImage(path, offset=start),
                image,
                offset,
                volume_size,
                paths,
                repeats,
                warmups,
            )
        except (OSError, TskError, ValueError) as exc:
            report["backends"]["pytsk3_error"] = str(exc)
    native = report["backends"].get("native")
    pytsk3 = report["backends"].get("pytsk3")
    if native is not None and pytsk3 is not None:
        ratio = native["median_iteration_seconds"] / max(
            pytsk3["median_iteration_seconds"], 1e-12
        )
        change_percent = (ratio - 1.0) * 100.0
        report["comparison"] = {
            "baseline": "pytsk3",
            "metric": "median full-iteration wall time; provider open excluded",
            "native_to_baseline_ratio": ratio,
            "relative_change_percent": change_percent,
            "target_max_regression_percent": 20,
            "within_target_for_this_run": change_percent <= 20,
            "note": "This comparison is descriptive; publish repeated results and cache methodology before claiming benchmark acceptance.",
        }
    if hash_image:
        report["image"]["sha256"] = _sha256_file(image)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("paths", nargs="+", help="absolute VFS paths to regular files")
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--volume-size", type=int, required=True)
    parser.add_argument(
        "--baseline", choices=("native", "pytsk3", "both"), default="both"
    )
    parser.add_argument(
        "--repeat", type=int, default=5, help="measured iterations per backend"
    )
    parser.add_argument(
        "--warmup", type=int, default=1, help="discarded warmup iterations"
    )
    parser.add_argument(
        "--cache-state",
        choices=("unspecified", "warm"),
        default="unspecified",
        help="label the externally controlled cache condition; cache is not manipulated",
    )
    parser.add_argument(
        "--hash-image", action="store_true", help="record SHA-256 of the full image"
    )
    parser.add_argument(
        "--json-out", type=Path, help="also save the report to this JSON file"
    )
    args = parser.parse_args()

    try:
        report = build_report(
            args.image,
            args.offset,
            args.volume_size,
            args.paths,
            baseline=args.baseline,
            repeats=args.repeat,
            warmups=args.warmup,
            cache_state=args.cache_state,
            hash_image=args.hash_image,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
