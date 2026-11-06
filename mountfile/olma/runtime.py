# MOUNTFILE_FIX — OLMA runtime tracing
"""Opt-in tracing of builtins.open and newly created BytesIO/StringIO streams.

C extension IO classes are immutable. Instrument supported factories instead of
attempting to patch IOBase; existing streams and C-created handles are not traced.
"""

from __future__ import annotations

import builtins
import io
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class RuntimeEvent:
    kind: str
    path: str
    offset: int = 0
    size: int = 0
    duration_ms: float = 0.0
    mode: str = ""
    error: str = ""
    stack: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class _TracedFile:
    """Delegate a file's API while recording operations and one close event."""

    def __init__(self, handle, tracer):
        self._handle = handle
        self._tracer = tracer
        self._close_recorded = False

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def _operation(self, name, kind, *args, **kwargs):
        started = time.perf_counter()
        try:
            offset = self._handle.tell()
        except (OSError, ValueError):
            offset = 0
        try:
            result = getattr(self._handle, name)(*args, **kwargs)
        except Exception as exc:
            self._tracer.on_error(getattr(self._handle, "name", "?"), exc)
            raise
        size = 0
        if kind == "read":
            if isinstance(result, (str, bytes, bytearray)):
                size = len(result)
            elif isinstance(result, int):
                size = result
            elif isinstance(result, list):
                size = sum(len(line) for line in result)
        elif kind == "write":
            size = result if isinstance(result, int) else 0
        self._tracer._push(
            RuntimeEvent(
                kind,
                str(getattr(self._handle, "name", "?")),
                offset=offset,
                size=size,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return result

    def read(self, *args, **kwargs):
        return self._operation("read", "read", *args, **kwargs)

    def read1(self, *args, **kwargs):
        return self._operation("read1", "read", *args, **kwargs)

    def readinto(self, *args, **kwargs):
        return self._operation("readinto", "read", *args, **kwargs)

    def readinto1(self, *args, **kwargs):
        return self._operation("readinto1", "read", *args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._operation("readline", "read", *args, **kwargs)

    def readlines(self, *args, **kwargs):
        return self._operation("readlines", "read", *args, **kwargs)

    def write(self, *args, **kwargs):
        return self._operation("write", "write", *args, **kwargs)

    def writelines(self, lines):
        # Preserve generator/stream behavior; do not materialize the input.
        for line in lines:
            self.write(line)

    def seek(self, *args, **kwargs):
        return self._operation("seek", "seek", *args, **kwargs)

    def close(self):
        try:
            return self._handle.close()
        finally:
            if self._handle.closed and not self._close_recorded:
                self._close_recorded = True
                self._tracer.on_close(getattr(self._handle, "name", "?"))

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __del__(self):
        try:
            self.close()
        except Exception:
            # Interpreter shutdown can invalidate tracing infrastructure.
            return


def _memory_factory(base, tracer):
    class TracedMemory(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._trace_name = "<%s:%x>" % (base.__name__, id(self))
            self._trace_closed = False
            tracer.on_open(self._trace_name, "memory")

        def read(self, *args, **kwargs):
            offset = self.tell()
            result = super().read(*args, **kwargs)
            tracer.on_read(self._trace_name, offset, len(result))
            return result

        def readline(self, *args, **kwargs):
            offset = self.tell()
            result = super().readline(*args, **kwargs)
            tracer.on_read(self._trace_name, offset, len(result))
            return result

        def readinto(self, *args, **kwargs):
            offset = self.tell()
            result = super().readinto(*args, **kwargs)
            tracer.on_read(self._trace_name, offset, result)
            return result

        def write(self, *args, **kwargs):
            offset = self.tell()
            result = super().write(*args, **kwargs)
            tracer.on_write(self._trace_name, offset, result)
            return result

        def close(self):
            try:
                return super().close()
            finally:
                if self.closed and not getattr(self, "_trace_closed", True):
                    self._trace_closed = True
                    tracer.on_close(self._trace_name)

    TracedMemory.__name__ = base.__name__
    return TracedMemory


class OlmaRuntime:
    """Temporarily instrument supported IO factories; uninstall in LIFO order."""

    MAX_EVENTS = 20_000

    def __init__(self, *, trace_files=True, trace_memory=False) -> None:
        self._trace_files = trace_files
        self._trace_memory = trace_memory
        self._events: deque[RuntimeEvent] = deque(maxlen=self.MAX_EVENTS)
        self._lock = threading.Lock()
        self._installed = False
        self._orig_open = None
        self._memory_factories = {}
        self._reentrant = threading.local()

    def install(self) -> None:
        if self._installed:
            return
        if self._trace_files:
            self._orig_open = builtins.open
            builtins.open = self._patched_open
        if self._trace_memory:
            for name in ("BytesIO", "StringIO"):
                original = getattr(io, name)
                replacement = _memory_factory(original, self)
                self._memory_factories[name] = (original, replacement)
                setattr(io, name, replacement)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if self._trace_files and builtins.open != self._patched_open:
            raise RuntimeError(
                "IO tracing must be uninstalled in reverse install order"
            )
        if any(
            getattr(io, name) is not replacement
            for name, (_, replacement) in self._memory_factories.items()
        ):
            raise RuntimeError(
                "IO tracing must be uninstalled in reverse install order"
            )
        if self._trace_files:
            builtins.open = self._orig_open
        for name, (original, _) in self._memory_factories.items():
            setattr(io, name, original)
        self._memory_factories.clear()
        self._installed = False

    def on_open(self, path, mode):
        self._push(RuntimeEvent("open", str(path), mode=mode, stack=self._stack()))

    def on_read(self, path, off, size):
        self._push(RuntimeEvent("read", str(path), offset=off, size=size))

    def on_write(self, path, off, size):
        self._push(RuntimeEvent("write", str(path), offset=off, size=size))

    def on_close(self, path):
        self._push(RuntimeEvent("close", str(path)))

    def on_error(self, path, err):
        self._push(
            RuntimeEvent("error", str(path), error=repr(err), stack=self._stack())
        )

    def _patched_open(self, file, mode="r", *args, **kwargs):
        if getattr(self._reentrant, "active", False):
            return self._orig_open(file, mode, *args, **kwargs)
        self._reentrant.active = True
        try:
            started = time.perf_counter()
            try:
                handle = self._orig_open(file, mode, *args, **kwargs)
            except Exception as exc:
                self.on_error(file, exc)
                raise
            self._push(
                RuntimeEvent(
                    "open",
                    str(file),
                    mode=mode,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    stack=self._stack(),
                )
            )
            return _TracedFile(handle, self)
        finally:
            self._reentrant.active = False

    def _push(self, event):
        with self._lock:
            self._events.append(event)

    def _stack(self, depth=6):
        frames = traceback.extract_stack()[:-2][-depth:]
        return " | ".join(
            f"{frame.filename.split(chr(47))[-1]}:{frame.lineno}:{frame.name}"
            for frame in frames
        )

    def events(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            return (
                [event.to_dict() for event in list(self._events)[-limit:]]
                if limit
                else []
            )

    def stats(self):
        with self._lock:
            events = list(self._events)
        result = {
            "total": len(events),
            "open": 0,
            "read": 0,
            "write": 0,
            "seek": 0,
            "close": 0,
            "error": 0,
            "bytes_read": 0,
            "bytes_written": 0,
            "slow_ops": [],
            "files_touched": set(),
            "errors": [],
        }
        for event in events:
            result[event.kind] = result.get(event.kind, 0) + 1
            if event.kind == "read":
                result["bytes_read"] += event.size
            elif event.kind == "write":
                result["bytes_written"] += event.size
            if event.duration_ms > 100:
                result["slow_ops"].append(
                    {
                        "path": event.path,
                        "ms": round(event.duration_ms, 1),
                        "kind": event.kind,
                    }
                )
            if event.path:
                result["files_touched"].add(event.path)
            if event.kind == "error":
                result["errors"].append(
                    {"path": event.path, "error": event.error, "stack": event.stack}
                )
        result["files_touched"] = sorted(result["files_touched"])
        result["slow_ops"] = result["slow_ops"][:50]
        result["errors"] = result["errors"][:50]
        return result

    def detect_anomalies(self):
        with self._lock:
            events = list(self._events)
        anomalies, opens, closes = [], {}, {}
        for event in events:
            if event.kind == "open":
                opens[event.path] = opens.get(event.path, 0) + 1
            elif event.kind == "close":
                closes[event.path] = closes.get(event.path, 0) + 1
            elif event.kind == "error":
                anomalies.append(
                    {"type": "runtime_error", "path": event.path, "detail": event.error}
                )
        for path in opens.keys() | closes.keys():
            count, closed = opens.get(path, 0), closes.get(path, 0)
            if count > closed:
                anomalies.append(
                    {
                        "type": "possible_leak",
                        "path": path,
                        "open": count,
                        "close": closed,
                    }
                )
            elif closed > count:
                anomalies.append(
                    {
                        "type": "double_close",
                        "path": path,
                        "open": count,
                        "close": closed,
                    }
                )
        return anomalies
