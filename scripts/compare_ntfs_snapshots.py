# SPDX-License-Identifier: MIT
"""Compare two raw NTFS metadata snapshot directories byte-for-byte."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

_CHUNK_BYTES = 1 << 20


def compare_file(before: Path, after: Path) -> dict:
    before_size = before.stat().st_size
    after_size = after.stat().st_size
    before_digest = hashlib.sha256()
    after_digest = hashlib.sha256()
    differing = abs(before_size - after_size)
    first = None
    compared = 0
    with before.open("rb") as left, after.open("rb") as right:
        while compared < min(before_size, after_size):
            length = min(_CHUNK_BYTES, min(before_size, after_size) - compared)
            left_chunk = left.read(length)
            right_chunk = right.read(length)
            if len(left_chunk) != length or len(right_chunk) != length:
                raise OSError("snapshot file changed or was truncated during compare")
            before_digest.update(left_chunk)
            after_digest.update(right_chunk)
            chunk_differences = sum(a != b for a, b in zip(left_chunk, right_chunk))
            differing += chunk_differences
            if first is None and chunk_differences:
                first = compared + next(
                    index
                    for index, (a, b) in enumerate(zip(left_chunk, right_chunk))
                    if a != b
                )
            compared += length
        while chunk := left.read(_CHUNK_BYTES):
            before_digest.update(chunk)
        while chunk := right.read(_CHUNK_BYTES):
            after_digest.update(chunk)
    if first is None and before_size != after_size:
        first = min(before_size, after_size)
    return {
        "before_size": before_size,
        "after_size": after_size,
        "before_sha256": before_digest.hexdigest(),
        "after_sha256": after_digest.hexdigest(),
        "differing_bytes": differing,
        "first_difference": first,
        "byte_identical": before_size == after_size and differing == 0,
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
