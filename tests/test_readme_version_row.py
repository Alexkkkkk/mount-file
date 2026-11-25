# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""README version row must never drift from the canonical package version (#390).

Issue #390 requires status synchronization to *break CI* when the product status
surfaces disagree. The README summary table is one of those surfaces and it is
maintained by hand, so it silently drifted to ``1.103.0`` while
``pyproject.toml`` / ``mountfile/__init__.py`` were already at ``1.103.2``.

This gate is deliberately narrow: it reads only the canonical version sources and
the single "Версия пакета" row of the README, so it cannot be satisfied by
weakening any generated artifact.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_ROW = re.compile(r"^\|\s*Версия пакета\s*\|\s*`(?P<version>[^`]+)`", re.MULTILINE)


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _init_version() -> str:
    text = (REPO_ROOT / "mountfile" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "mountfile/__init__.py must define __version__"
    return match.group(1)


def _readme_version() -> str:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = _ROW.search(text)
    assert match is not None, "README.md must keep a '| Версия пакета | `x.y.z` |' row"
    return match.group("version")


@pytest.mark.unit
def test_canonical_version_sources_agree() -> None:
    assert _pyproject_version() == _init_version()


@pytest.mark.unit
def test_readme_version_row_matches_canonical_version() -> None:
    canonical = _pyproject_version()
    readme = _readme_version()
    assert readme == canonical, (
        f"README.md version row says {readme!r} but the canonical version is "
        f"{canonical!r}; regenerate or update the row (see scripts/generate_readme.py)"
    )
