# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only, optional Windows Registry parsing.

This module is deliberately a small facade rather than a forensic credential
extractor.  It identifies common Windows hive filenames and exposes generic
key/value iteration through either ``python-registry`` or ``regipy`` when the
caller has installed one of those optional packages.  No hive is modified and
no value is interpreted as a password, hash, or other credential.

The optional imports are lazy.  Importing :mod:`mountfile.registry` therefore
remains safe on systems where neither third-party parser is installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike]
_MISSING = object()


class RegistryError(Exception):
    """Base class for registry facade errors."""


class RegistryInputError(RegistryError, ValueError):
    """Raised when a source cannot be used as a registry input."""


class UnsupportedRegistryError(RegistryError, ValueError):
    """Raised for an unsupported filename or an unsupported file family."""


class OptionalDependencyError(RegistryError, ImportError):
    """Raised when the selected registry backend is not installed."""


class RegistryParseError(RegistryError):
    """Raised when an installed adapter cannot expose the requested data."""


# Compatibility-friendly names for callers that use a more generic spelling.
BackendUnavailableError = OptionalDependencyError
RegistryDependencyError = OptionalDependencyError
UnsupportedInputError = UnsupportedRegistryError
RegistryAdapterError = RegistryParseError


class RegistryFileType(str, Enum):
    """Known Windows files accepted by the facade."""

    SAM = "SAM"
    SYSTEM = "SYSTEM"
    SOFTWARE = "SOFTWARE"
    NTUSER = "NTUSER.DAT"
    NTUSER_DAT = "NTUSER.DAT"
    USN_JOURNAL = "$UsnJrnl"
    USN = "$UsnJrnl"
    GENERIC = "registry_hive"
    UNKNOWN = "unknown"


RegistryKind = RegistryFileType


@dataclass(frozen=True)
class RegistryFileInfo:
    """Non-sensitive identification information for a candidate file.

    Identification is based on the basename for the named Windows files and,
    for an otherwise unnamed source, the standard ``regf`` hive signature.
    ``$UsnJrnl`` is identified for callers but is intentionally not treated as
    a registry hive: the optional adapters supported here do not parse USN
    journal records.
    """

    kind: RegistryFileType
    name: str
    path: Optional[str] = None
    is_registry_hive: bool = False

    @property
    def file_type(self) -> RegistryFileType:
        """Alias for integrations that call the kind a file type."""

        return self.kind

    @property
    def recognized(self) -> bool:
        return self.kind is not RegistryFileType.UNKNOWN

    @property
    def parseable(self) -> bool:
        return self.is_registry_hive

    @property
    def adapter_supported(self) -> bool:
        """Whether this facade has a registry-hive adapter boundary for it."""

        return self.is_registry_hive


@dataclass(frozen=True)
class RegistryKey:
    """A normalized registry key without adapter-specific objects."""

    path: str
    name: str
    parent_path: Optional[str] = None

    @property
    def key_path(self) -> str:
        return self.path


@dataclass(frozen=True)
class RegistryValue:
    """A normalized key value returned by generic iteration.

    ``data`` is passed through from the selected adapter and is never decoded
    or interpreted by this module.  In particular, this class has no
    credential/hash extraction behavior.
    """

    key_path: str
    name: str
    value_type: Any = None
    data: Any = None

    @property
    def path(self) -> str:
        return self.key_path

    @property
    def value(self) -> Any:
        return self.data


_BACKENDS: Mapping[str, Tuple[str, str]] = {
    "python-registry": ("python-registry", "Registry"),
    "regipy": ("regipy", "regipy"),
}
_BACKEND_ALIASES: Mapping[str, str] = {
    "python_registry": "python-registry",
    "pythonregistry": "python-registry",
    "registry": "python-registry",
    "reg-ipy": "regipy",
}


_NAME_TO_TYPE: Mapping[str, RegistryFileType] = {
    "sam": RegistryFileType.SAM,
    "system": RegistryFileType.SYSTEM,
    "software": RegistryFileType.SOFTWARE,
    "ntuser.dat": RegistryFileType.NTUSER,
    "$usnjrnl": RegistryFileType.USN_JOURNAL,
}


