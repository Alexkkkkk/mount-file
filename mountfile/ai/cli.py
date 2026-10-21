# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Optional command-line entry point for the package MCP server.

Run ``python -m mountfile.ai.cli mcp --image /path/to/image.img``. The server
uses stdio transport and confines output artifacts to the configured workspace.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

_DEFAULT_WORKSPACE = str(
    Path(os.environ.get("MOUNTFILE_WORKDIR", "~/.mountfile/workspace")).expanduser()
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mountfile.ai.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subparsers.add_parser("mcp", help="run the MCP stdio server")
    mcp_parser.add_argument(
        "--image",
        required=True,
        help="one image file or a directory that contains permitted images",
    )
    mcp_parser.add_argument(
        "--workspace",
        default=_DEFAULT_WORKSPACE,
        help="workspace for extracted/recovered artifacts (default: %(default)s)",
    )
    mcp_parser.add_argument("--name", default="mount-file", help="MCP server name")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp":
        from .mcp_server import McpUnavailableError, PathValidationError, create_server

        try:
            server = create_server(
                image_root=args.image,
                workspace=args.workspace,
                name=args.name,
            )
        except (McpUnavailableError, PathValidationError) as exc:
            parser.exit(2, "%s: %s\n" % (parser.prog, exc))
        server.run()
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
