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
        str(
            auto_mountfile.Path(auto_mountfile.__file__)
            .resolve()
            .with_name("main.py")
        ),
        args[0],
        str(image),
        *args[1:],
    ]
    assert auto_mountfile.Path(captured["command"][1]).is_absolute()
    assert captured["check"] is False


def test_auto_launcher_requires_a_subcommand(tmp_path):
    with pytest.raises(ValueError, match="subcommand"):
        auto_mountfile.run_mountfile_cli(tmp_path / "image.img", [])


def test_auto_run_propagates_cli_failure_without_starting_later_steps(monkeypatch):
    analysis = SimpleNamespace(
        path="dump.bin",
        dtype="generic",
        matched="",
        profile={"description": "test", "need_decode": False, "need_split": False,
                 "cli_after_open": ["volumes", "ls"]},
        recommended_fs="",
        recommended_offset=0,
        write_allowed=False,
    )
    calls = []

    def fail_cli(_image, args):
        calls.append(args)
        return 73

    monkeypatch.setattr(auto_mountfile, "run_mountfile_cli", fail_cli)
    assert auto_mountfile.auto_run(
        analysis,
        gui=False,
        decode=False,
        split=False,
        udev=None,
        list_only=False,
    ) == 73
    assert calls == [["volumes"]]


def test_auto_run_propagates_gui_exit_status(monkeypatch):
    analysis = SimpleNamespace(
        path="dump.bin",
        dtype="generic",
        matched="",
        profile={"description": "test", "need_decode": False, "need_split": False,
                 "cli_after_open": []},
        recommended_fs="",
        recommended_offset=0,
        write_allowed=False,
    )
    monkeypatch.setattr(
        auto_mountfile.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=91),
    )
    assert auto_mountfile.auto_run(
        analysis,
        gui=True,
        decode=False,
        split=False,
        udev=None,
        list_only=False,
    ) == 91


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_unauthenticated_dfir_api_refuses_non_loopback_hosts(host):
    args = SimpleNamespace(host=host, db=":memory:", port=8000)

    with pytest.raises(SystemExit, match="non-loopback"):
        mountfile_dfir.cmd_serve(args)
