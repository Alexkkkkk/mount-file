"""``python -m mfim`` — GUI if Qt and a display are available, CLI otherwise."""

from __future__ import annotations

import os
import sys


def main() -> int:
    want_cli = "--cli" in sys.argv or os.environ.get("MFIM_FORCE_CLI") == "1"
    if not want_cli:
        try:
            from .gui import main as gui_main
        except SystemExit:
            want_cli = True
        else:
            argv = [a for a in sys.argv if a != "--gui"]
            try:
                return gui_main(argv)
            except Exception as exc:  # no display, broken Qt plugin, ...
                print(f"GUI unavailable ({exc}); falling back to the CLI", file=sys.stderr)
                want_cli = True
    from .cli import main as cli_main

    argv = [a for a in sys.argv[1:] if a not in ("--cli", "--gui")]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
