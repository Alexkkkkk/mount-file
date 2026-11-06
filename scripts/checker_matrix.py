#!/usr/bin/env python3
"""Report the external-checker matrix without mutating any image.

The report is intentionally a capability/availability inventory, not a parity
verdict. Real-image tests remain authoritative for a specific fixture and
operation; missing tools are reported as unavailable rather than passing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
from pathlib import Path

MATRIX = (
    {
        "profile": "ext2-native-rw",
        "native": True,
        "checker": "e2fsck",
        "checker_package": "e2fsprogs",
        "checker_authoritative": True,
        "fixture_tests": ["test_native_ext2_mutation_passes_e2fsck_on_disposable_copy"],
    },
    {
        "profile": "fat12-16-native-rw",
        "native": True,
        "checker": "fsck.fat",
        "checker_package": "dosfstools",
        "checker_authoritative": True,
        "fixture_tests": ["test_real_fat1216_mutations_pass_fsck_on_disposable_copy"],
    },
    {
        "profile": "fat32-native-rw",
        "native": True,
        "checker": "fsck.fat",
        "checker_package": "dosfstools",
        "checker_authoritative": True,
        "fixture_tests": ["test_native_fat32_mutation_passes_fsck_on_disposable_copy"],
    },
    {
        "profile": "exfat-native-rw",
        "native": True,
        "checker": "fsck.exfat",
        "checker_package": "exfatprogs",
        "checker_authoritative": True,
        "fixture_tests": [
            "test_real_exfat_unicode_write_and_rename_pass_read_only_fsck"
        ],
    },
    {
        "profile": "ntfs-native-ro",
        "native": False,
        "checker": "ntfsfix",
        "checker_package": "ntfs-3g",
        "checker_authoritative": False,
        "fixture_tests": ["test_real_mkfs_ntfs_is_never_declared_clean_by_ntfsfix"],
    },
    {
        "profile": "all-native-readers",
        "native": True,
        "checker": "pytsk3",
        "checker_package": "pytsk3",
        "checker_kind": "python_module",
        "checker_authoritative": False,
        "fixture_tests": [
            "test_real_ext2_benchmark_requires_identical_pytsk3_file_content"
        ],
    },
)


def build_report() -> dict[str, object]:
    entries = []
    for item in MATRIX:
        if item.get("checker_kind") == "python_module":
            module = importlib.util.find_spec(item["checker"])
            executable = module.origin if module else None
        else:
            executable = shutil.which(item["checker"])
        entries.append(
            {
                **item,
                "checker_path": executable,
                "availability": "available" if executable else "unavailable",
                "parity": "not_claimed",
                "reason": (
                    "fixture-specific test required; inventory alone is not parity"
                    if executable
                    else "required checker/runtime is unavailable on this runner"
                ),
            }
        )
    return {
        "schema": 1,
        "report": "mountfile-external-checker-matrix",
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "python": platform.python_version(),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = build_report()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
