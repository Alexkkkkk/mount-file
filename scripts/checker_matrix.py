#!/usr/bin/env python3
"""Report the external-checker matrix without mutating any image.

The report is intentionally a capability/availability inventory, not a parity
verdict. Real-image tests remain authoritative for a specific fixture and
operation; missing tools are reported as unavailable rather than passing.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import platform
import shutil
import subprocess
from importlib import metadata
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


def _discover_fixture_tests(tests_root: Path) -> set[str]:
    """Collect test function names without importing the test suite."""
    discovered: set[str] = set()
    for path in sorted(tests_root.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        discovered.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        )
    return discovered


def _tool_version(item: dict, executable: str | None) -> str | None:
    """Return a bounded version string without turning inventory into evidence."""
    if not executable:
        return None
    if item.get("checker_kind") == "python_module":
        module = importlib.util.find_spec(item["checker"])
        if module is None:
            return None
        try:
            return metadata.version(item.get("checker_package", item["checker"]))
        except metadata.PackageNotFoundError:
            return getattr(module, "version", None)
    for flag in ("--version", "-V"):
        try:
            result = subprocess.run(
                [executable, flag],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0 and output:
            return output.splitlines()[0][:240]
    return None


def build_report(tests_root: Path | None = None) -> dict[str, object]:
    tests_root = tests_root or Path(__file__).resolve().parents[1] / "tests"
    discovered_tests = _discover_fixture_tests(tests_root)
    entries = []
    for item in MATRIX:
        if item.get("checker_kind") == "python_module":
            module = importlib.util.find_spec(item["checker"])
            executable = module.origin if module else None
        else:
            executable = shutil.which(item["checker"])
        available = bool(executable)
        fixture_tests = list(item["fixture_tests"])
        missing_fixtures = [
            name for name in fixture_tests if name not in discovered_tests
        ]
        fixture_status = "fixture_missing" if missing_fixtures else "fixture_declared"
        evidence = "fixture_missing" if missing_fixtures else "fixture_required"
        entries.append(
            {
                **item,
                "fixture_status": fixture_status,
                "fixture_tests_found": [
                    name for name in fixture_tests if name in discovered_tests
                ],
                "fixture_tests_missing": missing_fixtures,
                "checker_path": executable,
                "checker_version": _tool_version(item, executable),
                "availability": "available" if available else "unavailable",
                "evidence": evidence,
                "parity": "not_claimed",
                "reason": (
                    "fixture-specific test exists but was not executed; inventory "
                    "alone is not parity"
                    if not missing_fixtures and available
                    else (
                        "declared fixture test is missing from the test suite"
                        if missing_fixtures
                        else "required checker/runtime is unavailable on this runner"
                    )
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
