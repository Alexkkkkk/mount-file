# SPDX-License-Identifier: MIT
"""Tests for the package MCP command-line entry point."""

import pytest

from mountfile.ai import cli, mcp_server


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
