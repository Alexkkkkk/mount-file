# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""`python -m mountfile ...` entry point (see mountfile/cli.py)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
