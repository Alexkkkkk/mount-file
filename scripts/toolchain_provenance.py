# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Emit machine-readable provenance for one test/benchmark run.

Issues #389 and #390 require acceptance evidence to carry the interpreter,
platform, exact tool/library versions and the lockfile hash of the tree that
produced it, so two runs on the same commit can be compared instead of trusted.
This script prints that record as JSON and never mutates the image under test.

    python scripts/toolchain_provenance.py            # JSON on stdout
    python scripts/toolchain_provenance.py -o out.json

Exit code is 0 even when optional components are missing: absence is reported
as ``null`` (the ``unavailable`` state), never silently upgraded to a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import subprocess
import sys
from pathlib import Path

# Import name -> human/distribution name used in the report.
_DISTRIBUTIONS = {
    "PyQt6.QtCore": "PyQt6",
    "pydantic": "pydantic",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "openai": "openai",
    "mcp": "mcp",
    "regex": "regex",
    "dateutil": "python-dateutil",
    "yara": "yara-python",
    "pytsk3": "pytsk3",
}

_TOOLS = ("ruff", "black", "isort", "pytest", "mypy")


def _distribution_version(module_name: str) -> str | None:
    """Return the installed version or ``None`` when the component is absent."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    version = getattr(module, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib.metadata import version as dist_version

        return dist_version(_DISTRIBUTIONS.get(module_name, module_name))
    except Exception:  # noqa: BLE001 - provenance must never raise
        return None


def _tool_version(tool: str) -> str | None:
    """Return the version of a CLI tool on PATH, or ``None`` when absent."""
    for args in ([sys.executable, "-m", tool, "--version"], [tool, "--version"]):
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode == 0 and output:
            return output.splitlines()[0]
    return None


def _sha256(path: Path) -> str | None:
    """Hash a repository file, or ``None`` when it is not part of this tree."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(root: Path) -> str | None:
    """Return the current commit, or ``None`` outside a git checkout."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def collect(root: Path) -> dict:
    """Build the provenance record for ``root``."""
    # Run straight from a clean checkout (`python scripts/...`) as well as from
    # an installed wheel: the repository root is importable either way.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import mountfile

    return {
        "schema": "mountfile.toolchain-provenance/1",
        "revision": _git_head(root),
        "mountfile_version": mountfile.__version__,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "compiler": platform.python_compiler(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "lockfiles": {
            "pyproject.toml": _sha256(root / "pyproject.toml"),
            "uv.lock": _sha256(root / "uv.lock"),
            "requirements.txt": _sha256(root / "requirements.txt"),
        },
        "libraries": {
            dist: _distribution_version(module)
            for module, dist in _DISTRIBUTIONS.items()
        },
        "tools": {tool: _tool_version(tool) for tool in _TOOLS},
    }


def main(argv: list[str] | None = None) -> int:
    """Print the provenance record; ``-o`` also writes it to a new file."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="also write the JSON to this path")
    args = parser.parse_args(argv)

    payload = collect(Path(__file__).resolve().parent.parent)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.output:
        target = Path(args.output)
        if target.exists():
            print(f"error: {target} already exists", file=sys.stderr)
            return 1
        target.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
