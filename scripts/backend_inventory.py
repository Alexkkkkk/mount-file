#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Print the optional filesystem backend inventory as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running the script directly from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mountfile.optional_backends import inventory_dict


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report optional filesystem backends without promoting capabilities."
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    indent = 2 if args.pretty else None
    print(
        json.dumps(inventory_dict(), ensure_ascii=False, indent=indent, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
