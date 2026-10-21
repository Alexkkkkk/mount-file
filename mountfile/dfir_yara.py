# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Optional, read-only YARA scanning for files and raw images.

The :mod:`yara` package is deliberately imported only when source rules need
compiling.  Callers may inject an already-compiled rules object (or a fake
YARA module in tests), so importing this module never requires yara-python.
The scanner gives YARA a path to read and never opens an evidence path for
writing.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from typing import Any, Optional


class YaraUnavailableError(RuntimeError):
    """Raised when yara-python is needed but is not installed or loadable."""


def _load_yara() -> Any:
    """Load the optional yara-python module at the point it is needed."""

    try:
        return importlib.import_module("yara")
    except (
        Exception
    ) as exc:  # pragma: no cover - exact loader error is platform-specific
        raise YaraUnavailableError(
            "YARA scanning requires the optional 'yara-python' package; "
            "install it with 'python -m pip install yara-python' and retry"
        ) from exc


def _compiled_rules(rules: Any, yara_module: Optional[Any]) -> Any:
    """Return a compiled rules object, compiling source with an injected module."""

    matcher = getattr(rules, "match", None)
    if callable(matcher):
        return rules
    if rules is None:
        raise ValueError(
            "rules must be YARA source, a rule-file path, or compiled rules"
        )

    module = yara_module if yara_module is not None else _load_yara()
    compile_rules = getattr(module, "compile", None)
    if not callable(compile_rules):
        raise TypeError("yara_module must provide a callable compile()")

    if isinstance(rules, os.PathLike):
        return compile_rules(filepath=os.fspath(rules))
    if isinstance(rules, bytes):
        try:
            source = rules.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("YARA rule source bytes must be valid UTF-8") from exc
        return compile_rules(source=source)
    if isinstance(rules, str):
        return compile_rules(source=rules)
    if isinstance(rules, Mapping):
        sources = {}
        for namespace, source in rules.items():
            if isinstance(source, bytes):
                source = source.decode("utf-8")
            if not isinstance(source, str):
                raise TypeError("YARA rule sources mapping values must be str or bytes")
            sources[str(namespace)] = source
        return compile_rules(sources=sources)
    raise TypeError("rules must be YARA source, a rule-file path, or compiled rules")


def _match_value(match: Any, name: str, default: Any = None) -> Any:
    if isinstance(match, Mapping):
        return match.get(name, default)
    return getattr(match, name, default)


def _as_offset(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        if isinstance(value, str) and value.strip():
            return int(value)
    except ValueError:
        return None
    return None


def _collect_offsets(value: Any, output: set[int]) -> None:
    """Collect offsets from old tuple-style and new object-style yara-python hits."""

    if value is None or isinstance(value, (str, bytes, bytearray)):
        return
    direct = _as_offset(value)
    if direct is not None:
        output.add(direct)
        return
    if isinstance(value, Mapping):
        if "offset" in value:
            offset = _as_offset(value.get("offset"))
            if offset is not None:
                output.add(offset)
        for key in ("instances", "matches", "strings"):
            if key in value:
                _collect_offsets(value[key], output)
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        # yara-python <= 3 returned (offset, identifier, data) tuples.
        values = tuple(value)
        offsets = tuple(_as_offset(item) for item in values)
        if values and all(offset is not None for offset in offsets):
            output.update(offset for offset in offsets if offset is not None)
            return
        if values and offsets[0] is not None:
            output.add(offsets[0])
            return
        for item in values:
            _collect_offsets(item, output)
        return

    offset = _as_offset(getattr(value, "offset", None))
    if offset is not None:
        output.add(offset)
    instances = getattr(value, "instances", None)
    if instances is not None:
        _collect_offsets(instances, output)


def _normalise_tags(tags: Any) -> tuple[str, ...]:
    if tags is None:
        return ()
    if isinstance(tags, (str, bytes)):
        tags = (tags,)
    elif isinstance(tags, (set, frozenset)):
        tags = sorted(tags, key=str)
    try:
        return tuple(str(tag) for tag in tags)
    except TypeError:
        return (str(tags),)


def _normalise_meta(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        # Copy the YARA-owned mapping so a caller cannot mutate the returned
        # finding by retaining a reference to the match object.
        return {str(key): value for key, value in meta.items()}
    return {}


def _normalise_match(match: Any) -> dict[str, Any]:
    rule = _match_value(match, "rule", "")
    offsets: set[int] = set()
    strings = _match_value(match, "strings")
    _collect_offsets(strings, offsets)
    # A small compatibility convenience for deterministic adapters that expose
    # a direct offset/offsets field instead of yara-python's ``strings`` list.
    _collect_offsets(_match_value(match, "offsets"), offsets)
    direct_offset = _match_value(match, "offset")
    if direct_offset is not None:
        _collect_offsets(direct_offset, offsets)
    return {
        "rule": str(rule),
        "tags": _normalise_tags(_match_value(match, "tags")),
        "meta": _normalise_meta(_match_value(match, "meta")),
        "offsets": tuple(sorted(offsets)),
    }


def scan_yara(
    image_path: str | os.PathLike[str],
    rules: Any,
    *,
    yara_module: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Scan *image_path* read-only with user-supplied YARA rules.

    ``rules`` may be a compiled yara-python rules object, YARA source text,
    UTF-8 source bytes, a local rule-file :class:`~pathlib.Path`, or a mapping
    of namespace to source text.  Source rules are compiled lazily.  The
    returned list contains only stable built-in values: ``rule``, ``tags``,
    ``meta``, and sorted unique string ``offsets`` (empty when unavailable).
    The optional ``yara_module`` hook is useful for deterministic tests and
    avoids importing yara-python when an already-compiled object is supplied.
    """

    path = os.fspath(image_path)
    compiled = _compiled_rules(rules, yara_module)
    match = getattr(compiled, "match", None)
    if not callable(match):
        raise TypeError("compiled YARA rules must provide a callable match()")
    # yara-python opens filepath read-only internally.  Passing filepath rather
    # than bytes keeps large raw images bounded in this adapter and does not
    # provide any write-capable handle to the evidence.
    found = match(filepath=path)
    return [_normalise_match(item) for item in (found or ())]


# Explicit aliases keep the tiny API convenient for callers that use the
# terms "file" or "image" while retaining one implementation and schema.
scan_file = scan_yara
scan_image = scan_yara
scan = scan_yara


__all__ = [
    "YaraUnavailableError",
    "scan",
    "scan_file",
    "scan_image",
    "scan_yara",
]
