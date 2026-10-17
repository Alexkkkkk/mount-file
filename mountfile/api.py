# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Optional, read-only HTTP API for mount-file integrations.

The module deliberately keeps FastAPI optional.  Importing :mod:`mountfile.api`
never makes the web stack a runtime dependency; callers opt in by installing
FastAPI and calling :func:`create_app` with an injected service registry.

The registry is transport-independent.  It may be an object or a mapping with
these operations (the aliases in parentheses are accepted for compatibility):

* ``list_images`` (``images``) -- list configured images;
* ``filesystem_ls`` (``list_directory``, ``ls``) -- list an image directory;
* ``filesystem_read`` (``read_file``, ``read``) -- read an image file;
* ``timeline`` (``get_timeline``, ``list_timeline``) -- query timeline rows.

Service methods receive a validated host image path as ``str`` and a validated
POSIX virtual path.  No service is discovered globally and no image is opened
by this module.  This keeps the API useful for both the native mount-file
backends and small test/integration adapters.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import ipaddress
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional, Protocol, Union

try:  # Optional by design: importing mountfile must not require FastAPI.
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
    from fastapi.responses import JSONResponse, Response
except ImportError:  # pragma: no cover - exercised only without the extra.
    Depends = FastAPI = Header = HTTPException = Query = None  # type: ignore[assignment]
    JSONResponse = Response = None  # type: ignore[assignment]


FASTAPI_AVAILABLE = FastAPI is not None
logger = logging.getLogger(__name__)

MAX_LIMIT = 1000
MAX_VIRTUAL_PATH = 4096
MAX_AUTH_TOKEN_LENGTH = 4096


class APIUnavailableError(RuntimeError):
    """Raised when the optional FastAPI dependency is not installed."""


class APIConfigurationError(ValueError):
    """Raised when the app would be unsafe or cannot be configured."""


class APIPathError(ValueError):
    """Raised for an image or virtual filesystem path outside its boundary."""


class ServiceUnavailableError(RuntimeError):
    """Raised when an injected registry lacks a requested operation."""


class ServiceRegistry(Protocol):
    """Minimal documentation protocol for an injected API service registry."""

    def list_images(self) -> Any: ...

    def filesystem_ls(self, image: str, path: str, offset: int = 0) -> Any: ...

    def filesystem_read(self, image: str, path: str, offset: int = 0) -> Any: ...

    def timeline(
        self, image: Optional[str] = None, limit: int = 100, path: Optional[str] = None
    ) -> Any: ...


# These are intentionally small and explicit.  A registry can expose a
# canonical name or one compatibility spelling without being coupled to this
# transport module.
_OPERATION_NAMES = {
    "images": ("list_images", "images", "get_images", "image_list"),
    "ls": ("filesystem_ls", "list_directory", "ls", "list"),
    "read": ("filesystem_read", "read_file", "file_read", "read"),
    "timeline": ("timeline", "get_timeline", "list_timeline", "timeline_events"),
    "health": ("health", "healthcheck", "health_check"),
}

# Parameter aliases let existing adapters use the terminology already used by
# MountFileMcpServer (image_path, offset, path) without an adapter shim.
_PARAMETER_ALIASES = {
    "image": {"image", "image_path", "image_file", "source", "source_image"},
    "path": {"path", "virtual_path", "filesystem_path", "fs_path", "file_path"},
    "offset": {"offset", "partition_offset", "fs_offset"},
    "limit": {"limit", "max_items", "count"},
    "query": {"query", "path_like", "filter", "pattern"},
}


