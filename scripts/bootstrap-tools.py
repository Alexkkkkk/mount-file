#!/usr/bin/env python3
"""Verify the optional Python and external tools used by the full matrix."""

from __future__ import annotations

import importlib.util
import shutil
import sys


PYTHON_MODULES = {
    "PIL": "Pillow",
    "PyQt6": "PyQt6",
    "hypothesis": "hypothesis",
    "pytsk3": "pytsk3",
    "yara": "yara-python",
}
SYSTEM_TOOLS = (
    "mke2fs",
    "e2fsck",
    "mkfs.fat",
    "fsck.fat",
    "mkfs.exfat",
    "fsck.exfat",
    "mkfs.ntfs",
    "ntfsfix",
    "mcopy",
    "mdir",
    "mtype",
)


def main() -> int:
    missing = []
    for module, package in PYTHON_MODULES.items():
        available = importlib.util.find_spec(module) is not None
        print(f"python {module}: {'ok' if available else 'missing'} ({package})")
        if not available:
            missing.append(package)
    for tool in SYSTEM_TOOLS:
        available = shutil.which(tool) is not None
        print(f"system {tool}: {'ok' if available else 'missing'}")
        if not available:
            missing.append(tool)
    if missing:
        print("missing tools:", ", ".join(missing), file=sys.stderr)
        return 1
    print("all MountFile tools are available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
