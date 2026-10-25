"""Security regressions for extraction destinations and OpenAI API-key transport."""

import json

import pytest

from mountfile.ai.backend import OpenAiCompatBackend
from mountfile.extract import ExtractError, extract_path


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

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", fake_urlopen)
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

    def fake_urlopen(request, timeout=None):
        calls.append((request.full_url, request.get_header("Authorization")))
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", fake_urlopen)
    backend = OpenAiCompatBackend(base_url=base_url, model="test", api_key="secret")

    result = backend.analyze("sample.bin", b"data")

    assert result.ok
    assert calls == [(base_url + "/v1/chat/completions", "Bearer secret")]


def test_openai_backend_allows_https_with_api_key(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.full_url, request.get_header("Authorization")))
        return _success_response()

    monkeypatch.setattr("mountfile.ai.backend.urllib.request.urlopen", fake_urlopen)
    backend = OpenAiCompatBackend(
        base_url="https://api.example.test", model="test", api_key="secret"
    )

    result = backend.analyze("sample.bin", b"data")

    assert result.ok
    assert calls == [
        ("https://api.example.test/v1/chat/completions", "Bearer secret")
    ]
