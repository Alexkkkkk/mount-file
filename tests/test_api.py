# SPDX-License-Identifier: MIT
"""Focused tests for the optional authenticated mount-file HTTP API."""

from __future__ import annotations

import pytest

from mountfile.api import APIConfigurationError, FASTAPI_AVAILABLE, create_app

if FASTAPI_AVAILABLE:
    try:
        from fastapi.testclient import TestClient
    except ImportError:  # pragma: no cover - depends on optional httpx install.
        TestClient = None
else:  # pragma: no cover - exercised when the optional API is absent.
    TestClient = None


class FakeRegistry:
    def __init__(self):
        self.calls = []

    def list_images(self):
        self.calls.append(("images",))
        return [{"name": "disk.img"}]

    def filesystem_ls(self, image, path, offset=0):
        self.calls.append(("ls", image, path, offset))
        return [{"name": "note.txt", "is_dir": False}]

    def filesystem_read(self, image, path, offset=0):
        self.calls.append(("read", image, path, offset))
        return b"evidence"

    def timeline(self, image=None, limit=100, path=None, offset=0):
        self.calls.append(("timeline", image, limit, path, offset))
        return [{"path": "/note.txt", "mtime": 1.0}]


def _client_or_skip(app):
    if TestClient is None:
        pytest.skip("FastAPI TestClient/httpx is unavailable")
    try:
        return TestClient(app)
    except (ImportError, ModuleNotFoundError, TypeError) as exc:
        pytest.skip("FastAPI TestClient is unavailable: %s" % exc)


def test_non_loopback_app_fails_closed_without_auth(tmp_path):
    registry = FakeRegistry()
    with pytest.raises(APIConfigurationError, match="bearer auth token"):
        create_app(registry, image_roots=tmp_path, host="0.0.0.0")


def test_loopback_endpoints_and_registry_injection(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"image")
    registry = FakeRegistry()
    app = create_app(registry, image_roots=tmp_path, host="127.0.0.1")
    client = _client_or_skip(app)

    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/images").json() == [{"name": "disk.img"}]

    listed = client.get("/filesystem/ls", params={"image": "disk.img", "path": "/"})
    assert listed.status_code == 200
    assert listed.json() == [{"name": "note.txt", "is_dir": False}]

    read = client.get("/filesystem/read", params={"image": "disk.img", "path": "/note.txt"})
    assert read.status_code == 200
    assert read.content == b"evidence"
    assert read.headers["content-type"].startswith("application/octet-stream")

    timeline = client.get("/timeline", params={"image": "disk.img", "limit": 10})
    assert timeline.status_code == 200
    assert timeline.json()[0]["path"] == "/note.txt"

    path_listed = client.get("/fs/disk.img/ls", params={"path": "/"})
    assert path_listed.status_code == 200
    assert path_listed.json() == [{"name": "note.txt", "is_dir": False}]
    path_read = client.get("/fs/disk.img/read", params={"path": "/note.txt"})
    assert path_read.status_code == 200
    assert path_read.content == b"evidence"
    path_timeline = client.get("/fs/disk.img/timeline", params={"limit": 10})
    assert path_timeline.status_code == 200
    assert path_timeline.json()[0]["path"] == "/note.txt"
    assert {call[0] for call in registry.calls} == {"images", "ls", "read", "timeline"}


def test_bearer_auth_protects_non_loopback_endpoints(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"image")
    app = create_app(
        FakeRegistry(),
        image_roots=[image],
        host="0.0.0.0",
        auth_token="test-token",
    )
    client = _client_or_skip(app)

    missing = client.get("/health")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert client.get("/images", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(
        "/health", headers={"Authorization": "Bearer test-token"}
    ).status_code == 200


def test_image_and_virtual_paths_cannot_escape_configured_root(tmp_path):
    image_root = tmp_path / "images"
    image_root.mkdir()
    image = image_root / "disk.img"
    image.write_bytes(b"image")
    outside = tmp_path / "outside.img"
    outside.write_bytes(b"outside")
    registry = FakeRegistry()
    app = create_app(registry, image_roots=image_root)
    client = _client_or_skip(app)

    outside_response = client.get(
        "/filesystem/ls", params={"image": str(outside), "path": "/"}
    )
    assert outside_response.status_code == 400
    traversal = client.get(
        "/filesystem/read", params={"image": "disk.img", "path": "/../outside"}
    )
    assert traversal.status_code == 400
    encoded_traversal = client.get(
        "/filesystem/ls", params={"image": "../outside.img", "path": "/"}
    )
    assert encoded_traversal.status_code == 400

    assert all(call[0] not in {"ls", "read"} for call in registry.calls)


def test_symlink_image_cannot_escape_configured_root(tmp_path):
    image_root = tmp_path / "images"
    image_root.mkdir()
    outside = tmp_path / "outside.img"
    outside.write_bytes(b"outside evidence")
    link = image_root / "linked.img"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip("symlink creation is unavailable: %s" % exc)

    registry = FakeRegistry()
    app = create_app(registry, image_roots=image_root)
    client = _client_or_skip(app)
    response = client.get(
        "/filesystem/ls", params={"image": "linked.img", "path": "/"}
    )

    assert response.status_code == 400
    assert all(call[0] != "ls" for call in registry.calls)


def test_api_module_remains_importable_without_fastapi():
    # This is intentionally an environment-sensitive assertion: when FastAPI
    # is installed the module still exposes the feature flag and factory.
    assert isinstance(FASTAPI_AVAILABLE, bool)
    if not FASTAPI_AVAILABLE:
        with pytest.raises(Exception, match="FastAPI"):
            create_app(FakeRegistry())
