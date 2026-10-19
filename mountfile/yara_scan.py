# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Bounded, read-only YARA scanning for local forensic images.

The scanner is deliberately separate from the image filesystem providers.  It
reads at most ``max_bytes`` from a host image and never writes to the evidence
file.  ``yara-python`` remains optional so the base package can be installed on
systems that only need image browsing.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional


DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class YaraScanError(RuntimeError):
    """Raised for invalid scanner input or a failed YARA operation."""


class YaraUnavailableError(YaraScanError):
    """Raised when optional ``yara-python`` is not installed."""


@dataclass(frozen=True)
class YaraFinding:
    """One YARA rule match in the scanned byte range."""

    rule: str
    tags: tuple[str, ...]
    meta: dict[str, Any]
    strings: tuple[str, ...]


def _load_yara(yara_module: Optional[Any] = None) -> Any:
    if yara_module is not None:
        return yara_module
    try:
        return importlib.import_module("yara")
    except Exception as exc:
        raise YaraUnavailableError(
            "optional dependency yara-python is unavailable; install mountfile[ai]"
        ) from exc


def _match_string_ids(match: Any) -> tuple[str, ...]:
    values: List[str] = []
    for item in getattr(match, "strings", ()) or ():
        identifier = getattr(item, "identifier", None)
        if identifier is None and isinstance(item, tuple) and len(item) >= 2:
            identifier = item[1]
        if identifier is not None and str(identifier) not in values:
            values.append(str(identifier))
    return tuple(values)


def scan_image(
    image: Path,
    rules: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    yara_module: Optional[Any] = None,
) -> List[YaraFinding]:
    """Scan the first bounded byte range of ``image`` with a YARA rule file."""

    image_path = Path(image).expanduser()
    rules_path = Path(rules).expanduser()
    if not image_path.is_file():
        raise YaraScanError("image is not a regular file: %s" % image_path)
    if not rules_path.is_file():
        raise YaraScanError("rules are not a regular file: %s" % rules_path)
    if max_bytes < 1:
        raise YaraScanError("max_bytes must be positive")
    try:
        size = image_path.stat().st_size
        with image_path.open("rb") as stream:
            data = stream.read(min(size, int(max_bytes)))
    except OSError as exc:
        raise YaraScanError("cannot read image: %s" % exc) from exc
    yara = _load_yara(yara_module)
    try:
        compiled = yara.compile(filepath=str(rules_path))
        matches = compiled.match(data=data)
    except Exception as exc:
        raise YaraScanError("YARA scan failed: %s" % exc) from exc
    findings: List[YaraFinding] = []
    for match in matches or ():
        findings.append(
            YaraFinding(
                rule=str(getattr(match, "rule", match)),
                tags=tuple(str(value) for value in (getattr(match, "tags", ()) or ())),
                meta=dict(getattr(match, "meta", {}) or {}),
                strings=_match_string_ids(match),
            )
        )
    return findings
