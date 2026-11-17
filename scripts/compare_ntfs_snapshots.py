# SPDX-License-Identifier: MIT
"""Compare two raw NTFS metadata snapshot directories byte-for-byte."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def compare_file(before: Path, after: Path) -> dict:
    left = before.read_bytes()
    right = after.read_bytes()
    differing = sum(a != b for a, b in zip(left, right))
    differing += abs(len(left) - len(right))
    first = next(
        (index for index, (a, b) in enumerate(zip(left, right)) if a != b), None
    )
    if first is None and len(left) != len(right):
        first = min(len(left), len(right))
    return {
        "before_size": len(left),
        "after_size": len(right),
        "before_sha256": hashlib.sha256(left).hexdigest(),
        "after_sha256": hashlib.sha256(right).hexdigest(),
        "differing_bytes": differing,
        "first_difference": first,
        "byte_identical": left == right,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema": "mountfile.windows-ntfs-snapshot-comparison/v1",
        "mft": compare_file(args.before / "MFT.bin", args.after / "MFT.bin"),
        "bitmap": compare_file(args.before / "BITMAP.bin", args.after / "BITMAP.bin"),
        "claims": [
            "Comparison is byte-for-byte over the extracted stream files.",
            "A difference is evidence of metadata mutation, not proof that replay is correct.",
        ],
    }
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
