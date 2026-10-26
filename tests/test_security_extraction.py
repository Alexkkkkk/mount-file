"""Security regressions for extraction destinations and OpenAI API-key transport."""

import io
import json
import os
import urllib.error

import pytest

from mountfile import extract as extract_module
from mountfile.ai.backend import OpenAiCompatBackend
from mountfile.extract import ExtractError, extract_file, extract_path


class TreeProvider:
    def list_dir(self, path="/"):
        if path == "/":
            return [{"name": "secret.bin", "is_dir": False}]
        raise AssertionError("unexpected directory: %s" % path)

    def read_file(self, path):
        assert path == "/secret.bin"
        return b"image payload"

    def file_size(self, path):
        return len(self.read_file(path))


class RangeProvider:
    def __init__(self, payload, *, fail_at=None):
        self.payload = payload
        self.fail_at = fail_at
        self.calls = []

    def file_size(self, path):
        assert path == "/large.bin"
        return len(self.payload)

    def read_file_range(self, path, offset, length):
        assert path == "/large.bin"
        self.calls.append((offset, length))
        if self.fail_at is not None and offset >= self.fail_at:
            raise ValueError("simulated source read failure")
        return self.payload[offset : offset + length]

    def read_file(self, path):
        raise AssertionError("ranged providers must not be read all at once")


@pytest.mark.parametrize(
    "use_dir_fd", sorted({False, bool(extract_module._HAS_DIR_FD)})
)
def test_extract_streams_ranged_provider_in_bounded_chunks(
    tmp_path, monkeypatch, use_dir_fd
):
    payload = bytes(range(10))
    provider = RangeProvider(payload)
    destination = tmp_path / "large.bin"
    monkeypatch.setattr(extract_module, "_HAS_DIR_FD", use_dir_fd)
    monkeypatch.setattr(extract_module, "_COPY_CHUNK_SIZE", 4)

    assert extract_file(provider, "/large.bin", destination) == len(payload)

    assert destination.read_bytes() == payload
    assert provider.calls == [(0, 4), (4, 4), (8, 2)]


@pytest.mark.parametrize(
    "use_dir_fd", sorted({False, bool(extract_module._HAS_DIR_FD)})
)
def test_streaming_failure_preserves_existing_destination_and_cleans_temp(
    tmp_path, monkeypatch, use_dir_fd
):
    provider = RangeProvider(b"abcdefghij", fail_at=4)
    destination = tmp_path / "large.bin"
    destination.write_bytes(b"old contents")
    monkeypatch.setattr(extract_module, "_HAS_DIR_FD", use_dir_fd)
    monkeypatch.setattr(extract_module, "_COPY_CHUNK_SIZE", 4)

    with pytest.raises(ValueError, match="simulated source read failure"):
        extract_file(provider, "/large.bin", destination)

    assert destination.read_bytes() == b"old contents"
    assert list(tmp_path.glob(".mountfile-*.tmp")) == []


def test_extract_does_not_follow_destination_symlink(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"must remain unchanged")
    link = output / "secret.bin"
    try:
        link.symlink_to(protected)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in test environment")

    with pytest.raises(ExtractError, match="symlink|safely write"):
        extract_path(TreeProvider(), "/", output)

    assert protected.read_bytes() == b"must remain unchanged"
    assert link.is_symlink()


def test_extract_file_does_not_follow_intermediate_destination_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "secret.bin"
    protected.write_bytes(b"must remain unchanged")
    destination_root = tmp_path / "output"
    destination_root.mkdir()
    try:
        (destination_root / "nested").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in test environment")

    with pytest.raises(ExtractError, match="symlink|safely"):
        extract_file(
            TreeProvider(), "/secret.bin", destination_root / "nested/secret.bin"
        )

    assert protected.read_bytes() == b"must remain unchanged"


def test_extract_replaces_hardlink_without_mutating_external_file(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"must remain unchanged")
    destination = output / "secret.bin"
    try:
        os.link(protected, destination)
    except OSError:
        pytest.skip("hard links unavailable in test environment")

    extract_path(TreeProvider(), "/", output)

    assert protected.read_bytes() == b"must remain unchanged"
    assert destination.read_bytes() == b"image payload"
    assert destination.stat().st_ino != protected.stat().st_ino


def test_extract_does_not_follow_symlinked_parent_directory(tmp_path):
    class NestedProvider(TreeProvider):
        def list_dir(self, path="/"):
            if path == "/":
                return [{"name": "nested", "is_dir": True}]
            if path == "/nested":
                return [{"name": "secret.bin", "is_dir": False}]
            raise AssertionError("unexpected directory: %s" % path)

        def read_file(self, path):
            assert path == "/nested/secret.bin"
            return b"image payload"

    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "secret.bin"
    protected.write_bytes(b"must remain unchanged")
    try:
        (output / "nested").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in test environment")

    with pytest.raises(ExtractError, match="safely create directory|symlink"):
        extract_path(NestedProvider(), "/", output)

    assert protected.read_bytes() == b"must remain unchanged"


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


def _success_response():
    return FakeResponse({"choices": [{"message": {"content": "ok"}}]})


def test_openai_backend_rejects_remote_http_with_api_key_without_request(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None, allow_redirects=True):
        calls.append(request.full_url)
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend._open_request", fake_urlopen)
    backend = OpenAiCompatBackend(
        base_url="http://api.example.invalid", model="test", api_key="secret"
    )

    result = backend.analyze("sample.bin", b"data")

    assert not result.ok
    assert "HTTPS" in result.error
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:1234", "http://127.0.0.1:1234", "http://[::1]:1234"],
)
def test_openai_backend_allows_loopback_http_with_api_key(monkeypatch, base_url):
    calls = []

    def fake_urlopen(request, timeout=None, allow_redirects=True):
        calls.append((request.full_url, request.get_header("Authorization")))
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend._open_request", fake_urlopen)
    backend = OpenAiCompatBackend(base_url=base_url, model="test", api_key="secret")

    result = backend.analyze("sample.bin", b"data")

    assert result.ok
    assert calls == [(base_url + "/v1/chat/completions", "Bearer secret")]


def test_openai_backend_allows_https_with_api_key(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None, allow_redirects=True):
        calls.append((request.full_url, request.get_header("Authorization")))
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend._open_request", fake_urlopen)
    backend = OpenAiCompatBackend(
        base_url="https://api.example.test", model="test", api_key="secret"
    )

    result = backend.analyze("sample.bin", b"data")

    assert result.ok
    assert calls == [("https://api.example.test/v1/chat/completions", "Bearer secret")]


def test_openai_backend_refuses_redirects_with_api_key(monkeypatch):
    import mountfile.ai.backend as ai_backend

    calls = []

    class RedirectResponse:
        def open(self, request, timeout=None):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {}, io.BytesIO(b"redirect")
            )

    def build_opener(handler):
        assert isinstance(handler, ai_backend._NoRedirectHandler)
        return RedirectResponse()

    monkeypatch.setattr(ai_backend.urllib.request, "build_opener", build_opener)
    backend = OpenAiCompatBackend(
        base_url="https://api.example.test", model="test", api_key="secret"
    )
    result = backend.analyze("sample.bin", b"data")

    assert not result.ok
    assert len(calls) == 1
    handler = ai_backend._NoRedirectHandler()
    assert (
        handler.redirect_request(
            calls[0], None, 302, "Found", {}, "http://attacker.invalid/collect"
        )
        is None
    )
