# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Optional MCP tools for safe, read-mostly mount-file investigations.

The MCP dependency is deliberately optional: importing this module never makes
``mcp`` a runtime requirement.  :class:`MountFileMcpServer` is the transport-
independent service and can be exercised with an injected backend in tests.
``create_server`` creates a :class:`mcp.server.fastmcp.FastMCP` instance only
when the optional package is installed.

Every host path accepted by a tool is resolved below either the configured
image root (for source images) or the configured workspace (for outputs and
hashing).  An image root may be one existing image file or a directory of
images.  Virtual filesystem paths (the ``path`` arguments) are separately
normalised and never treated as host paths.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Union

try:
    import regex as _timeout_regex
except ImportError:  # fail closed if the safe optional engine is unavailable
    _timeout_regex = None

try:  # Optional by design.  No MCP import should be required by mountfile.
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - depends on the environment
    FastMCP = None  # type: ignore[assignment,misc]


MCP_AVAILABLE = FastMCP is not None


class McpServerError(RuntimeError):
    """Base error for the transport-independent MCP service."""


class McpUnavailableError(McpServerError):
    """Raised when the optional MCP package or a requested backend operation is absent."""


class PathValidationError(McpServerError, ValueError):
    """Raised when a host path is outside the configured image/workspace."""


class OperationUnavailable(McpUnavailableError):
    """Raised when a backend cannot provide one of the advertised tools."""


class MountFileBackend(Protocol):
    """Backend protocol used by the MCP façade.

    Methods intentionally use ordinary Python values rather than MCP types.
    A deployment can inject a native mount-file integration, a test double, or
    a restricted remote-image adapter without coupling it to a transport.
    Implementations may raise :class:`OperationUnavailable` when an optional
    capability (for example deleted-file recovery) is not available.
    """

    def list_partitions(self, image: Path) -> Any: ...

    def list_directory(self, image: Path, offset: int, path: str) -> Any: ...

    def search_files(
        self, image: Path, offset: int, pattern: str, start: str, include_deleted: bool
    ) -> Any: ...

    def extract_file(
        self, image: Path, offset: int, path: str, destination: Path
    ) -> Any: ...

    def recover_deleted_file(
        self,
        image: Path,
        offset: int,
        inode: int,
        original_size: Optional[int],
        destination: Path,
    ) -> Any: ...

    def detect_signatures(self, image: Path, span: int) -> Any: ...

    def try_xor_decrypt(self, image: Path, key_name: str, output: Path) -> Any: ...

    def auto_decrypt(self, image: Path) -> Any: ...

    def hash_file(self, path: Path) -> Any: ...


TOOL_NAMES = (
    "list_partitions",
    "list_directory",
    "search_files",
    "extract_file",
    "recover_deleted_file",
    "detect_signatures",
    "try_xor_decrypt",
    "auto_decrypt",
    "hash_file",
)

TOOL_DESCRIPTIONS = {
    "list_partitions": "List partitions in a configured disk image.",
    "list_directory": "List entries in a filesystem directory inside a configured image.",
    "search_files": "Recursively search image filenames using a regular expression.",
    "extract_file": "Extract one live image file into the configured workspace.",
    "recover_deleted_file": "Recover an unallocated image file by inode into the workspace.",
    "detect_signatures": "Detect known filesystem signatures in a configured image.",
    "try_xor_decrypt": "Try one explicitly named XOR decoder and write only to the workspace.",
    "auto_decrypt": "Try the configured safe automatic decoder for an image.",
    "hash_file": "Calculate SHA-256 for a file already inside the workspace.",
}

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_MAX_SIGNATURE_SCAN = 64 * 1024 * 1024
_MAX_SEARCH_PATTERN = 256
_MAX_SEARCH_VISITS = 10_000
_MAX_SEARCH_RESULTS = 1_000
_MAX_SEARCH_SECONDS = 10.0
_SEARCH_REGEX_TIMEOUT = 0.05
_RISKY_REGEX = re.compile(
    r"\([^)]*[+*{|][^)]*\)[+*{]|(?:\.\*|\.\+){2}|(?:\+|\*|\{\d+(?:,\d*)?\})\s*(?:\+|\*|\{)"
)


