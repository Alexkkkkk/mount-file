# SPDX-License-Identifier: MIT
"""Focused tests for the optional mount-file MCP façade."""

import struct
from pathlib import Path

import pytest

from mountfile.ai.mcp_server import (
    DefaultBackend,
    MCP_AVAILABLE,
    MountFileMcpServer,
    OperationUnavailable,
    PathValidationError,
    TOOL_NAMES,
    create_server,
)


class FakeBackend:
    def __init__(self):
        self.calls = []

    def list_partitions(self, image):
        self.calls.append(("list_partitions", image))
        return [{"offset": 512, "kind": "fake"}]

    def list_directory(self, image, offset, path):
        self.calls.append(("list_directory", image, offset, path))
        return [{"name": "note.txt", "path": "/note.txt", "is_dir": False}]

    def search_files(self, image, offset, pattern, start, include_deleted):
        self.calls.append(("search_files", image, offset, pattern, start, include_deleted))
        return [{"name": "note.txt", "path": "/note.txt"}]

    def extract_file(self, image, offset, path, destination):
        self.calls.append(("extract_file", image, offset, path, destination))
        destination.write_bytes(b"extracted")
        return destination

    def recover_deleted_file(self, image, offset, inode, original_size, destination):
        self.calls.append(
            ("recover_deleted_file", image, offset, inode, original_size, destination)
        )
        destination.write_bytes(b"recovered")
        return {"inode": inode, "path": str(destination), "complete": True}

    def detect_signatures(self, image, span):
        self.calls.append(("detect_signatures", image, span))
        return [{"offset": 0, "kind": "fake"}]

    def try_xor_decrypt(self, image, key_name, output):
        self.calls.append(("try_xor_decrypt", image, key_name, output))
        output.write_bytes(b"decoded")
        return {"output": str(output), "key": key_name}

    def auto_decrypt(self, image):
        self.calls.append(("auto_decrypt", image))
        return None

    def hash_file(self, path):
        self.calls.append(("hash_file", path))
        return "a" * 64


class FakeMcpApp:
    def __init__(self):
        self.registered = {}

    def tool(self, *, name, description):
        def decorate(fn):
            self.registered[name] = (fn, description)
            return fn

        return decorate


def make_service(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"image")
    workspace = tmp_path / "workspace"
    backend = FakeBackend()
    service = MountFileMcpServer(image, workspace, backend=backend)
    return service, backend, image, workspace


def test_default_partition_results_include_byte_offset(tmp_path):
    image = tmp_path / "mbr.img"
    mbr = bytearray(20 * 512)
    entry = 446
    mbr[entry + 4] = 0x0B
    struct.pack_into("<II", mbr, entry + 8, 1, 10)
    mbr[510:512] = b"\x55\xaa"
    image.write_bytes(mbr)

    partitions = DefaultBackend(tmp_path / "workspace").list_partitions(image)

    assert len(partitions) == 1
    assert partitions[0]["start_lba"] == 1
    assert partitions[0]["offset"] == 512
    assert partitions[0]["size_bytes"] == 10 * 512
    assert partitions[0]["type_name"] == "FAT32"


def test_registers_exactly_requested_tools_without_transport(tmp_path):
    service, _, _, _ = make_service(tmp_path)
    app = FakeMcpApp()

    assert service.register_tools(app) is app
    assert tuple(app.registered) == TOOL_NAMES
    assert all(description for _, description in app.registered.values())


def test_injected_backend_is_called_and_outputs_are_workspace_confined(tmp_path):
    service, backend, image, workspace = make_service(tmp_path)

    assert service.list_partitions(str(image)) == [{"offset": 512, "kind": "fake"}]
    assert service.list_directory(str(image), 0, "/")
    assert service.search_files(str(image), 0, "\\.txt$")
    extracted = service.extract_file(str(image), 0, "/note.txt")
    recovered = service.recover_deleted_file(str(image), 0, 7)
    decoded = service.try_xor_decrypt(str(image), "MTK")
    assert service.detect_signatures(str(image)) == [{"offset": 0, "kind": "fake"}]
    assert service.auto_decrypt(str(image)) is None

    assert Path(extracted["dest"]).parent == workspace
    assert Path(recovered["path"]).parent == workspace
    assert Path(decoded["output"]).parent == workspace
    assert len(backend.calls) == 8


def test_rejects_paths_outside_configured_roots(tmp_path):
    service, _, image, workspace = make_service(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do not expose")

    with pytest.raises(PathValidationError, match="outside"):
        service.list_partitions(str(outside))
    with pytest.raises(PathValidationError, match="outside"):
        service.extract_file(str(image), 0, "/name", output="../outside.bin")
    with pytest.raises(PathValidationError, match="outside"):
        service.hash_file(str(outside))
    with pytest.raises(ValueError, match="inside"):
        service.list_directory(str(image), 0, "/../etc")

    link = workspace / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable in test environment")
    with pytest.raises(PathValidationError, match="outside"):
        service.hash_file("escape")


def test_missing_backend_operation_is_a_clear_error(tmp_path):
    service, _, image, _ = make_service(tmp_path)
    service.backend = object()

    with pytest.raises(OperationUnavailable, match="list_partitions"):
        service.list_partitions(str(image))


def test_hash_file_only_accepts_workspace_files(tmp_path):
    service, _, image, workspace = make_service(tmp_path)
    target = workspace / "result.bin"
    target.write_bytes(b"abc")

    result = service.hash_file("result.bin")
    assert result == {
        "path": str(target),
        "sha256": "a" * 64,
        "size": 3,
    }
    assert image.exists()


def test_mcp_dependency_is_optional_and_missing_dependency_is_explicit(tmp_path):
    service, _, image, workspace = make_service(tmp_path)
    if MCP_AVAILABLE:
        pytest.skip("the optional MCP package is installed in this environment")
    with pytest.raises(OperationUnavailable):
        service._call("not_a_tool", image)
    with pytest.raises(Exception, match="optional 'mcp'"):
        create_server(image, workspace, backend=FakeBackend())
