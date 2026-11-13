"""Offline regressions for real optional API/AI/DFIR integration paths."""

from __future__ import annotations

import builtins
import io
import os
import sys
from types import SimpleNamespace

import pytest

from mountfile import crypto, search_content, timeline
from mountfile.ai import backend
from mountfile.ai.mcp_server import (
    DefaultBackend,
    MountFileMcpServer,
    PathValidationError,
)
from mountfile.olma.core import OlmaBoundsError, OlmaHandle, OlmaXorDecoder
from mountfile.olma.runtime import OlmaRuntime


def api_client(tmp_path, registry, **kwargs):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mountfile.api import create_app

    image = tmp_path / "case.img"
    image.write_bytes(b"evidence")
    return TestClient(create_app(registry, image_roots=image, **kwargs))


def test_api_unicode_token_does_not_crash(tmp_path):
    client = api_client(tmp_path, {}, auth_token="секрет")
    assert (
        client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


@pytest.mark.parametrize("endpoint", ["ls", "read", "status"])
def test_api_nonzero_partition_requires_adapter_support(tmp_path, endpoint):
    def legacy(image, path=None, length=100):
        raise AssertionError("must not silently ignore partition offset")

    client = api_client(tmp_path, {"filesystem_" + endpoint: legacy})
    response = client.get(
        "/filesystem/" + endpoint, params={"offset": 512, "path": "/note"}
    )
    assert response.status_code == 501


def test_api_reader_must_respect_requested_length(tmp_path):
    client = api_client(
        tmp_path, {"filesystem_read": lambda image, path, length: b"too much"}
    )
    response = client.get("/filesystem/read", params={"path": "/note", "length": 1})
    assert response.status_code == 413


def test_api_ambiguous_image_id_is_not_arbitrarily_selected(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from mountfile.api import create_app

    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
        (root / "case.img").write_bytes(b"evidence")
    client = TestClient(
        create_app({"filesystem_ls": lambda **kwargs: []}, image_roots=roots)
    )
    response = client.get("/filesystem/ls", params={"image": "case.img"})
    assert response.status_code == 400
    assert "ambiguous" in response.json()["detail"]


@pytest.mark.parametrize("chunk", [0, -1, True])
def test_ai_collect_invalid_chunk_fails_instead_of_looping(chunk):
    with pytest.raises(ValueError, match="positive integer"):
        backend.collect(lambda offset, length: b"x", chunk=chunk)


def test_ai_collect_checks_provider_contract():
    with pytest.raises(ValueError, match="exceeded"):
        backend.collect(lambda offset, length: b"123", max_bytes=2)


@pytest.mark.parametrize("payload", [[], "bad", {"response": 42}])
def test_ollama_malformed_json_payload_returns_error(monkeypatch, payload):
    monkeypatch.setattr(backend, "_post_json", lambda *args, **kwargs: (payload, ""))
    result = backend.OllamaBackend().analyze("sample", b"data")
    assert result.ok is False
    assert result.error


def test_openai_standard_v1_base_and_loopback_without_key(monkeypatch):
    captured = []

    def post(url, *args, **kwargs):
        captured.append(url)
        return {"choices": [{"message": {"content": "local result"}}]}, ""

    monkeypatch.setattr(backend, "_post_json", post)
    result = backend.OpenAiCompatBackend(
        base_url="http://127.0.0.2:8000/v1", api_key=""
    ).analyze("sample", b"data")
    assert result.ok is True
    assert captured == ["http://127.0.0.2:8000/v1/chat/completions"]


@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",
        "https://server:99999",
        "https://user:secret@server",
        "https://server?token=x",
    ],
)
def test_openai_invalid_endpoint_fails_before_network(monkeypatch, url):
    monkeypatch.setattr(
        backend, "_post_json", lambda *args, **kwargs: pytest.fail("network")
    )
    assert (
        backend.OpenAiCompatBackend(base_url=url, api_key="key")
        .analyze("sample", b"data")
        .ok
        is False
    )


def test_openai_error_cannot_echo_key(monkeypatch):
    monkeypatch.setattr(
        backend, "_post_json", lambda *args, **kwargs: (None, "bad key super-secret")
    )
    result = backend.OpenAiCompatBackend(api_key="super-secret").analyze(
        "sample", b"data"
    )
    assert "super-secret" not in result.error


def test_mcp_hardlink_output_must_not_corrupt_evidence(tmp_path):
    image = tmp_path / "case.img"
    image.write_bytes(b"source")
    service = MountFileMcpServer(image, tmp_path / "work", backend=object())
    os.link(image, service.workspace / "alias")
    with pytest.raises(PathValidationError, match="source image"):
        service.extract_file(str(image), 0, "/note", output="alias")
    assert image.read_bytes() == b"source"


def test_default_auto_decrypt_validates_output_before_decoder(tmp_path, monkeypatch):
    from mountfile import rt809

    image = tmp_path / "case.img"
    image.write_bytes(b"source")
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (workspace / "case.decoded.bin").symlink_to(outside)
    monkeypatch.setattr(
        rt809, "decode_dump", lambda *args, **kwargs: pytest.fail("unsafe decoder call")
    )
    with pytest.raises(PathValidationError, match="outside"):
        DefaultBackend(workspace).auto_decrypt(image)
    assert outside.read_bytes() == b"keep"