def _normalise_backend_name(name: str) -> str:
    value = str(name).strip().lower()
    value = _BACKEND_ALIASES.get(value, value)
    if value not in _BACKENDS and value != "auto":
        choices = ", ".join(sorted(_BACKENDS))
        raise ValueError("unknown registry backend {!r}; choose auto, {}".format(name, choices))
    return value


def _module_available(import_name: str) -> bool:
    # Embedders and tests may provide an already-imported module without a
    # discoverable spec (for example, a small mocked adapter module).
    import sys

    if import_name in sys.modules:
        return sys.modules[import_name] is not None
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, AttributeError, ValueError):
        # A test or embedding application may have supplied a module directly
        # in sys.modules without a normal module spec.
        try:
            importlib.import_module(import_name)
        except ImportError:
            return False
        return True


def available_backends() -> Dict[str, bool]:
    """Return optional backend availability without opening a source file."""

    return {
        backend: _module_available(import_name)
        for backend, (_package_name, import_name) in _BACKENDS.items()
    }


def _dependency_error(backend: str, detail: Optional[str] = None) -> OptionalDependencyError:
    package_name, import_name = _BACKENDS[backend]
    message = (
        "Registry backend {!r} is unavailable: install optional package {!r} "
        "(import {!r}) to parse registry hives."
    ).format(backend, package_name, import_name)
    if detail:
        message += " Import failed with {}.".format(detail)
    return OptionalDependencyError(message)


def _source_path(source: Any) -> Optional[str]:
    if isinstance(source, (str, os.PathLike)):
        try:
            return os.fspath(source)
        except TypeError:
            return None
    return None


def _source_name(source: Any, filename: Optional[PathLike]) -> str:
    candidate: Any = filename
    if candidate is None:
        candidate = _source_path(source)
    if candidate is None:
        candidate = getattr(source, "name", "")
    try:
        text = os.fspath(candidate) if candidate is not None else ""
    except TypeError:
        text = str(candidate) if candidate is not None else ""
    if not isinstance(text, str):
        text = str(text)
    # ``os.path.basename`` follows the host OS; normalize both separators so
    # copied Windows paths are identified correctly when inspected on Linux.
    normalised = text.rstrip("/\\").replace("\\", "/")
    return normalised.rsplit("/", 1)[-1] or text


