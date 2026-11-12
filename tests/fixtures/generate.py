# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Генератор воспроизводимых файловых фикстур (issue #389).

Создаёт синтетические, но структурно валидные образы для профилей, которые
нужны матрице приёмки. Для «настоящих» оракулов используются внешние утилиты
(mkfs.fat, mke2fs, mkfs.ntfs), если они установлены; иначе фикстура помечается
как ``synthetic`` и проверки, требующие внешнего checker'а, пропускаются.

Запуск:  python -m tests.fixtures.generate --out tests/fixtures/images
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

BPS = 512


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_fat32_fixture(path: Path, size_mb: int = 64) -> dict:
    """Минимальный валидный образ FAT32 через mkfs.fat, если он доступен."""
    if shutil.which("mkfs.fat") is None:
        raise SystemExit("mkfs.fat not found; install dosfstools first")
    subprocess.run(
        ["mkfs.fat", "-F", "32", "-I", "-C", str(path), f"{size_mb}"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return {"filesystem": "FAT32", "generator": ["mkfs.fat -F 32 -C"]}


def make_ext4_fixture(path: Path, size_mb: int = 64) -> dict:
    """Минимальный валидный образ ext4 через mke2fs (без журнала replay-гейтов)."""
    if shutil.which("mke2fs") is None:
        raise SystemExit("mke2fs not found; install e2fsprogs first")
    subprocess.run(
        [
            "mke2fs",
            "-q",
            "-t",
            "ext4",
            "-b",
            "4096",
            "-I",
            "256",
            "-F",
            "-E",
            "lazy_itable_init=0,lazy_journal_init=0",
            str(path),
            f"{size_mb}M",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return {"filesystem": "EXT4", "generator": ["mke2fs -t ext4"]}


def make_ntfs_fixture(path: Path, size_mb: int = 32) -> dict:
    """Минимальный валидный образ NTFS через mkfs.ntfs."""
    if shutil.which("mkfs.ntfs") is None:
        raise SystemExit("mkfs.ntfs not found; install ntfs-3g first")
    subprocess.run(
        ["mkfs.ntfs", "-F", "-Q", "-c512", str(path), f"{size_mb}M"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return {"filesystem": "NTFS", "generator": ["mkfs.ntfs -F -Q -c512"]}


GENERATORS = {
    "fat32": make_fat32_fixture,
    "ext4": make_ext4_fixture,
    "ntfs": make_ntfs_fixture,
}


def write_manifest(path: Path, meta: dict) -> None:
    manifest = {
        "schema": "mountfile.filesystem-fixture/v1",
        "fixture": path.name,
        "kind": "real-generated",
        "image_size_bytes": path.stat().st_size,
        "image_sha256": _sha256(path),
        **meta,
    }
    sidecar = path.with_name(path.name + ".manifest.json")
    sidecar.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate filesystem fixtures")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--gzip", action="store_true", help="also write .img.gz copies")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS.items():
        image = out / f"{name}-generated.img"
        meta = generator(image)
        write_manifest(image, meta)
        if args.gzip:
            gz = image.with_suffix(".img.gz")
            with image.open("rb") as src, gzip.open(gz, "wb") as dst:
                shutil.copyfileobj(src, dst)
        print(f"generated {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
