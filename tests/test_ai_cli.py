# SPDX-License-Identifier: MIT
"""Tests for the package MCP command-line entry point."""

import importlib.util
from pathlib import Path

import pytest

from mountfile.ai import cli, mcp_server

ROOT_CLI_PATH = Path(__file__).resolve().parents[1] / "mountfile_ai.py"
ROOT_CLI_SPEC = importlib.util.spec_from_file_location(
    "mountfile_ai_root", ROOT_CLI_PATH
)
root_cli = importlib.util.module_from_spec(ROOT_CLI_SPEC)
ROOT_CLI_SPEC.loader.exec_module(root_cli)


class FakeServer:
    def __init__(self):
        self.ran = False

    def run(self):
        self.ran = True


def test_mcp_command_builds_and_runs_stdio_server(monkeypatch, tmp_path):
    server = FakeServer()
    calls = []

    def fake_create_server(**kwargs):
        calls.append(kwargs)
        return server

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    image = tmp_path / "case.img"
    workspace = tmp_path / "outputs"

    assert (
        cli.main(
            [
                "mcp",
                "--image",
                str(image),
                "--workspace",
                str(workspace),
                "--name",
                "test",
            ]
        )
        == 0
    )
    assert calls == [
        {"image_root": str(image), "workspace": str(workspace), "name": "test"}
    ]
    assert server.ran is True


def test_mcp_command_reports_missing_optional_dependency(monkeypatch, capsys):
    def unavailable(**kwargs):
        raise mcp_server.McpUnavailableError("install the optional mcp package")

    monkeypatch.setattr(mcp_server, "create_server", unavailable)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["mcp", "--image", "case.img"])

    assert exc_info.value.code == 2
    assert "install the optional mcp package" in capsys.readouterr().err


def test_root_inspect_returns_zero_when_partitions_are_read(
    monkeypatch, tmp_path, capsys
):
    image = tmp_path / "case.img"
    image.write_bytes(b"test image")
    monkeypatch.setattr(root_cli, "list_partitions", lambda image: [])

    exit_code = root_cli.main(["inspect", "--image", str(image)])

    assert exit_code == 0
    assert "partitions:\n  (empty)" in capsys.readouterr().out


def test_root_inspect_returns_nonzero_when_partitions_are_unreadable(
    monkeypatch, tmp_path, capsys
):
    image = tmp_path / "case.img"
    image.write_bytes(b"test image")

    def unreadable(image):
        raise RuntimeError("no volume table")

    monkeypatch.setattr(root_cli, "list_partitions", unreadable)

    exit_code = root_cli.main(["inspect", "--image", str(image)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "(unreadable: no volume table)" in output
    assert "try: python mountfile_ai.py agent --image" in output
