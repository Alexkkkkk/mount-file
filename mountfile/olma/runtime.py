# MOUNTFILE_FIX — OLMA runtime tracing
"""Трассировка файловых и memory-операций во время работы программы."""

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
    kind: str  # open/read/write/seek/close/error
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


class OlmaRuntime:
    """Патчит builtins.open + io.IOBase, собирает события."""

    MAX_EVENTS = 20_000

    def __init__(self, *, trace_files=True, trace_memory=False) -> None:
        self._trace_files = trace_files
        self._trace_memory = trace_memory
        self._events: deque[RuntimeEvent] = deque(maxlen=self.MAX_EVENTS)
        self._lock = threading.Lock()
        self._installed = False
        self._orig_open = None
        self._orig_io_read = None
        self._orig_io_write = None
        self._orig_io_close = None
        # Re-entrancy guard: traceback.extract_stack() can itself allocate
        # or load modules that call open() again. A second call inside the
        # hook would loop forever, which is exactly the "maximum recursion
        # depth exceeded" we see when the bootloader opens resource files
        # during startup. The hook short-circuits to the real open in that
        # case instead of re-recording and risking a stack blow.
        self._reentrant = 0

    # --- install/uninstall ---
    def install(self) -> None:
        if self._installed:
            return
        self._orig_open = builtins.open
        builtins.open = self._patched_open
        if self._trace_memory:
            self._orig_io_read = io.IOBase.read
            self._orig_io_write = io.IOBase.write
            self._orig_io_close = io.IOBase.close
            io.IOBase.read = self._patched_read
            io.IOBase.write = self._patched_write
            io.IOBase.close = self._patched_close
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if self._orig_open:
            builtins.open = self._orig_open
        if self._orig_io_read:
            io.IOBase.read = self._orig_io_read
        if self._orig_io_write:
            io.IOBase.write = self._orig_io_write
        if self._orig_io_close:
            io.IOBase.close = self._orig_io_close
        self._installed = False

    # --- hooks ---
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

    # --- patched functions ---
    def _patched_open(self, file, mode="r", *a, **kw):
        # Short-circuit if we are already inside the hook; this both breaks
        # recursion and protects against any code path (Qt, importlib,
        # traceback itself) that legitimately opens a file under the hook.
        if self._reentrant:
            return self._orig_open(file, mode, *a, **kw)
        self._reentrant += 1
        try:
            t0 = time.perf_counter()
            try:
                fh = self._orig_open(file, mode, *a, **kw)
            except Exception as e:
                self.on_error(file, e)
                raise
            dt = (time.perf_counter() - t0) * 1000
            self._push(
                RuntimeEvent(
                    "open", str(file), mode=mode, duration_ms=dt, stack=self._stack()
                )
            )
            return fh
        finally:
            self._reentrant -= 1

    def _patched_read(self, self_, *a, **kw):
        t0 = time.perf_counter()
        try:
            r = self._orig_io_read(self_, *a, **kw)
            dt = (time.perf_counter() - t0) * 1000
            n = len(r) if isinstance(r, (bytes, bytearray, str)) else 0
            p = getattr(self_, "name", "?")
            self._push(RuntimeEvent("read", str(p), size=n, duration_ms=dt))
            return r
        except Exception as e:
            self.on_error(getattr(self_, "name", "?"), e)
            raise

    def _patched_write(self, self_, *a, **kw):
        t0 = time.perf_counter()
        try:
            r = self._orig_io_write(self_, *a, **kw)
            dt = (time.perf_counter() - t0) * 1000
            n = len(a[0]) if a and isinstance(a[0], (bytes, bytearray, str)) else 0
            p = getattr(self_, "name", "?")
            self._push(RuntimeEvent("write", str(p), size=n, duration_ms=dt))
            return r
        except Exception as e:
            self.on_error(getattr(self_, "name", "?"), e)
            raise

    def _patched_close(self, self_):
        try:
            return self._orig_io_close(self_)
        finally:
            self._push(RuntimeEvent("close", str(getattr(self_, "name", "?"))))

    # --- helpers ---
    def _push(self, ev: RuntimeEvent) -> None:
        with self._lock:
            self._events.append(ev)

    def _stack(self, depth: int = 6) -> str:
        frames = traceback.extract_stack()[:-2][-depth:]
        return " | ".join(
            f"{f.filename.split(chr(47))[-1]}:{f.lineno}:{f.name}" for f in frames
        )

    # --- queries ---
    def events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in list(self._events)[-limit:]]

    def stats(self) -> dict:
        with self._lock:
            evs = list(self._events)
        s = {
            "total": len(evs),
            "open": 0,
            "read": 0,
            "write": 0,
            "close": 0,
            "error": 0,
            "bytes_read": 0,
            "bytes_written": 0,
            "slow_ops": [],
            "files_touched": set(),
            "errors": [],
        }
        for e in evs:
            s[e.kind] = s.get(e.kind, 0) + 1
            if e.size:
                if e.kind == "read":
                    s["bytes_read"] += e.size
                if e.kind == "write":
                    s["bytes_written"] += e.size
            if e.duration_ms > 100:
                s["slow_ops"].append(
                    {"path": e.path, "ms": round(e.duration_ms, 1), "kind": e.kind}
                )
            if e.path:
                s["files_touched"].add(e.path)
            if e.kind == "error":
                s["errors"].append({"path": e.path, "error": e.error, "stack": e.stack})
        s["files_touched"] = sorted(s["files_touched"])
        s["slow_ops"] = s["slow_ops"][:50]
        s["errors"] = s["errors"][:50]
        return s

    def detect_anomalies(self) -> list[dict]:
        """Ищет утечки, повторные close, ошибки."""
        with self._lock:
            evs = list(self._events)
        anomalies = []
        opens: dict[str, int] = {}
        closes: dict[str, int] = {}
        for e in evs:
            if e.kind == "open":
                opens[e.path] = opens.get(e.path, 0) + 1
            if e.kind == "close":
                closes[e.path] = closes.get(e.path, 0) + 1
            if e.kind == "error":
                anomalies.append(
                    {"type": "runtime_error", "path": e.path, "detail": e.error}
                )
        for p, n in opens.items():
            c = closes.get(p, 0)
            if n > c:
                anomalies.append(
                    {"type": "possible_leak", "path": p, "open": n, "close": c}
                )
            elif c > n:
                anomalies.append(
                    {"type": "double_close", "path": p, "open": n, "close": c}
                )
        return anomalies