def test_mcp_recovery_refuses_allocated_inode(tmp_path, monkeypatch):
    fake_file = SimpleNamespace(
        info=SimpleNamespace(meta=SimpleNamespace(flags=1, size=3))
    )
    fake_fs = SimpleNamespace(open_meta=lambda **kwargs: fake_file)
    fake_tsk = SimpleNamespace(
        Img_Info=lambda path: object(),
        FS_Info=lambda *args, **kwargs: fake_fs,
        TSK_FS_META_FLAG_UNALLOC=2,
    )
    monkeypatch.setitem(sys.modules, "pytsk3", fake_tsk)
    destination = tmp_path / "output"
    with pytest.raises(RuntimeError, match="allocated"):
        DefaultBackend(tmp_path).recover_deleted_file(
            tmp_path / "image", 0, 1, None, destination
        )
    assert not destination.exists()


@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be"])
def test_utf16_regex_offsets_account_for_non_bmp_characters(encoding):
    blob = "𝄞abc TARGET".encode(encoding)
    hit = search_content.search_blob(
        blob, "TARGET", encoding=encoding, regex=True, context=3
    )[0]
    assert hit.offset == len("𝄞abc ".encode(encoding))
    assert hit.length == len("TARGET".encode(encoding))
    assert hit.exact_offset is True
    assert "TARGET" in hit.context


def test_search_regions_max_hits_is_global(tmp_path):
    image = tmp_path / "case.img"
    image.write_bytes(b"xx...xx")
    hits = search_content.search_regions(image, [(0, 2), (5, 2)], "x", max_hits=1)
    assert len(hits) == 1
    assert search_content.search_image(image, "x", max_hits=0) == []


def test_invalid_search_is_not_disguised_as_clean_empty_filesystem():
    fs = SimpleNamespace(open_dir=lambda path: [])
    with pytest.raises(search_content.SearchError, match="invalid regex"):
        search_content.search_fs(fs, "[", regex=True)


def test_timeline_ignores_missing_metadata_and_stops_directory_cycles():
    broken = SimpleNamespace(
        info=SimpleNamespace(name=SimpleNamespace(name=b"lost"), meta=None)
    )
    directory = SimpleNamespace(
        info=SimpleNamespace(
            name=SimpleNamespace(name=b"loop"), meta=SimpleNamespace(type=2, addr=5)
        )
    )
    directory.as_directory = lambda: [directory, broken]
    fs = SimpleNamespace(open_dir=lambda path: [directory, broken])
    assert list(timeline.iter_file_entries(fs)) == []


def test_timeline_does_not_hash_partial_data():
    entry = SimpleNamespace(info=SimpleNamespace(meta=SimpleNamespace(size=10)))
    entry.read_random = lambda offset, length: b"abc" if offset == 0 else b""
    assert timeline._hash_entry(entry, None) is None


def test_timeline_partition_offset_reaches_tsk(tmp_path, monkeypatch):
    image = tmp_path / "image"
    image.write_bytes(b"data")
    captured = []
    fake_tsk = SimpleNamespace(
        Img_Info=lambda path: path,
        FS_Info=lambda image, **kwargs: captured.append(kwargs) or "fs",
    )
    monkeypatch.setitem(sys.modules, "pytsk3", fake_tsk)
    assert timeline.open_tsk_fs(image, offset=512) == "fs"
    assert captured == [{"offset": 512}]


def test_olma_xor_is_independent_of_chunk_size_and_write_updates_size(tmp_path):
    image, output = tmp_path / "source", tmp_path / "output"
    data = b"0123456789abcdef"
    image.write_bytes(data)
    decoder = OlmaXorDecoder(b"abc")
    with OlmaHandle(image) as source, OlmaHandle(output, "w+b") as target:
        assert decoder.decode_file(source, target, chunk=4) == len(data)
        assert target.size == len(data)
        assert target.read_at(0, target.size) == decoder.decode(data)
        with pytest.raises(OlmaBoundsError):
            decoder.decode_file(source, target, chunk=0)


def test_olma_runtime_records_real_file_and_memory_operations(tmp_path):
    original_open, original_bytes, original_text = (
        builtins.open,
        io.BytesIO,
        io.StringIO,
    )
    tracer = OlmaRuntime(trace_files=True, trace_memory=True)
    tracer.install()
    try:
        with open(tmp_path / "file", "w+b") as stream:
            assert stream.write(b"abc") == 3
            stream.seek(0)
            assert stream.read() == b"abc"
        with io.BytesIO(b"hello") as stream:
            assert stream.read(2) == b"he"
            stream.write(b"!")
        with io.StringIO("hello") as stream:
            assert stream.read(2) == "he"
        assert tracer.stats()["bytes_read"] >= 7
        assert tracer.stats()["bytes_written"] >= 4
        assert tracer.events(0) == []
        assert tracer.detect_anomalies() == []
    finally:
        tracer.uninstall()
    assert builtins.open is original_open
    assert io.BytesIO is original_bytes
    assert io.StringIO is original_text


def test_crypto_adapters_are_read_only_and_luks_stdin_is_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto.shutil, "which", lambda name: "/fake/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    crypto.unlock_luks(tmp_path / "image", "mapping", password="secret", runner=run)
    assert "--readonly" in calls[0][0]
    assert calls[0][1]["input"] == b"secret"
    crypto.unlock_bitlocker(
        tmp_path / "image", tmp_path / "fuse", password="secret", runner=run
    )
    assert "-r" in calls[1][0]
    with pytest.raises(crypto.CryptoOperationError, match="partition-specific"):
        crypto.unlock_luks(tmp_path / "image", "mapping", offset=512, runner=run)
    assert len(calls) == 2
