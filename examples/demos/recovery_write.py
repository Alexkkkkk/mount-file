#!/usr/bin/env python3
"""Recover one already-identified deleted inode into an external demo file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# This script is only invoked by recovery_demo.sh against its disposable image.
os.environ["MOUNTFILE_READONLY"] = "0"

import mountfile_ai as mountfile_ai  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("offset", type=int)
    parser.add_argument("inode", type=int)
    parser.add_argument("size", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    image = args.image.resolve()
    output = args.output.resolve()
    mountfile_ai.CONFIG.allow(image)
    result = mountfile_ai.recover_deleted_file(
        image,
        offset=args.offset,
        inode=args.inode,
        original_size=args.size,
        output=output,
    )
    print(
        "Recovered %d bytes; complete=%s; SHA-256=%s"
        % (result.recovered_size, result.complete, result.sha256)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
