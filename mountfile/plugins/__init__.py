# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Opt-in plugin lifecycle support for read-only image analysis.

The registry is deliberately inert until a caller explicitly registers plugins or
calls :meth:`PluginRegistry.discover`.  Hook arguments are immutable snapshots;
plugins never receive an image file object or another writable application-owned
object.  A failing plugin is isolated from the remaining plugins for the current
session and the failure is retained in :attr:`PluginRegistry.errors`.

A plugin is a regular object with a stable ``name`` attribute and any of these
optional methods::

    on_open(open_event)
    on_file(file_event)
    on_close()

The registry does not interpret hook return values.  This makes the boundary
observational by default and prevents a plugin from changing mount or write
behaviour accidentally.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import os
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

__all__ = [
    "DEFAULT_ENTRY_POINT_GROUP",
    "FileEvent",
    "OpenEvent",
    "PluginFailure",
    "PluginRegistrationError",
    "PluginRegistry",
    "PluginStatus",
    "ReadOnlyImage",
]

DEFAULT_ENTRY_POINT_GROUP = "mountfile.plugins"


class PluginRegistrationError(ValueError):
    """Raised when a plugin cannot be registered safely."""


@dataclass(frozen=True)
class PluginFailure:
    """A serialisable description of an isolated plugin failure."""

    plugin: str
    hook: str
    exception_type: str
    message: str

    @property
    def error(self) -> str:
        """Return a concise human-readable description."""

        return "%s: %s" % (self.exception_type, self.message)


@dataclass(frozen=True)
class PluginStatus:
    """Status used by optional example adapters.

    ``state`` is one of ``available``, ``degraded``, ``unconfigured`` or
    ``unavailable``.  The class is intentionally dependency-free so adapters
    can still be imported when their optional packages are not installed.
    """

    name: str
    state: str
    message: str = ""

    @property
    def available(self) -> bool:
        return self.state in ("available", "degraded")