def _json_value(value: Any) -> Any:
    """Convert common mount-file result objects into JSON-compatible values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if dataclasses.is_dataclass(value):
        return {
            key: _json_value(item) for key, item in dataclasses.asdict(value).items()
        }
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _json_value(method())
            except TypeError:
                continue
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def _require_integer(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % name)
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError("%s must be %s" % (name, qualifier))
    return value


def _virtual_path(value: Any, name: str = "path") -> str:
    """Normalise an image-internal POSIX path without touching the host FS."""

    if value is None or value == "":
        return "/"
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % name)
    if "\x00" in value or "\\" in value:
        raise ValueError("%s contains an unsafe path separator" % name)
    parts = [part for part in value.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError("%s must stay inside the image filesystem" % name)
    return "/" + "/".join(parts)


class DefaultBackend:
    """Best-effort adapter over the existing local mount-file APIs.

    Optional operations fail explicitly rather than silently pretending that a
    decoder or forensic recovery implementation exists.  A caller can inject
    a richer backend without changing the MCP surface.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @staticmethod
    def _provider(image: Path, offset: int) -> Any:
        errors = []
        try:
            from mountfile.vfs import TskImage

            return TskImage(str(image), offset=offset)
        except Exception as exc:
            errors.append(exc)
        try:
            from mountfile.fat32 import Fat32Provider

            return Fat32Provider(str(image), offset=offset, writable=False)
        except Exception as exc:
            errors.append(exc)
        detail = str(errors[-1]) if errors else "no filesystem provider"
        raise OperationUnavailable("filesystem browsing is unavailable: %s" % detail)

    def list_partitions(self, image: Path) -> Any:
        from mountfile.partition import list_partitions

        partitions = []
        for partition in list_partitions(str(image)):
            value = _json_value(partition)
            row = dict(value) if isinstance(value, Mapping) else {"value": value}
            # Partition.offset and the other useful values are properties, so
            # they are absent from vars(partition) and must be copied explicitly.
            for name in ("offset", "size_bytes", "size_mb", "type_name"):
                field = getattr(partition, name, None)
                if field is not None:
                    row[name] = _json_value(field)
            partitions.append(row)
        return partitions

    def list_directory(self, image: Path, offset: int, path: str) -> Any:
        provider = self._provider(image, offset)
        try:
            entries = []
            for item in provider.list_dir(path):
                row = dict(item)
                row.setdefault("deleted", False)
                row["path"] = (path.rstrip("/") + "/" + str(row["name"])).replace(
                    "//", "/"
                )
                entries.append(row)
            return entries
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    def search_files(
        self, image: Path, offset: int, pattern: str, start: str, include_deleted: bool
    ) -> Any:
        if len(pattern) > _MAX_SEARCH_PATTERN:
            raise ValueError(
                "search pattern exceeds %d characters" % _MAX_SEARCH_PATTERN
            )
        if _RISKY_REGEX.search(pattern):
            raise ValueError(
                "search pattern uses a potentially catastrophic regex form"
            )
        if _timeout_regex is None:
            raise ValueError(
                "safe regex search is unavailable; install the MountFile AI dependencies"
            )
        try:
            regex = _timeout_regex.compile(pattern, _timeout_regex.IGNORECASE)
        except _timeout_regex.error as exc:
            raise ValueError("invalid search pattern: %s" % exc) from exc
        matches = []
        stack = [start]
        seen = set()
        inspected = 0
        deadline = time.monotonic() + _MAX_SEARCH_SECONDS
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if len(seen) > _MAX_SEARCH_VISITS:
                raise ValueError(
                    "search exceeded directory visit limit (%d)" % _MAX_SEARCH_VISITS
                )
            for item in self.list_directory(image, offset, current):
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "search exceeded time limit (%.1f seconds)"
                        % _MAX_SEARCH_SECONDS
                    )
                inspected += 1
                if inspected > _MAX_SEARCH_VISITS:
                    raise ValueError(
                        "search exceeded entry visit limit (%d)" % _MAX_SEARCH_VISITS
                    )
                deleted = bool(item.get("deleted", False))
                if deleted and not include_deleted:
                    continue
                try:
                    matched = regex.search(
                        str(item.get("name", "")), timeout=_SEARCH_REGEX_TIMEOUT
                    )
                except TimeoutError as exc:
                    raise ValueError("search exceeded regex time limit") from exc
                if matched:
                    if len(matches) >= _MAX_SEARCH_RESULTS:
                        raise ValueError(
                            "search exceeded result limit (%d)" % _MAX_SEARCH_RESULTS
                        )
                    matches.append(item)
                if item.get("is_dir") and not deleted:
                    stack.append(str(item.get("path", "/")))
        return matches

    def extract_file(
        self, image: Path, offset: int, path: str, destination: Path
    ) -> Any:
        from mountfile.extract import extract_file

        provider = self._provider(image, offset)
        try:
            extract_file(provider, path, str(destination))
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        return destination

    def recover_deleted_file(
        self,
        image: Path,
        offset: int,
        inode: int,
        original_size: Optional[int],
        destination: Path,
    ) -> Any:
        try:
            import pytsk3
        except ImportError as exc:
            raise OperationUnavailable(
                "deleted-file recovery is unavailable: pytsk3 is not installed"
            ) from exc
        try:
            image_info = pytsk3.Img_Info(str(image))
            filesystem = pytsk3.FS_Info(image_info, offset=offset)
            file_obj = filesystem.open_meta(inode=inode)
            meta = file_obj.info.meta
            if meta is None:
                raise ValueError("inode %d has no metadata" % inode)
            size = int(meta.size)
            recovered = 0
            digest = hashlib.sha256()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                while recovered < size:
                    chunk = file_obj.read_random(
                        recovered, min(1 << 20, size - recovered)
                    )
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    recovered += len(chunk)
        except OperationUnavailable:
            raise
        except Exception as exc:
            raise RuntimeError("deleted-file recovery failed: %s" % exc) from exc
        expected = size if original_size is None else original_size
        return {
            "inode": inode,
            "path": str(destination),
            "recovered_size": recovered,
            "original_size": expected,
            "complete": recovered == expected,
            "sha256": digest.hexdigest(),
        }

    def detect_signatures(self, image: Path, span: int) -> Any:
        from mountfile.firmware import detect_volumes

        volumes = detect_volumes(str(image), scan_limit=span)
        return [
            {
                "offset": int(volume.offset),
                "kind": str(volume.fstype),
                "hint": str(volume.source),
            }
            for volume in volumes
        ]

    def try_xor_decrypt(self, image: Path, key_name: str, output: Path) -> Any:
        if key_name.upper() not in ("MTK", "MST"):
            raise OperationUnavailable(
                "XOR decryption is unavailable for key %r; supported decoders are MTK and MST"
                % key_name
            )
        from mountfile.rt809 import decode_dump

        result = decode_dump(str(image), out_path=str(output), kind=key_name.upper())
        if result is None:
            raise RuntimeError("the image has a recognised marker but is not encrypted")
        return {"output": str(result), "key": key_name.upper()}

    def auto_decrypt(self, image: Path) -> Any:
        from mountfile.rt809 import decode_dump

        output = self.workspace / (image.stem + ".decoded.bin")
        result = decode_dump(str(image), out_path=str(output))
        if result is None:
            return None
        return {"output": str(result), "key": "auto"}

    def hash_file(self, path: Path) -> Any:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()