def _json_value(value: Any) -> Any:
    """Convert ordinary service results to conservative JSON-compatible values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        # Binary values in metadata must not be decoded as arbitrary text.
        return {"base64": base64.b64encode(value).decode("ascii")}
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
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _is_within(candidate: Path, root: Path) -> bool:
    """Return whether *candidate* is root itself or a descendant of *root*."""

    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:  # Different drives on Windows, for example.
        return False


def _normalise_roots(
    image_roots: Optional[Union[os.PathLike, str, Sequence[Union[os.PathLike, str]]]],
) -> tuple[Path, ...]:
    if image_roots is None:
        return ()
    if isinstance(image_roots, (str, os.PathLike)):
        values = (image_roots,)
    else:
        try:
            values = tuple(image_roots)
        except TypeError as exc:
            raise APIConfigurationError(
                "image_roots must be a path or a sequence of paths"
            ) from exc
    if not values:
        return ()
    roots = []
    for value in values:
        if not isinstance(value, (str, os.PathLike)):
            raise APIConfigurationError("image_roots must contain only path values")
        raw = os.fspath(value)
        if isinstance(raw, bytes) or "\x00" in raw:
            raise APIConfigurationError(
                "image roots must be text paths without NUL bytes"
            )
        try:
            root = Path(raw).expanduser().resolve()
        except OSError as exc:
            raise APIConfigurationError("cannot resolve an image root") from exc
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _is_loopback(host: str) -> bool:
    """Recognise only explicit loopback bind addresses as local deployments."""

    value = str(host).strip().lower()
    if value in {"localhost", "localhost."}:
        return True
    value = value.strip("[]")
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _normalise_virtual_path(value: Any, name: str = "path") -> str:
    """Validate an image-internal POSIX path without touching the host filesystem."""

    if value is None or value == "":
        return "/"
    if not isinstance(value, str):
        raise APIPathError("%s must be a string" % name)
    if len(value) > MAX_VIRTUAL_PATH:
        raise APIPathError("%s is too long" % name)
    if "\x00" in value or "\\" in value:
        raise APIPathError("%s contains an unsafe path separator" % name)
    parts = [part for part in value.split("/") if part]
    # Reject traversal before normalising.  Silently turning /a/../b into /b
    # makes an invalid client request indistinguishable from a valid one.
    if any(part in {".", ".."} for part in parts):
        raise APIPathError("%s must stay inside the image filesystem" % name)
    return "/" + "/".join(parts)


def _normalise_filter(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise APIPathError("path filter must be a string")
    if "\x00" in value or "\\" in value:
        raise APIPathError("path filter contains an unsafe character")
    if len(value) > MAX_VIRTUAL_PATH:
        raise APIPathError("path filter is too long")
    if any(part in {".", ".."} for part in value.split("/")):
        raise APIPathError("path filter must stay inside the image namespace")
    return value


def _find_operation(registry: Any, operation: str) -> Any:
    names = _OPERATION_NAMES[operation]
    containers = [registry]
    services = getattr(registry, "services", None)
    if services is not None and services is not registry:
        containers.append(services)
    for container in containers:
        if isinstance(container, Mapping):
            for name in names:
                if name in container:
                    return container[name]
        else:
            for name in names:
                value = getattr(container, name, None)
                if value is not None:
                    return value
    return None


def _canonical_parameter(name: str) -> Optional[str]:
    lowered = name.lower()
    for canonical, aliases in _PARAMETER_ALIASES.items():
        if lowered in aliases:
            return canonical
    return None


def _invoke(operation: Any, values: Mapping[str, Any], operation_name: str) -> Any:
    """Call a registry operation using its declared parameter names.

    This avoids a broad TypeError retry, which could accidentally execute a
    service twice when its own implementation raises TypeError.
    """

    if not callable(operation):
        if operation_name == "images":
            return operation
        raise ServiceUnavailableError(
            "service operation %r is not callable" % operation_name
        )

    try:
        signature = inspect.signature(operation)
    except (TypeError, ValueError):
        # A small number of extension callables do not expose a signature.
        ordered = [
            values[key]
            for key in ("image", "path", "offset", "limit", "query")
            if key in values
        ]
        return operation(*ordered)

    parameters = list(signature.parameters.values())
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    positional_index = 0
    ordered_keys = ("image", "path", "offset", "limit", "query")

    for parameter in parameters:
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        canonical = _canonical_parameter(parameter.name)
        if canonical is not None and canonical in values:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(values[canonical])
            else:
                kwargs[parameter.name] = values[canonical]
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        # Unnamed required positional arguments are uncommon, but deterministic
        # positional fallback is safer than silently omitting them.
        while (
            positional_index < len(ordered_keys)
            and ordered_keys[positional_index] not in values
        ):
            positional_index += 1
        if positional_index >= len(ordered_keys):
            raise ServiceUnavailableError(
                "service operation %r has an unsupported signature" % operation_name
            )
        positional.append(values[ordered_keys[positional_index]])
        positional_index += 1

    if accepts_kwargs:
        for key, value in values.items():
            kwargs.setdefault(key, value)
    return operation(*positional, **kwargs)


def _safe_token(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_AUTH_TOKEN_LENGTH:
        raise APIConfigurationError("auth token must be a non-empty text value")
    return value


def _resolve_image(raw: Any, roots: Sequence[Path]) -> Path:
    if raw is None or raw == "":
        file_roots = [root for root in roots if root.is_file()]
        if len(file_roots) == 1:
            return file_roots[0]
        raise APIPathError("image is required when image roots are ambiguous")
    if not isinstance(raw, (str, os.PathLike)):
        raise APIPathError("image must be a path string")
    value = os.fspath(raw)
    if isinstance(value, bytes) or "\x00" in value or "\\" in value:
        raise APIPathError("image contains an unsafe path")
    candidate = Path(value).expanduser()
    matches: list[Path] = []
    for root in roots:
        if root.is_file():
            possible = candidate if candidate.is_absolute() else root.parent / candidate
            if possible.resolve(strict=False) == root:
                matches.append(root)
            continue
        possible = candidate if candidate.is_absolute() else root / candidate
        try:
            resolved = possible.resolve(strict=False)
        except OSError as exc:
            raise APIPathError("cannot resolve image") from exc
        if _is_within(resolved, root):
            matches.append(resolved)
    if not matches:
        raise APIPathError("image is outside the configured image roots")
    # A path can occur under two overlapping roots.  The resolved path is the
    # same; selecting the first does not weaken the boundary check.
    resolved = matches[0]
    if resolved.exists() and not resolved.is_file():
        raise APIPathError("image must be a file")
    return resolved


def _configured_images(roots: Sequence[Path]) -> list[dict[str, str]]:
    """Safe fallback listing when a registry does not provide image listing."""

    result: list[dict[str, str]] = []
    for root in roots:
        if root.is_file():
            result.append({"name": root.name, "path": str(root)})
        elif root.is_dir():
            try:
                children = sorted(
                    (
                        child
                        for child in root.iterdir()
                        if child.is_file()
                        and _is_within(child.resolve(strict=False), root)
                    ),
                    key=lambda p: p.name,
                )
            except OSError:
                children = []
            result.extend(
                {"name": child.name, "path": str(child)} for child in children
            )
    return result


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, APIPathError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
        )
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="operation forbidden"
        )
    if isinstance(exc, ServiceUnavailableError):
        return HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="service operation unavailable",
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    logger.exception("injected mount-file API service failed")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="service operation failed",
    )


def create_app(
    service_registry: Any = None,
    *,
    registry: Any = None,
    image_roots: Optional[
        Union[os.PathLike, str, Sequence[Union[os.PathLike, str]]]
    ] = None,
    image_root: Optional[Union[os.PathLike, str]] = None,
    host: str = "127.0.0.1",
    auth_token: Optional[str] = None,
    token: Optional[str] = None,
    title: str = "mount-file API",
) -> Any:
    """Create the optional FastAPI application.

    ``host`` is the intended bind address, not a request header.  Any address
    other than an explicit loopback address requires a configured bearer token;
    this fail-closed check prevents accidentally exposing an unauthenticated
    service on ``0.0.0.0``.  A token is also honoured on loopback when supplied.
    """

    if not FASTAPI_AVAILABLE:
        raise APIUnavailableError(
            "FastAPI is unavailable; install the optional 'fastapi' package"
        )
    if service_registry is not None and registry is not None:
        raise APIConfigurationError("provide only one service_registry or registry")
    service_registry = service_registry if service_registry is not None else registry
    if service_registry is None:
        raise APIConfigurationError("an injected service registry is required")
    if image_roots is not None and image_root is not None:
        raise APIConfigurationError("provide only one image_roots or image_root")
    if image_roots is None:
        image_roots = image_root
    roots = _normalise_roots(image_roots)

    if auth_token is not None and token is not None and auth_token != token:
        raise APIConfigurationError("auth_token and token do not match")
    configured_token = _safe_token(auth_token if auth_token is not None else token)
    if configured_token is None:
        configured_token = _safe_token(os.environ.get("MOUNTFILE_API_TOKEN"))
    if not _is_loopback(host) and configured_token is None:
        raise APIConfigurationError(
            "a bearer auth token is required when the API host is not loopback"
        )

    app = FastAPI(title=title)

    def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
        if configured_token is None:
            return
        if not isinstance(authorization, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not supplied:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # compare_digest avoids making token equality observable through timing.
        import secrets

        if not secrets.compare_digest(supplied, configured_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def _dependency() -> Any:
        return Depends(require_auth)

    @app.get("/health", dependencies=[_dependency()])
    def health() -> Any:
        operation = _find_operation(service_registry, "health")
        if operation is None:
            return {"status": "ok"}
        try:
            value = _invoke(operation, {}, "health")
        except Exception as exc:
            raise _http_error(exc)
        if isinstance(value, Mapping):
            result = _json_value(value)
            if isinstance(result, dict):
                result.setdefault("status", "ok")
                return result
        if isinstance(value, bool):
            return {"status": "ok" if value else "degraded", "healthy": value}
        return {"status": "ok", "detail": _json_value(value)}

    @app.get("/images", dependencies=[_dependency()])
    def images() -> Any:
        operation = _find_operation(service_registry, "images")
        if operation is None:
            return _configured_images(roots)
        try:
            return _json_value(_invoke(operation, {}, "images"))
        except Exception as exc:
            raise _http_error(exc)

    def _filesystem_context(
        image: Optional[str],
        path: str,
        offset: int,
    ) -> tuple[str, str, int]:
        try:
            resolved = _resolve_image(image, roots)
            virtual = _normalise_virtual_path(path)
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise APIPathError("offset must be a non-negative integer")
            return str(resolved), virtual, offset
        except Exception as exc:
            if isinstance(exc, APIPathError):
                raise
            raise APIPathError("invalid image path") from exc

    def _ls_impl(image: Optional[str], path: str, offset: int) -> Any:
        try:
            image_path, virtual_path, partition_offset = _filesystem_context(
                image, path, offset
            )
        except Exception as exc:
            raise _http_error(exc)
        operation = _find_operation(service_registry, "ls")
        if operation is None:
            raise _http_error(
                ServiceUnavailableError("filesystem listing is unavailable")
            )
        values = {"image": image_path, "path": virtual_path, "offset": partition_offset}
        try:
            return _json_value(_invoke(operation, values, "ls"))
        except Exception as exc:
            raise _http_error(exc)

    def _read_impl(image: Optional[str], path: str, offset: int) -> Any:
        try:
            image_path, virtual_path, partition_offset = _filesystem_context(
                image, path, offset
            )
        except Exception as exc:
            raise _http_error(exc)
        if virtual_path == "/":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="a file path is required",
            )
        operation = _find_operation(service_registry, "read")
        if operation is None:
            raise _http_error(
                ServiceUnavailableError("filesystem reading is unavailable")
            )
        values = {"image": image_path, "path": virtual_path, "offset": partition_offset}
        try:
            value = _invoke(operation, values, "read")
        except Exception as exc:
            raise _http_error(exc)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return Response(content=bytes(value), media_type="application/octet-stream")
        return JSONResponse(content=_json_value(value))

    @app.get("/filesystem/ls", dependencies=[_dependency()])
    def filesystem_ls(
        image: Optional[str] = Query(default=None),
        path: str = Query(default="/"),
        offset: int = Query(default=0),
    ) -> Any:
        return _ls_impl(image, path, offset)

    @app.get("/fs/ls", include_in_schema=False, dependencies=[_dependency()])
    def fs_ls(
        image: Optional[str] = Query(default=None),
        path: str = Query(default="/"),
        offset: int = Query(default=0),
    ) -> Any:
        return _ls_impl(image, path, offset)

    @app.get("/filesystem/read", dependencies=[_dependency()])
    def filesystem_read(
        image: Optional[str] = Query(default=None),
        path: str = Query(...),
        offset: int = Query(default=0),
    ) -> Any:
        return _read_impl(image, path, offset)

    @app.get("/fs/read", include_in_schema=False, dependencies=[_dependency()])
    def fs_read(
        image: Optional[str] = Query(default=None),
        path: str = Query(...),
        offset: int = Query(default=0),
    ) -> Any:
        return _read_impl(image, path, offset)

    def _timeline_impl(
        image: Optional[str],
        limit: int,
        path: Optional[str],
        offset: int,
    ) -> Any:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIMIT
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit must be between 1 and %d" % MAX_LIMIT,
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="offset must be a non-negative integer",
            )
        try:
            filter_path = _normalise_filter(path)
            image_path = None if image is None else str(_resolve_image(image, roots))
        except Exception as exc:
            if isinstance(exc, APIPathError):
                raise _http_error(exc)
            raise _http_error(APIPathError("invalid image path"))
        operation = _find_operation(service_registry, "timeline")
        if operation is None:
            raise _http_error(ServiceUnavailableError("timeline is unavailable"))
        values = {
            "image": image_path,
            "limit": limit,
            "path": filter_path,
            "offset": offset,
        }
        try:
            return _json_value(_invoke(operation, values, "timeline"))
        except Exception as exc:
            raise _http_error(exc)

    @app.get("/timeline", dependencies=[_dependency()])
    def timeline(
        image: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        path: Optional[str] = Query(default=None),
        offset: int = Query(default=0),
    ) -> Any:
        return _timeline_impl(image, limit, path, offset)

    @app.get("/timeline/events", include_in_schema=False, dependencies=[_dependency()])
    def timeline_events(
        image: Optional[str] = Query(default=None),
        limit: int = Query(default=100),
        path: Optional[str] = Query(default=None),
        offset: int = Query(default=0),
    ) -> Any:
        return _timeline_impl(image, limit, path, offset)

    return app


# Descriptive aliases keep integrations concise without creating another app
# construction path.
create_api = create_app
build_app = create_app


__all__ = [
    "APIConfigurationError",
    "APIPathError",
    "APIUnavailableError",
    "FASTAPI_AVAILABLE",
    "ServiceRegistry",
    "ServiceUnavailableError",
    "build_app",
    "create_api",
    "create_app",
]