def _freeze(value: Any) -> Any:
    """Copy common mutable containers into immutable equivalents."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


class ReadOnlyImage:
    """A minimal read-only image view passed to ``OpenEvent``.

    For a filesystem path, each read opens the file with ``rb`` and closes it
    immediately.  For bytes-like input, an immutable copy is retained.  No
    writable file descriptor or caller-owned stream is retained.
    """

    __slots__ = ("_data", "_path", "_size")

    def __init__(self, source: Any = None) -> None:
        data: Optional[bytes] = None
        path: Optional[str] = None
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
        elif isinstance(source, (str, os.PathLike)):
            path = os.fspath(source)
        elif isinstance(source, Mapping):
            path_value = source.get("path", source.get("filename"))
            if path_value is not None:
                path = os.fspath(path_value)
            raw_data = source.get("data", source.get("content"))
            if raw_data is not None:
                data = bytes(raw_data)
        elif source is not None:
            path_value = getattr(source, "path", None)
            if path_value is not None:
                path = os.fspath(path_value)

        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_path", path)
        if data is not None:
            size: Optional[int] = len(data)
        elif path is not None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
        else:
            size = None
        object.__setattr__(self, "_size", size)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ReadOnlyImage is immutable")

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def size(self) -> Optional[int]:
        return self._size

    def read(self, offset: int = 0, length: int = -1) -> bytes:
        """Read bytes without exposing a writable source."""

        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(length, bool) or not isinstance(length, int) or length < -1:
            raise ValueError("length must be -1 or a non-negative integer")
        if self._data is not None:
            end = None if length == -1 else offset + length
            return self._data[offset:end]
        if self._path is None:
            raise OSError("image content is not available through this read-only view")
        with open(self._path, "rb") as handle:
            handle.seek(offset)
            return handle.read() if length == -1 else handle.read(length)


@dataclass(frozen=True)
class OpenEvent:
    """Immutable information made available to ``on_open``."""

    image: ReadOnlyImage
    path: Optional[str]
    size: Optional[int]
    metadata: Mapping[str, Any]

    @classmethod
    def from_value(
        cls, value: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> "OpenEvent":
        if isinstance(value, cls):
            if not metadata:
                return value
            merged = dict(value.metadata)
            merged.update(metadata)
            return cls(value.image, value.path, value.size, _freeze(merged))

        source = value
        supplied: Dict[str, Any] = {}
        if isinstance(value, Mapping):
            source = value.get("image", value.get("path", value))
            raw_metadata = value.get("metadata")
            if isinstance(raw_metadata, Mapping):
                supplied.update(raw_metadata)
            for key in ("path", "size", "format"):
                if key in value and key not in supplied:
                    supplied[key] = value[key]
        if metadata:
            supplied.update(metadata)
        image = ReadOnlyImage(source)
        path = image.path
        size = image.size
        if isinstance(value, Mapping):
            path = value.get("path", path)
            size = value.get("size", size)
        return cls(image, path, size, _freeze(supplied))


@dataclass(frozen=True)
class FileEvent:
    """Immutable file metadata and an optional immutable content snapshot."""

    path: Optional[str]
    size: Optional[int]
    offset: Optional[int]
    data: Optional[bytes]
    metadata: Mapping[str, Any]

    @property
    def content(self) -> Optional[bytes]:
        """Alias for adapters that use ``content`` rather than ``data``."""

        return self.data

    @property
    def name(self) -> Optional[str]:
        """The file name/path supplied by the caller."""

        return self.path

    @classmethod
    def from_value(
        cls, value: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> "FileEvent":
        if isinstance(value, cls):
            if not metadata:
                return value
            merged = dict(value.metadata)
            merged.update(metadata)
            return cls(
                value.path, value.size, value.offset, value.data, _freeze(merged)
            )

        source = value
        supplied: Dict[str, Any] = {}
        path: Optional[str] = None
        size: Optional[int] = None
        offset: Optional[int] = None
        data: Optional[bytes] = None
        if isinstance(value, Mapping):
            source = value.get("data", value.get("content"))
            path_value = value.get("path", value.get("name", value.get("filename")))
            if path_value is not None:
                path = os.fspath(path_value)
            raw_metadata = value.get("metadata")
            if isinstance(raw_metadata, Mapping):
                supplied.update(raw_metadata)
            for key in ("timestamp", "mtime", "ctime", "atime", "extension"):
                if key in value and key not in supplied:
                    supplied[key] = value[key]
            size_value = value.get("size")
            if isinstance(size_value, int) and not isinstance(size_value, bool):
                size = size_value
            offset_value = value.get("offset")
            if isinstance(offset_value, int) and not isinstance(offset_value, bool):
                offset = offset_value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            source = value
        else:
            path_value = getattr(value, "path", getattr(value, "name", None))
            if path_value is not None and not callable(path_value):
                path = os.fspath(path_value)
            raw_data = getattr(value, "data", getattr(value, "content", None))
            if raw_data is not None:
                source = raw_data
            raw_metadata = getattr(value, "metadata", None)
            if isinstance(raw_metadata, Mapping):
                supplied.update(raw_metadata)

        if source is not None:
            if isinstance(source, str):
                source = source.encode("utf-8")
            try:
                data = bytes(source)
            except (TypeError, ValueError):
                data = None
        if data is not None and size is None:
            size = len(data)
        if metadata:
            supplied.update(metadata)
        return cls(path, size, offset, data, _freeze(supplied))


@dataclass
class _PluginState:
    plugin: Any
    name: str
    failed: bool = False
    opened: bool = False


class PluginRegistry:
    """Explicit plugin registry with deterministic discovery and safe dispatch."""

    HOOKS = ("on_open", "on_file", "on_close")

    def __init__(
        self, plugins: Optional[Iterable[Any]] = None, *, discover: bool = False
    ) -> None:
        self._states: List[_PluginState] = []
        self._errors: List[PluginFailure] = []
        self._active = False
        # Kept as a public compatibility view for callers of the original
        # prototype API.  Registration still goes through the stateful paths
        # below so failures can be associated with a plugin name.
        self.hooks: Dict[str, List[Callable[..., Any]]] = {
            hook: [] for hook in self.HOOKS
        }
        if plugins:
            for plugin in plugins:
                self.register(plugin)
        if discover:
            self.discover(entry_points=True)

    @property
    def plugins(self) -> Tuple[Any, ...]:
        return tuple(state.plugin for state in self._states)

    @property
    def plugin_names(self) -> Tuple[str, ...]:
        return tuple(state.name for state in self._states)

    @property
    def errors(self) -> Tuple[PluginFailure, ...]:
        return tuple(self._errors)

    @property
    def active(self) -> bool:
        return self._active

    def register(
        self,
        plugin: Any,
        hook: Optional[str] = None,
        func: Optional[Callable[..., Any]] = None,
        *,
        name: Optional[str] = None,
    ) -> Any:
        """Register one plugin; registration order is preserved exactly."""

        # The stage-toolkit prototype also accepted register(name, hook,
        # function).  Keep that small API useful without making it the
        # default discovery path.
        if hook is not None or func is not None:
            if hook not in self.HOOKS:
                raise PluginRegistrationError("unknown hook: %s" % hook)
            callback = func if func is not None else plugin
            if not callable(callback):
                raise PluginRegistrationError("hook callback is not callable")
            callback_name = name or (
                plugin
                if isinstance(plugin, str)
                else getattr(callback, "__name__", "callback")
            )
            callback_plugin = type(
                "_CallbackPlugin", (), {"name": str(callback_name), hook: callback}
            )()
            return self.register(callback_plugin)

        plugin = self._instantiate(plugin)
        plugin_name = name or getattr(plugin, "name", None)
        if not isinstance(plugin_name, str) or not plugin_name.strip():
            plugin_name = plugin.__class__.__name__
        plugin_name = plugin_name.strip()
        if not any(state.name == plugin_name for state in self._states):
            hooks = [
                getattr(plugin, hook, None)
                for hook in ("on_open", "on_file", "on_close")
            ]
            if not any(callable(hook) for hook in hooks):
                raise PluginRegistrationError(
                    "plugin %r has no lifecycle hooks" % plugin_name
                )
            self._states.append(_PluginState(plugin, plugin_name))
            for hook_name in self.HOOKS:
                callback = getattr(plugin, hook_name, None)
                if callable(callback):
                    self.hooks[hook_name].append(callback)
            return plugin
        raise PluginRegistrationError("duplicate plugin name: %s" % plugin_name)

    def try_register(self, plugin: Any, *, name: Optional[str] = None) -> bool:
        """Register a plugin and retain registration errors instead of raising."""

        try:
            self.register(plugin, name=name)
        except Exception as exc:
            self._record_failure(
                name or getattr(plugin, "name", repr(plugin)), "registration", exc
            )
            return False
        return True

    def open(
        self, image: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> OpenEvent:
        """Start a session and call every registered ``on_open`` hook."""

        if self._active:
            raise RuntimeError("plugin session is already open")
        event = OpenEvent.from_value(image, metadata)
        self._active = True
        for state in self._states:
            state.failed = False
            state.opened = True
            hook = getattr(state.plugin, "on_open", None)
            if callable(hook):
                try:
                    self._invoke(hook, "on_open", (event,))
                except Exception as exc:
                    self._isolate(state, "on_open", exc)
        return event

    def on_open(
        self, image: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> OpenEvent:
        """Hook-shaped alias for :meth:`open`."""

        return self.open(image, metadata)

    def file(
        self, file: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> FileEvent:
        """Call ``on_file`` hooks with an immutable file snapshot."""

        if not self._active:
            raise RuntimeError("plugin session is not open")
        event = FileEvent.from_value(file, metadata)
        for state in self._states:
            if state.failed:
                continue
            hook = getattr(state.plugin, "on_file", None)
            if callable(hook):
                try:
                    self._invoke(hook, "on_file", (event,))
                except Exception as exc:
                    self._isolate(state, "on_file", exc)
        return event

    def on_file(
        self, file: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> FileEvent:
        """Hook-shaped alias for :meth:`file`."""

        return self.file(file, metadata)

    def close(self) -> None:
        """End a session and isolate every ``on_close`` failure.

        Close is attempted even for a plugin whose earlier hook failed, so a
        plugin can release its own resources without affecting other plugins.
        """

        if not self._active:
            return
        for state in self._states:
            hook = getattr(state.plugin, "on_close", None)
            if callable(hook):
                try:
                    self._invoke(hook, "on_close", ())
                except Exception as exc:
                    self._isolate(state, "on_close", exc)
            state.opened = False
        self._active = False

    def on_close(self) -> None:
        """Hook-shaped alias for :meth:`close`."""

        self.close()

    def emit(self, hook: str, *args: Any, **kwargs: Any) -> List[Any]:
        """Call one hook with isolated failures and return successful results.

        This preserves the original ``emit("on_file", path, data)`` API.  A
        one-argument ``on_file`` receives a :class:`FileEvent`; a legacy
        two-argument hook receives immutable ``(path, bytes)`` values.
        """

        if hook not in self.HOOKS:
            raise ValueError("unknown hook: %s" % hook)
        safe_args = self._safe_emit_args(hook, args)
        safe_kwargs = {key: _freeze(value) for key, value in kwargs.items()}
        results: List[Any] = []
        for state in self._states:
            if state.failed and hook != "on_close":
                continue
            callback = getattr(state.plugin, hook, None)
            if not callable(callback):
                continue
            try:
                results.append(self._invoke(callback, hook, safe_args, safe_kwargs))
            except Exception as exc:
                self._isolate(state, hook, exc)
        return results

    def trigger(self, hook: str, *args: Any, **kwargs: Any) -> List[Any]:
        """Alias for :meth:`emit` retained for the stage-toolkit prototype."""

        return self.emit(hook, *args, **kwargs)

    def dispatch(self, hook: str, *args: Any, **kwargs: Any) -> List[Any]:
        """Alias for :meth:`emit` for callers using dispatch terminology."""

        return self.emit(hook, *args, **kwargs)

    @staticmethod
    def _safe_emit_args(hook: str, args: Tuple[Any, ...]) -> Tuple[Any, ...]:
        if hook == "on_file" and len(args) == 2:
            path, data = args
            if not isinstance(data, bytes):
                try:
                    data = bytes(data)
                except (TypeError, ValueError):
                    data = _freeze(data)
            return (path, data)
        return tuple(_freeze(arg) for arg in args)

    @staticmethod
    def _accepts_two_positional(callback: Callable[..., Any]) -> bool:
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) >= 2

    def _invoke(
        self,
        callback: Callable[..., Any],
        hook: str,
        args: Tuple[Any, ...],
        kwargs: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        if hook == "on_file" and len(args) == 1 and isinstance(args[0], FileEvent):
            if self._accepts_two_positional(callback):
                event = args[0]
                return callback(event.path, event.data)
        elif (
            hook == "on_file"
            and len(args) == 2
            and not self._accepts_two_positional(callback)
        ):
            return callback(FileEvent.from_value({"path": args[0], "data": args[1]}))
        return callback(*args, **dict(kwargs or {}))

    @contextmanager
    def session(
        self, image: Any, metadata: Optional[Mapping[str, Any]] = None
    ) -> Iterator["PluginRegistry"]:
        """Run a complete open/close lifecycle around a caller's loop."""

        self.open(image, metadata)
        try:
            yield self
        finally:
            self.close()

    def discover(
        self,
        modules: Iterable[str] = (),
        paths: Iterable[os.PathLike] = (),
        *,
        entry_points: bool = False,
        group: str = DEFAULT_ENTRY_POINT_GROUP,
    ) -> Tuple[str, ...]:
        """Discover plugins in deterministic order.

        Discovery is never implicit.  Module names, files, and entry points are
        sorted before loading.  A module may expose ``register(registry)``,
        ``PLUGINS``, ``PLUGIN``, ``plugin`` or ``create_plugin``.
        """

        before = set(self.plugin_names)
        for module_name in sorted({str(item) for item in modules}):
            try:
                module = importlib.import_module(module_name)
                self._register_export(module, module_name)
            except Exception as exc:
                self._record_failure(module_name, "discovery", exc)

        for raw_path in sorted({os.fspath(item) for item in paths}):
            path = Path(raw_path)
            if path.suffix != ".py" or not path.is_file():
                continue
            origin = str(path)
            module_name = (
                "mountfile_plugin_%s"
                % hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
            )
            try:
                spec = importlib.util.spec_from_file_location(module_name, origin)
                if spec is None or spec.loader is None:
                    raise ImportError("cannot create import specification")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._register_export(module, origin)
            except Exception as exc:
                self._record_failure(origin, "discovery", exc)

        if entry_points:
            for entry_point in self._entry_points(group):
                origin = "%s:%s" % (group, getattr(entry_point, "name", ""))
                try:
                    self._register_export(entry_point.load(), origin)
                except Exception as exc:
                    self._record_failure(origin, "discovery", exc)
        return tuple(name for name in self.plugin_names if name not in before)

    def load_dir(self, path: os.PathLike) -> int:
        """Load ``.py`` plugins from a directory and return registrations.

        This is a compatibility spelling for explicit path discovery.  Files
        may expose the modern exports documented by :meth:`discover` or a
        class named ``Plugin`` from the original prototype.
        """

        before = len(self._states)
        directory = Path(path)
        self.discover(paths=sorted(directory.glob("*.py")))
        return len(self._states) - before

    @staticmethod
    def _entry_points(group: str) -> List[Any]:
        discovered = importlib_metadata.entry_points()
        if hasattr(discovered, "select"):
            selected = list(discovered.select(group=group))
        else:  # pragma: no cover - Python 3.8 compatibility path
            selected = list(discovered.get(group, ()))
        return sorted(
            selected,
            key=lambda item: (
                str(getattr(item, "name", "")),
                str(getattr(item, "value", "")),
            ),
        )

    @staticmethod
    def _instantiate(plugin: Any) -> Any:
        if inspect.isclass(plugin):
            return plugin()
        return plugin

    def _register_export(self, export: Any, origin: str) -> None:
        if isinstance(export, ModuleType):
            register = getattr(export, "register", None)
            if callable(register):
                count = len(self._states)
                result = register(self)
                if result is not None and len(self._states) == count:
                    self._register_export(result, origin)
                return
            for attribute in ("PLUGINS", "PLUGIN", "Plugin", "plugin", "create_plugin"):
                if hasattr(export, attribute):
                    self._register_export(getattr(export, attribute), origin)
                    return
            return
        if isinstance(export, (list, tuple, set, frozenset)):
            for item in sorted(
                export,
                key=lambda item: str(getattr(item, "name", item.__class__.__name__)),
            ):
                self._register_export(item, origin)
            return
        if callable(export) and not any(
            callable(getattr(export, hook, None))
            for hook in ("on_open", "on_file", "on_close")
        ):
            export = export()
        self.register(export)

    def _isolate(self, state: _PluginState, hook: str, exc: Exception) -> None:
        state.failed = True
        self._record_failure(state.name, hook, exc)

    def _record_failure(self, plugin: Any, hook: str, exc: Exception) -> None:
        self._errors.append(
            PluginFailure(
                plugin=str(plugin),
                hook=hook,
                exception_type=exc.__class__.__name__,
                message=str(exc),
            )
        )
