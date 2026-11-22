#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""E1 draft: emit a typed Windows checker inventory (UNVERIFIED).

The acceptance tracker cannot distinguish ``unavailable`` from ``pass``
without knowing which checker binaries exist on the evidence-producing host
and what each of them can actually verify. This draft emits
``windows-checker-inventory.json``: on a non-Windows host it is a typed
``unavailable`` artifact (never a pass); on Windows it records the checker
binaries found, their versions and an explicit capability map. The capability
descriptions start as declared constants that the first real Windows run must
confirm; nothing here upgrades itself to ``pass`` without an inventory.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path

SCHEMA = "mountfile.windows-checker-inventory/v0-draft"

# Declared capability map (draft): confirmed by the first real Windows run,
# corrected there if the actual checker output disagrees.
CHECKERS = (
    {
        "name": "chkdsk.exe",
        "binary": "chkdsk.exe",
        "authority": "windows_native",
        "verifies": [
            "NTFS metadata consistency after repair on a mounted volume",
        ],
        "does_not_verify": [
            "native replay parity by itself",
            "power-loss durability",
        ],
    },
    {
        "name": "ntfsfix (ntfs-3g)",
        "binary": "ntfsfix",
        "authority": "secondary_only_not_chkdsk_parity",
        "verifies": [
            "basic NTFS consistency (fixing common minor issues)",
        ],
        "does_not_verify": [
            "Windows/chkdsk parity",
        ],
    },
)


def _version_of(binary: str) -> str | None:
    if shutil.which(binary) is None:
        return None
    try:
        probe = subprocess.run(
            [binary, "/?" if binary.endswith(".exe") else "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unresolvable"
    return (
        (probe.stdout or probe.stderr).strip().splitlines()[0][:200]
        if (probe.stdout or probe.stderr)
        else "unresolvable"
    )


def build_inventory() -> dict[str, object]:
    is_windows = platform.system() == "Windows"
    checkers: list[dict[str, object]] = []
    for spec in CHECKERS:
        version = _version_of(spec["binary"]) if is_windows else None
        checkers.append(
            {
                "name": spec["name"],
                "binary": spec["binary"],
                "authority": spec["authority"],
                "present": version is not None,
                "version": version,
                "verifies": list(spec["verifies"]),
                "does_not_verify": list(spec["does_not_verify"]),
            }
        )
    present = [item for item in checkers if item["present"]]
    return {
        "schema": SCHEMA,
        "status": "available" if present else "unavailable",
        "host": platform.system(),
        "reason": None if present else "no checker binary found on this host",
        "checkers": checkers,
        "claims": [
            "An empty or unavailable inventory is typed unavailable, never pass.",
            "Capability descriptions are declared constants until a real Windows run confirms them.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, default=Path("windows-checker-inventory.json")
    )
    args = parser.parse_args()
    inventory = build_inventory()
    args.out.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
