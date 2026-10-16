#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Write a release version into ``mountfile/__init__.py``.

The release pipeline knows the version from the git tag, while the package
hard-codes it in ``__version__``. A frozen executable built from a tag used
to ship whatever number happened to be committed last (an exe released as
v1.2.0 still reported 1.1.1 and offered itself an update), so every build
job runs this script with the tag right before PyInstaller.

Usage:
    python scripts/stamp_version.py v1.2.1
    python scripts/stamp_version.py 1.2.1
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "mountfile" / "__init__.py"
PATTERN = re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE)
VALID = re.compile(r"\d+(\.\d+)*([-+][0-9A-Za-z.\-]+)?")


def main(argv):
    if len(argv) != 2:
        print("usage: stamp_version.py <version|tag>", file=sys.stderr)
        return 2
    version = argv[1].strip()
    if version.startswith("v"):
        version = version[1:]
    if not VALID.fullmatch(version):
        print("refusing to stamp a malformed version: %r" % version, file=sys.stderr)
        return 2
    text = TARGET.read_text(encoding="utf-8")
    new, count = PATTERN.subn('__version__ = "%s"' % version, text, count=1)
    if count != 1:
        print("no __version__ line found in %s" % TARGET, file=sys.stderr)
        return 1
    TARGET.write_text(new, encoding="utf-8")
    print("stamped %s with version %s" % (TARGET.relative_to(ROOT), version))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
