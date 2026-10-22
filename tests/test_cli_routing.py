"""Regression tests for command-line argument construction in launchers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import auto_mountfile
import mountfile_dfir


@pytest.mark.parametrize(
    "args",
    [
        ["ls", "/"],
        ["split", "layout.udev", "-o", "parts"],
        ["decode"],
    ],
)
def test_auto_launcher_places_image_after_subcommand(monkeypatch, tmp_path, args):
    captured = {}

    def fake_run(command, check=False):
        captured["command"] = command
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(auto_mountfile.subprocess, "run", fake_run)

    image = tmp_path / "image.img"
    assert auto_mountfile.run_mountfile_cli(image, args) == 0
    assert captured["command"] == [
        auto_mountfile.sys.executable,
        "main.py",
        args[0],
        str(image),
        *args[1:],
    ]
    assert captured["check"] is False


def test_auto_launcher_requires_a_subcommand(tmp_path):
    with pytest.raises(ValueError, match="subcommand"):
        auto_mountfile.run_mountfile_cli(tmp_path / "image.img", [])


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_unauthenticated_dfir_api_refuses_non_loopback_hosts(host):
    args = SimpleNamespace(host=host, db=":memory:", port=8000)

    with pytest.raises(SystemExit, match="non-loopback"):
        mountfile_dfir.cmd_serve(args)
