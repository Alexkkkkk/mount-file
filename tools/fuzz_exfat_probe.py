#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Coverage-guided fuzz target for the bounded exFAT boot-region parser.

Run from the repository root with:
    uv run --extra fuzz python tools/fuzz_exfat_probe.py /tmp/mountfile-exfat-corpus -runs=10000

The corpus directory is created if needed and receives valid and malformed
seeds. Existing corpus entries are never overwritten.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SECTOR_SIZE = 512
_BOOT_REGION_SECTORS = 12
_MAX_INPUT_SIZE = 8 + 24 * 4096
_MAX_CLUSTER_SIZE = 32 * 1024 * 1024
_VOLUME_SIZE = 4096 * _SECTOR_SIZE


def _boot_checksum(data: bytes) -> int:
    checksum = 0
    for index, value in enumerate(data):
        if index in (106, 107, 112):
            continue
        checksum = ((checksum >> 1) | ((checksum & 1) << 31)) & 0xFFFFFFFF
        checksum = (checksum + value) & 0xFFFFFFFF
    return checksum


def _write_checksum(region: bytearray, start: int) -> None:
    checksum = _boot_checksum(bytes(region[start : start + 11 * _SECTOR_SIZE]))
    checksum_sector = start + 11 * _SECTOR_SIZE
    for offset in range(0, _SECTOR_SIZE, 4):
        struct.pack_into("<I", region, checksum_sector + offset, checksum)


def _valid_seed_input() -> bytes:
    region = bytearray(24 * _SECTOR_SIZE)
    region[3:11] = b"EXFAT   "
    struct.pack_into("<Q", region, 0x48, _VOLUME_SIZE // _SECTOR_SIZE)
    struct.pack_into("<I", region, 0x50, 24)
    struct.pack_into("<I", region, 0x54, 8)
    struct.pack_into("<I", region, 0x58, 32)
    struct.pack_into("<I", region, 0x5C, 100)
    struct.pack_into("<I", region, 0x60, 2)
    region[0x6C] = 9
    region[0x6D] = 0
    region[0x6E] = 1
    region[0x70] = 10
    region[510:512] = b"\x55\xaa"
    backup_start = 12 * _SECTOR_SIZE
    region[backup_start : backup_start + 11 * _SECTOR_SIZE] = region[
        : 11 * _SECTOR_SIZE
    ]
    _write_checksum(region, 0)
    _write_checksum(region, backup_start)
    return struct.pack("<Q", _VOLUME_SIZE) + bytes(region)


def _seed_corpus(corpus_dir: Path) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    valid = _valid_seed_input()
    region = bytearray(valid[8:])

    bad_main_checksum = bytearray(valid)
    bad_main_checksum[8 + 200] ^= 1

    bad_backup_geometry = bytearray(valid)
    backup_start = 12 * _SECTOR_SIZE
    struct.pack_into("<I", bad_backup_geometry, 8 + backup_start + 0x50, 25)
    backup_region = bytearray(bad_backup_geometry[8:])
    _write_checksum(backup_region, backup_start)
    bad_backup_geometry[8:] = backup_region

    too_small_volume = struct.pack("<Q", 1) + bytes(region)
    seeds = {
        "boot-region-valid.bin": valid,
        "boot-region-main-checksum.bin": bytes(bad_main_checksum),
        "boot-region-backup-geometry.bin": bytes(bad_backup_geometry),
        "boot-region-selected-volume-bound.bin": too_small_volume,
    }
    for name, data in seeds.items():
        destination = corpus_dir / name
        if not destination.exists():
            destination.write_bytes(data)


def _build_target(parse_region, probe_error):
    def test_one_input(data: bytes) -> None:
        raw = bytes(data)
        if len(raw) < 8 or len(raw) > _MAX_INPUT_SIZE:
            return
        volume_size = struct.unpack_from("<Q", raw, 0)[0]
        try:
            profile = parse_region(raw[8:], volume_size=volume_size)
        except probe_error:
            return

        if profile.partition_size != volume_size:
            raise AssertionError("exFAT profile lost the selected volume size")
        if profile.bytes_per_sector not in (512, 1024, 2048, 4096):
            raise AssertionError("exFAT profile returned an unsupported sector size")
        if (
            profile.cluster_size
            != profile.bytes_per_sector * profile.sectors_per_cluster
        ):
            raise AssertionError("exFAT cluster geometry is inconsistent")
        if not 0 < profile.cluster_size <= _MAX_CLUSTER_SIZE:
            raise AssertionError("exFAT profile escaped the supported cluster bound")
        if profile.volume_length * profile.bytes_per_sector > volume_size:
            raise AssertionError("exFAT volume exceeds the selected extent")
        if (
            profile.fat_offset + profile.fat_length * profile.number_of_fats
            > profile.volume_length
        ):
            raise AssertionError("exFAT FAT exceeds the declared volume")
        if (
            profile.cluster_heap_offset
            + profile.cluster_count * profile.sectors_per_cluster
            > profile.volume_length
        ):
            raise AssertionError("exFAT cluster heap exceeds the declared volume")
        if not 2 <= profile.root_cluster <= profile.cluster_count + 1:
            raise AssertionError("exFAT root cluster is outside the declared heap")
        if not 0 <= profile.percent_in_use <= 100:
            raise AssertionError("exFAT percent-in-use is outside its valid range")

    return test_one_input


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        print(
            "usage: fuzz_exfat_probe.py CORPUS_DIR [libFuzzer options, e.g. -runs=10000]",
            file=sys.stderr,
        )
        return 2

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    corpus_dir = Path(sys.argv[1]).expanduser().resolve()
    _seed_corpus(corpus_dir)

    try:
        import atheris
    except ImportError as exc:
        print(
            "Install the optional fuzz extra with: uv sync --extra fuzz",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    with atheris.instrument_imports():
        from mountfile.exfat_probe import ExfatProbeError, parse_exfat_boot_region

    target = _build_target(parse_exfat_boot_region, ExfatProbeError)
    atheris.Setup(sys.argv, target)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