class MountFileMcpServer:
    """Transport-independent MCP service with an injectable backend."""

    def __init__(
        self,
        image_root: Union[str, os.PathLike],
        workspace: Union[str, os.PathLike],
        backend: Optional[MountFileBackend] = None,
    ) -> None:
        configured_image = Path(image_root).expanduser().resolve()
        if (
            configured_image.exists()
            and not configured_image.is_file()
            and not configured_image.is_dir()
        ):
            raise PathValidationError(
                "configured image root is not a file or directory"
            )
        self._image_file = configured_image if configured_image.is_file() else None
        self.image_root = (
            configured_image.parent if self._image_file else configured_image
        )
        self.workspace = Path(workspace).expanduser().resolve()
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathValidationError("configured workspace is not writable") from exc
        if not self.workspace.is_dir():
            raise PathValidationError("configured workspace is not a directory")
        self.backend = (
            backend if backend is not None else DefaultBackend(self.workspace)
        )

    def _image_path(self, value: Any) -> Path:
        if value is None or value == "":
            if self._image_file is None:
                raise PathValidationError(
                    "image_path is required when image_root is a directory"
                )
            return self._image_file
        if not isinstance(value, (str, os.PathLike)):
            raise PathValidationError("image_path must be a path string")
        raw = os.fspath(value)
        if not isinstance(raw, str):
            raise PathValidationError("image_path must be a text path")
        if "\x00" in raw:
            raise PathValidationError("image_path contains a NUL byte")
        candidate = Path(raw).expanduser()
        candidate = (
            candidate if candidate.is_absolute() else self.image_root / candidate
        )
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise PathValidationError("cannot resolve image_path") from exc
        if self._image_file is not None:
            allowed = resolved == self._image_file
        else:
            allowed = _is_within(resolved, self.image_root)
        if not allowed:
            raise PathValidationError("image_path is outside the configured image root")
        if not resolved.is_file():
            raise PathValidationError("configured image does not exist: %s" % resolved)
        return resolved

    def _workspace_path(self, value: Any, *, default: Optional[Path] = None) -> Path:
        if value is None or value == "":
            if default is None:
                raise PathValidationError("workspace path is required")
            candidate = default
        else:
            if not isinstance(value, (str, os.PathLike)):
                raise PathValidationError("workspace path must be a path string")
            raw = os.fspath(value)
            if not isinstance(raw, str):
                raise PathValidationError("workspace path must be a text path")
            if "\x00" in raw:
                raise PathValidationError("workspace path contains a NUL byte")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise PathValidationError("cannot resolve workspace path") from exc
        if not _is_within(resolved, self.workspace):
            raise PathValidationError(
                "workspace path is outside the configured workspace"
            )
        return resolved

    def _call(self, operation: str, *args: Any) -> Any:
        method = getattr(self.backend, operation, None)
        if not callable(method):
            raise OperationUnavailable(
                "operation %r is unavailable: backend does not implement it" % operation
            )
        try:
            return method(*args)
        except NotImplementedError as exc:
            raise OperationUnavailable(
                "operation %r is unavailable in the configured backend" % operation
            ) from exc

    def _confine_artifact_paths(self, value: Any) -> Any:
        """Validate path-like fields returned for a workspace artifact."""

        if isinstance(value, Mapping):
            confined = {}
            for key, item in value.items():
                if key in ("dest", "output", "path") and isinstance(item, str):
                    confined[key] = str(self._workspace_path(item))
                else:
                    confined[key] = self._confine_artifact_paths(item)
            return confined
        if isinstance(value, list):
            return [self._confine_artifact_paths(item) for item in value]
        return value

    def _artifact_result(self, result: Any, destination: Path) -> Any:
        value = _json_value(result)
        if isinstance(value, Mapping):
            return self._confine_artifact_paths(value)
        size = destination.stat().st_size if destination.is_file() else None
        return {"dest": str(destination), "size": size, "result": value}

    def list_partitions(self, image_path: str) -> Any:
        return _json_value(self._call("list_partitions", self._image_path(image_path)))

    def list_directory(self, image_path: str, offset: int, path: str = "/") -> Any:
        offset = _require_integer(offset, "offset")
        return _json_value(
            self._call(
                "list_directory",
                self._image_path(image_path),
                offset,
                _virtual_path(path),
            )
        )

    def search_files(
        self,
        image_path: str,
        offset: int,
        pattern: str,
        start: str = "/",
        include_deleted: bool = True,
    ) -> Any:
        offset = _require_integer(offset, "offset")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty regular expression")
        if len(pattern) > _MAX_SEARCH_PATTERN:
            raise ValueError(
                "search pattern exceeds %d characters" % _MAX_SEARCH_PATTERN
            )
        if _RISKY_REGEX.search(pattern):
            raise ValueError(
                "search pattern uses a potentially catastrophic regex form"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("invalid search pattern: %s" % exc) from exc
        if not isinstance(include_deleted, bool):
            raise ValueError("include_deleted must be a boolean")
        return _json_value(
            self._call(
                "search_files",
                self._image_path(image_path),
                offset,
                pattern,
                _virtual_path(start, "start"),
                include_deleted,
            )
        )

    def extract_file(
        self,
        image_path: str,
        offset: int,
        path: str,
        output: Optional[str] = None,
    ) -> Any:
        offset = _require_integer(offset, "offset")
        source_path = _virtual_path(path)
        if source_path == "/":
            raise ValueError("extract_file requires a file path, not the image root")
        image = self._image_path(image_path)
        default = self.workspace / Path(source_path).name
        destination = self._workspace_path(output, default=default)
        if destination == image:
            raise PathValidationError("output must not overwrite the source image")
        result = self._call("extract_file", image, offset, source_path, destination)
        return self._artifact_result(result, destination)

    def recover_deleted_file(
        self,
        image_path: str,
        offset: int,
        inode: int,
        original_size: Optional[int] = None,
        output: Optional[str] = None,
    ) -> Any:
        offset = _require_integer(offset, "offset")
        inode = _require_integer(inode, "inode", positive=True)
        if original_size is not None:
            original_size = _require_integer(original_size, "original_size")
        image = self._image_path(image_path)
        destination = self._workspace_path(
            output, default=self.workspace / ("recovered_%d.bin" % inode)
        )
        if destination == image:
            raise PathValidationError("output must not overwrite the source image")
        result = self._call(
            "recover_deleted_file", image, offset, inode, original_size, destination
        )
        return self._artifact_result(result, destination)

    def detect_signatures(self, image_path: str, span: int = 1 << 20) -> Any:
        span = _require_integer(span, "span", positive=True)
        span = min(span, _MAX_SIGNATURE_SCAN)
        return _json_value(
            self._call("detect_signatures", self._image_path(image_path), span)
        )

    def try_xor_decrypt(
        self,
        image_path: str,
        key_name: str,
        output: Optional[str] = None,
    ) -> Any:
        if not isinstance(key_name, str) or not _SAFE_KEY.fullmatch(key_name):
            raise ValueError("key_name contains unsafe characters")
        image = self._image_path(image_path)
        default = self.workspace / (image.stem + "." + key_name + ".bin")
        destination = self._workspace_path(output, default=default)
        if destination == image:
            raise PathValidationError("output must not overwrite the source image")
        result = self._call("try_xor_decrypt", image, key_name, destination)
        return self._artifact_result(result, destination)

    def auto_decrypt(self, image_path: str) -> Any:
        image = self._image_path(image_path)
        result = self._call("auto_decrypt", image)
        value = _json_value(result)
        if isinstance(value, Mapping):
            return self._confine_artifact_paths(value)
        return value

    def hash_file(self, path: str) -> Any:
        target = self._workspace_path(path)
        if not target.is_file():
            raise PathValidationError(
                "hash_file accepts only an existing workspace file"
            )
        result = self._call("hash_file", target)
        value = _json_value(result)
        digest = value.get("sha256") if isinstance(value, Mapping) else value
        if not isinstance(digest, str) or not digest:
            raise RuntimeError("hash_file backend returned no SHA-256 digest")
        return {"path": str(target), "sha256": digest, "size": target.stat().st_size}

    def register_tools(self, app: Any) -> Any:
        """Register exactly the nine tools on an MCP-compatible app object."""

        decorator = getattr(app, "tool", None)
        if not callable(decorator):
            raise TypeError("MCP app must provide a tool decorator")
        for name in TOOL_NAMES:
            fn = getattr(self, name)
            app_decorator = decorator(name=name, description=TOOL_DESCRIPTIONS[name])
            app_decorator(fn)
        return app


def register_tools(
    app: Any,
    image_root: Union[str, os.PathLike],
    workspace: Union[str, os.PathLike],
    backend: Optional[MountFileBackend] = None,
) -> MountFileMcpServer:
    """Build a service, register its tools on ``app``, and return the service."""

    service = MountFileMcpServer(
        image_root=image_root, workspace=workspace, backend=backend
    )
    service.register_tools(app)
    return service


def create_server(
    image_root: Union[str, os.PathLike],
    workspace: Union[str, os.PathLike],
    backend: Optional[MountFileBackend] = None,
    name: str = "mount-file",
) -> Any:
    """Create an optional FastMCP server, failing clearly when MCP is absent."""

    if FastMCP is None:
        raise McpUnavailableError(
            "MCP server is unavailable: install the optional 'mcp' package"
        )
    service = MountFileMcpServer(
        image_root=image_root, workspace=workspace, backend=backend
    )
    app = FastMCP(name)
    service.register_tools(app)
    return app


# Compatibility with the standalone prototype's naming, without importing it.
build_mcp = create_server


__all__ = [
    "DefaultBackend",
    "MCP_AVAILABLE",
    "MountFileBackend",
    "MountFileMcpServer",
    "McpServerError",
    "McpUnavailableError",
    "OperationUnavailable",
    "PathValidationError",
    "TOOL_DESCRIPTIONS",
    "TOOL_NAMES",
    "build_mcp",
    "create_server",
    "register_tools",
]