def _read_signature(source: Any) -> bytes:
    """Read at most four bytes while preserving a seekable stream position."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:4])
    path = _source_path(source)
    if path is not None:
        try:
            with open(path, "rb") as handle:
                return handle.read(4)
        except (OSError, TypeError):
            return b""
    reader = getattr(source, "read", None)
    if not callable(reader):
        return b""
    position: Any = _MISSING
    try:
        position = source.tell()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return bytes(reader(4) or b"")
    except (OSError, TypeError, ValueError):
        return b""
    finally:
        if position is not _MISSING:
            try:
                source.seek(position)
            except (AttributeError, OSError, ValueError):
                pass


def identify_registry_file(source: Any, *, filename: Optional[PathLike] = None) -> RegistryFileInfo:
    """Identify a supported hive name without parsing or modifying the source.

    ``source`` may be a path, a file-like object, or bytes.  Bytes and streams
    are only inspected for the four-byte ``regf`` signature when no known
    basename is available.  Unknown content is returned as ``UNKNOWN`` rather
    than guessed.
    """

    name = _source_name(source, filename)
    kind = _NAME_TO_TYPE.get(name.casefold(), RegistryFileType.UNKNOWN)
    path = _source_path(source)
    if kind is RegistryFileType.USN_JOURNAL:
        return RegistryFileInfo(kind=kind, name=name, path=path, is_registry_hive=False)
    if kind is not RegistryFileType.UNKNOWN:
        return RegistryFileInfo(kind=kind, name=name, path=path, is_registry_hive=True)
    if _read_signature(source) == b"regf":
        return RegistryFileInfo(
            kind=RegistryFileType.GENERIC,
            name=name,
            path=path,
            is_registry_hive=True,
        )
    return RegistryFileInfo(kind=RegistryFileType.UNKNOWN, name=name, path=path, is_registry_hive=False)


# Common detector spellings used by small integrations.
detect_registry_file = identify_registry_file
identify_file = identify_registry_file


def is_registry_file(source: Any, *, filename: Optional[PathLike] = None) -> bool:
    """Return whether the source is one of the supported registry hive files."""

    return identify_registry_file(source, filename=filename).is_registry_hive


def _member(obj: Any, names: Sequence[str], default: Any = _MISSING) -> Any:
    """Read a named attribute or mapping item, calling zero-argument methods."""

    for name in names:
        value: Any = _MISSING
        if isinstance(obj, Mapping) and name in obj:
            value = obj[name]
        else:
            try:
                value = getattr(obj, name)
            except AttributeError:
                continue
        if callable(value):
            try:
                value = value()
            except TypeError:
                # A method requiring arguments is not a scalar property.
                continue
        return value
    return default


def _iterable(value: Any) -> Optional[Iterator[Any]]:
    if value is None or value is _MISSING or isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return iter(value)
    except TypeError:
        return None


def _children(key: Any) -> Iterable[Any]:
    for names in (("subkeys", "sub_keys", "subkey_list", "children"), ("get_subkeys",)):
        value = _member(key, names)
        iterator = _iterable(value)
        if iterator is not None:
            return iterator
    return ()


def _walk_keys(root: Any) -> Iterator[Any]:
    """Walk a finite adapter key tree while tolerating repeated object links."""

    if root is None or root is _MISSING:
        return
    seen = set()
    stack = [(root, 0)]
    while stack:
        key, depth = stack.pop()
        marker = id(key)
        if marker in seen:
            continue
        seen.add(marker)
        yield key
        if depth >= 256:
            continue
        children = list(_children(key))
        for child in reversed(children):
            stack.append((child, depth + 1))


def _root(handle: Any) -> Any:
    root = _member(handle, ("root", "root_key"))
    if root is not _MISSING and root is not None:
        return root
    return None


def _text(value: Any, default: str = "") -> str:
    if value is None or value is _MISSING:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _key_record(raw_key: Any, parent_path: Optional[str] = None) -> RegistryKey:
    raw_path = _member(raw_key, ("path", "key_path"))
    raw_name = _member(raw_key, ("name", "key_name"))
    name = _text(raw_name)
    path = _text(raw_path)
    if not path:
        if not name:
            name = "ROOT"
        if parent_path:
            if parent_path == "\\":
                path = "\\" + name
            else:
                path = parent_path.rstrip("\\") + "\\" + name
        else:
            path = "\\" if name.upper() == "ROOT" else "\\" + name
    if not name:
        stripped = path.rstrip("\\")
        name = stripped.rsplit("\\", 1)[-1] if stripped else "ROOT"
    if parent_path is None:
        parent = path.rsplit("\\", 1)[0] if "\\" in path.rstrip("\\") else None
        parent_path = parent or None
    return RegistryKey(path=path, name=name, parent_path=parent_path)


def _values(raw_key: Any) -> Iterable[Any]:
    value = _member(raw_key, ("values", "value_list", "get_values"))
    iterator = _iterable(value)
    return iterator if iterator is not None else ()


def _value_record(raw_value: Any, key_path: str) -> RegistryValue:
    raw_name = _member(raw_value, ("name", "value_name"), default="")
    raw_type = _member(raw_value, ("value_type", "type", "kind"), default=None)
    raw_data = _member(raw_value, ("data", "value", "raw_value"), default=None)
    return RegistryValue(
        key_path=key_path,
        name=_text(raw_name),
        value_type=raw_type,
        data=raw_data,
    )


class PythonRegistryAdapter:
    """Adapter for the ``python-registry`` package (lazy import)."""

    name = "python-registry"
    package = "python-registry"

    def _registry_class(self) -> Any:
        try:
            module = importlib.import_module("Registry")
        except ImportError as exc:
            raise _dependency_error(self.name, exc.__class__.__name__) from exc
        candidate = getattr(module, "Registry", None)
        if candidate is None:
            raise RegistryParseError("python-registry import has no Registry factory")
        nested = getattr(candidate, "Registry", None)
        if nested is not None and callable(nested):
            candidate = nested
        if not callable(candidate):
            raise RegistryParseError("python-registry Registry factory is not callable")
        return candidate

    def open(self, source: Any) -> Any:
        return self._registry_class()(source)

    def iter_keys(self, handle: Any) -> Iterator[Any]:
        root = _root(handle)
        if root is None:
            raise RegistryParseError("python-registry handle does not expose a root key")
        return _walk_keys(root)


class RegipyAdapter:
    """Adapter for the ``regipy`` package (lazy import)."""

    name = "regipy"
    package = "regipy"

    def _hive_class(self) -> Any:
        module: Any
        try:
            module = importlib.import_module("regipy.registry")
        except ImportError:
            try:
                module = importlib.import_module("regipy")
            except ImportError as exc:
                raise _dependency_error(self.name, exc.__class__.__name__) from exc
        candidate = getattr(module, "RegistryHive", None)
        if candidate is None:
            raise RegistryParseError("regipy import has no RegistryHive factory")
        return candidate

    def open(self, source: Any) -> Any:
        return self._hive_class()(source)

    def iter_keys(self, handle: Any) -> Iterator[Any]:
        root = _root(handle)
        if root is None:
            raise RegistryParseError("regipy handle does not expose a root key")
        walked = list(_walk_keys(root))
        if len(walked) > 1:
            return iter(walked)
        recurse = getattr(handle, "recurse_subkeys", None)
        if callable(recurse):
            try:
                descendants = iter(recurse())
            except TypeError:
                descendants = iter(())

            def _with_root() -> Iterator[Any]:
                seen = {id(root)}
                yield root
                for key in descendants:
                    if id(key) not in seen:
                        seen.add(id(key))
                        yield key

            return _with_root()
        return iter(walked)


def _select_adapter(backend: str) -> Any:
    selected = _normalise_backend_name(backend)
    if selected == "auto":
        statuses = available_backends()
        for candidate in ("python-registry", "regipy"):
            if statuses[candidate]:
                return PythonRegistryAdapter() if candidate == "python-registry" else RegipyAdapter()
        raise OptionalDependencyError(
            "No optional Windows Registry backend is installed; install "
            "'python-registry' or 'regipy' to parse registry hives."
        )
    import_name = _BACKENDS[selected][1]
    if not _module_available(import_name):
        raise _dependency_error(selected)
    return PythonRegistryAdapter() if selected == "python-registry" else RegipyAdapter()


class RegistryParser:
    """Lazy, read-only facade over one supported registry hive."""

    def __init__(
        self,
        source: Any,
        *,
        backend: str = "auto",
        adapter: Optional[Any] = None,
        file_info: Optional[RegistryFileInfo] = None,
    ) -> None:
        self.source = source
        self.file_info = file_info or identify_registry_file(source)
        if not self.file_info.is_registry_hive:
            raise UnsupportedRegistryError(
                "unsupported registry input {!r}; expected SAM, SYSTEM, SOFTWARE, "
                "NTUSER.DAT, or a file with the regf hive signature".format(self.file_info.name)
            )
        self.backend = _normalise_backend_name(backend)
        self.adapter = adapter if adapter is not None else _select_adapter(self.backend)
        adapter_name = getattr(self.adapter, "name", None)
        self.backend = _text(adapter_name, self.backend)
        self._handle: Any = _MISSING
        self._closed = False

    def _open(self) -> Any:
        if self._closed:
            raise RegistryParseError("registry parser is closed")
        if self._handle is _MISSING:
            opener = getattr(self.adapter, "open", None)
            if not callable(opener):
                raise RegistryParseError("registry adapter does not expose open(source)")
            try:
                self._handle = opener(self.source)
            except OptionalDependencyError:
                raise
            except Exception as exc:
                raise RegistryParseError(
                    "registry adapter {!r} could not open the source: {}".format(
                        self.backend, exc.__class__.__name__
                    )
                ) from exc
        return self._handle

    def close(self) -> None:
        if self._closed:
            return
        if self._handle is not _MISSING:
            closer = getattr(self._handle, "close", None)
            if callable(closer):
                closer()
        self._closed = True

    def __enter__(self) -> "RegistryParser":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _raw_keys(self) -> Iterator[Any]:
        handle = self._open()
        iterator = getattr(self.adapter, "iter_keys", None)
        if not callable(iterator):
            raise RegistryParseError("registry adapter does not expose iter_keys(handle)")
        try:
            values = iterator(handle)
            return iter(values)
        except OptionalDependencyError:
            raise
        except Exception as exc:
            raise RegistryParseError(
                "registry adapter {!r} could not enumerate keys: {}".format(
                    self.backend, exc.__class__.__name__
                )
            ) from exc

    @staticmethod
    def _matches(record: RegistryKey, path: Optional[str]) -> bool:
        if path is None:
            return True
        target = str(path).rstrip("\\").casefold() or "\\"
        current = record.path.rstrip("\\").casefold() or "\\"
        return current == target or current.startswith(target + "\\")

    def iter_keys(self, path: Optional[str] = None) -> Iterator[RegistryKey]:
        """Yield normalized keys, optionally restricted to a path subtree."""

        for raw_key in self._raw_keys():
            record = _key_record(raw_key)
            if self._matches(record, path):
                yield record

    def iter_values(self, path: Optional[str] = None) -> Iterator[RegistryValue]:
        """Yield normalized values without adapter-specific interpretation."""

        for raw_key in self._raw_keys():
            key = _key_record(raw_key)
            if not self._matches(key, path):
                continue
            for raw_value in _values(raw_key):
                yield _value_record(raw_value, key.path)

    # Short aliases make the facade convenient without exposing adapter objects.
    keys = iter_keys
    values = iter_values
    iter_key_values = iter_values
    iter_entries = iter_values


def open_registry(
    source: Any,
    *,
    backend: str = "auto",
    adapter: Optional[Any] = None,
) -> RegistryParser:
    """Create a lazy parser for a supported hive.

    ``adapter`` is an explicit injection point for applications and tests.  It
    must expose ``open(source)`` and ``iter_keys(handle)``; no optional package
    is imported when an injected adapter is used.
    """

    info = identify_registry_file(source)
    if info.kind is RegistryFileType.USN_JOURNAL:
        raise UnsupportedRegistryError(
            "$UsnJrnl is an NTFS USN journal, not a registry hive; the supported "
            "python-registry/regipy adapters do not parse journal records"
        )
    if not info.is_registry_hive:
        raise UnsupportedRegistryError(
            "unsupported registry input {!r}; expected SAM, SYSTEM, SOFTWARE, "
            "NTUSER.DAT, or a file with the regf hive signature".format(info.name)
        )
    return RegistryParser(source, backend=backend, adapter=adapter, file_info=info)


parse_registry = open_registry
open_hive = open_registry


__all__ = [
    "BackendUnavailableError",
    "OptionalDependencyError",
    "PythonRegistryAdapter",
    "RegistryAdapterError",
    "RegistryDependencyError",
    "RegistryError",
    "RegistryFileInfo",
    "RegistryFileType",
    "RegistryInputError",
    "RegistryKey",
    "RegistryKind",
    "RegistryParseError",
    "RegistryParser",
    "RegistryValue",
    "RegipyAdapter",
    "UnsupportedInputError",
    "UnsupportedRegistryError",
    "available_backends",
    "detect_registry_file",
    "identify_file",
    "identify_registry_file",
    "is_registry_file",
    "open_hive",
    "open_registry",
    "parse_registry",
]
