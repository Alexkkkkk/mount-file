#!/usr/bin/env python3
"""Validate that the published capability matrix matches callable APIs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.capabilities import (
    CANONICAL_OPERATIONS,
    NATIVE_CAPABILITY_MATRIX,
    NATIVE_WRITE_PROFILES_BY_STRATEGY,
    api_operation_for_canonical_operation,
)
from mountfile.exfat_reader import NativeExfatImage
from mountfile.ext2_reader import NativeExt2Image
from mountfile.ext3_reader import NativeExt3Image, NativeExt4Image
from mountfile.fat_reader import NativeFatImage
from mountfile.ntfs_reader import NativeNtfsMetadataImage
from mountfile.universal_fs_rw import UniversalFSRW

VFS_TYPES = (
    NativeFatImage,
    NativeExt2Image,
    NativeExt3Image,
    NativeExt4Image,
    NativeExfatImage,
    NativeNtfsMetadataImage,
)
ENDPOINTS = {
    "vfs": {
        "list": "list_dir",
        "stat": "stat",
        "read-file": "read_file",
        "read-range": "read_file_range",
        "extract": "extract",
    },
    "universal-fs-rw": {
        "list": "list",
        "stat": "stat",
        "read-file": "read",
        "read-range": "read_range",
        "extract": "extract",
        "write-file": "write_file",
        "mkdir": "mkdir",
        "delete": "delete",
        "rename": "rename",
        "hard-link": "link",
    },
}


def validate_matrix() -> dict[str, object]:
    blockers: list[str] = []
    checked: list[dict[str, object]] = []
    for profile in NATIVE_CAPABILITY_MATRIX:
        aliases = ENDPOINTS[profile.interface]
        implementations = VFS_TYPES if profile.interface == "vfs" else (UniversalFSRW,)
        matrix = profile.operation_matrix(
            write_enabled=bool(profile.write_capabilities)
        )
        available_api = set(profile.read_capabilities) | set(profile.write_capabilities)
        if profile.interface == "vfs" and profile.write_capabilities:
            blockers.append(f"{profile.profile_id}:vfs-write-capability")
        if profile.interface == "universal-fs-rw":
            known = NATIVE_WRITE_PROFILES_BY_STRATEGY.get(profile.write_strategy)
            if known is not profile:
                blockers.append(f"{profile.profile_id}:strategy-profile-mismatch")
        for operation in CANONICAL_OPERATIONS:
            cell = matrix[operation]
            api_operation = api_operation_for_canonical_operation(
                profile.interface, operation
            )
            if cell["state"] == "available":
                if api_operation is None or api_operation not in available_api:
                    blockers.append(f"{profile.profile_id}:{operation}:matrix-mismatch")
                    continue
                endpoint = aliases.get(operation)
                if endpoint is None or not all(
                    callable(getattr(provider, endpoint, None))
                    for provider in implementations
                ):
                    blockers.append(f"{profile.profile_id}:{operation}:not-callable")
            else:
                refusal = cell.get("refusal") or {}
                if cell.get("api_operation") is not None:
                    blockers.append(f"{profile.profile_id}:{operation}:refused-has-api")
                if refusal.get("code") != "operation_not_supported":
                    blockers.append(f"{profile.profile_id}:{operation}:bad-refusal")
            checked.append(
                {
                    "profile": profile.profile_id,
                    "operation": operation,
                    "state": cell["state"],
                    "api_operation": cell.get("api_operation"),
                }
            )
    return {
        "schema": "mountfile.capability-conformance/v1",
        "verdict": "pass" if not blockers else "blocked",
        "profiles": len(NATIVE_CAPABILITY_MATRIX),
        "operations": list(CANONICAL_OPERATIONS),
        "cells_checked": len(checked),
        "blockers": blockers,
        "parity": "not_claimed",
        "fallback": "not_checked_by_this_command",
        "cells": checked,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = validate_matrix()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
