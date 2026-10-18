#!/usr/bin/env python3
"""Find a deleted directory entry in a disposable demo image, read-only."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["MOUNTFILE_READONLY"] = "1"

import mountfile_ai as mountfile_ai  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: recovery_scan.py IMAGE", file=sys.stderr)
        return 2

    image = Path(sys.argv[1]).resolve()
    mountfile_ai.CONFIG.allow(image)
    for partition in mountfile_ai.list_partitions(image):
        entries = mountfile_ai.search_files(
            image, partition.offset, r"\.TXT$", include_deleted=True
        )
        for entry in entries:
            if entry.deleted:
                print(
                    json.dumps(
                        {
                            "offset": partition.offset,
                            "inode": entry.inode,
                            "path": entry.path,
                            "size": entry.size,
                            "deleted": entry.deleted,
                        },
                        sort_keys=True,
                    )
                )
                return 0

    print("no deleted demo entry found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
