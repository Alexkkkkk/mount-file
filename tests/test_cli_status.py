"""Tests for the issue-182 CLI capability/refusal status projection."""

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

import mountfile.cli as cli
from mountfile.safety import RefusalCode, RefusalError
from mountfile.vfs import TskError
from tests.test_ext2_native import _make_ext2


def _args(tmp_path):
    return SimpleNamespace(
        image=tmp_path / "disk.img",
        offset=0,
        volume=None,
        auto=False,
        udev=None,
        scan_limit=1024 * 1024,
    )


def test_status_reports_provider_backend_and_extent(monkeypatch, tmp_path, capsys):
    args = _args(tmp_path)
    provider = SimpleNamespace(
        backend_status={
            "backend": "native-ext2",
            "mode": "read-only",
            "profile_id": "ext2-native-ro",
            "read_capabilities": ["list", "stat"],
            "write_capabilities": [],
            "write_enabled": False,
        },
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "_select", lambda *_args: (0, "ext2", "whole image"))
    monkeypatch.setattr(cli, "volume_size_for_offset", lambda *_args: 4096)
    monkeypatch.setattr(cli, "_provider", lambda *_args: (provider, "native-ext2"))

    assert cli.cmd_status(args) == cli.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "native-ext2"
    assert payload["profile_id"] == "ext2-native-ro"
    assert payload["offset"] == 0
    assert payload["volume_size"] == 4096


def test_status_reports_native_writer_profile_without_mutating_image(
    monkeypatch, tmp_path, capsys
):
    args = _args(tmp_path)
    provider = SimpleNamespace(
        backend_status={
            "backend": "native-fat-rw",
            "mode": "read-only",
            "read_capabilities": ["list"],
            "write_capabilities": [],
            "write_enabled": False,
        },
        close=lambda: None,
    )

    class FakeWriter:
        def __init__(self, *_args, **_kwargs):
            pass

        def info(self):
            return SimpleNamespace()

        @staticmethod
        def backend_status(_info):
            return writer_status

        def close(self):
            pass

    writer_status = {
        "mode": "read-write",
        "write_capabilities": ["write_file", "mkdir", "delete", "rename"],
        "write_enabled": True,
        "write_rejection_reason": "",
        "write_rejection_code": None,
    }
    monkeypatch.setattr(cli, "_select", lambda *_args: (0, "fat32", "whole image"))
    monkeypatch.setattr(cli, "volume_size_for_offset", lambda *_args: 4096)
    monkeypatch.setattr(cli, "_provider", lambda *_args: (provider, "native-fat-rw"))
    monkeypatch.setattr(cli, "UniversalFSRW", FakeWriter)

    assert cli.cmd_status(args) == cli.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["write_enabled"] is False
    assert payload["write_capabilities"] == []
    assert payload["write_status"]["write_enabled"] is True
    assert payload["write_status"]["write_capabilities"] == [
        "write_file",
        "mkdir",
        "delete",
        "rename",
    ]
    assert payload["write_status"]["mode"] == "read-write"


@pytest.mark.skipif(not shutil.which("mke2fs"), reason="mke2fs is required")
def test_status_real_ext2_does_not_modify_image(tmp_path, capsys):
    image = tmp_path / "status.ext2"
    _make_ext2(image)
    before = hashlib.sha256(image.read_bytes()).digest()
    args = _args(tmp_path)
    args.image = image

    assert cli.cmd_status(args) == cli.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "native-ext2"
    assert payload["write_status"]["profile_id"] == "ext2-native-rw"
    assert payload["write_status"]["write_enabled"] is True
    assert hashlib.sha256(image.read_bytes()).digest() == before


def test_status_preserves_typed_native_refusal_without_fallback(
    monkeypatch, tmp_path, capsys
):
    args = _args(tmp_path)
    refusal = RefusalError(
        "dirty or requires journal recovery",
        RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
    )
    monkeypatch.setattr(cli, "_select", lambda *_args: (0, "ext3", "whole image"))
    monkeypatch.setattr(cli, "volume_size_for_offset", lambda *_args: 8192)
    monkeypatch.setattr(
        cli,
        "_provider",
        lambda *_args: (_ for _ in ()).throw(TskError(str(refusal), refusal.code)),
    )

    assert cli.cmd_status(args) == cli.ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "unavailable"
    assert payload["refusal"]["code"] == "dirty_or_recovery_required"
    assert payload["write_enabled"] is False


def test_status_classifies_writer_dirty_refusal(monkeypatch, tmp_path, capsys):
    args = _args(tmp_path)
    provider = SimpleNamespace(
        backend_status={
            "backend": "native-ext3-ro",
            "mode": "read-only",
            "read_capabilities": ["list"],
            "write_capabilities": [],
            "write_enabled": False,
        },
        close=lambda: None,
    )

    def reject_writer(*_args, **_kwargs):
        raise RuntimeError("ext3 volume is dirty or requires journal recovery")

    monkeypatch.setattr(cli, "_select", lambda *_args: (0, "ext3", "whole image"))
    monkeypatch.setattr(cli, "volume_size_for_offset", lambda *_args: 8192)
    monkeypatch.setattr(cli, "_provider", lambda *_args: (provider, "native-ext3-ro"))
    monkeypatch.setattr(cli, "UniversalFSRW", reject_writer)

    assert cli.cmd_status(args) == cli.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["write_status"]["write_rejection_code"] == (
        "dirty_or_recovery_required"
    )
    assert payload["write_status"]["write_rejection"]["category"] == ("dirty_recovery")
