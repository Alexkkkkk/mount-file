# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Read-only tests for crypto-container identification and safe adapters."""

from __future__ import annotations

import io
import struct

import pytest

from mountfile import crypto


def _header(**markers: bytes) -> bytes:
    data = bytearray(64 * 1024)
    if "luks" in markers:
        data[: len(markers["luks"])] = markers["luks"]
    if "bitlocker" in markers:
        data[3 : 3 + len(markers["bitlocker"])] = markers["bitlocker"]
    if "apfs" in markers:
        data[32 : 32 + len(markers["apfs"])] = markers["apfs"]
    if "corestorage" in markers:
        data[88 : 88 + len(markers["corestorage"])] = markers["corestorage"]
    return bytes(data)


def test_detects_luks_versions_without_writing(tmp_path):
    image = tmp_path / "luks.img"
    data = bytearray(_header(luks=crypto.LUKS_MAGIC))
    struct.pack_into(">H", data, 6, 2)
    image.write_bytes(data)

    assert crypto.detect_container(image) == "luks"
    info = crypto.inspect_container(image)
    assert info.version == 2
    assert info.encrypted is True
    assert image.read_bytes() == bytes(data)


def test_detects_bitlocker_and_apfs_from_volume_headers():
    bitlocker = _header(bitlocker=b"-FVE-FS-")
    apfs = _header(apfs=crypto.APFS_CONTAINER_MAGIC)

    assert crypto.detect_container(bitlocker) == "bitlocker"
    apfs_info = crypto.inspect_container(io.BytesIO(apfs))
    assert apfs_info.kind == "apfs"
    assert apfs_info.signature == "NXSB"
    assert apfs_info.encrypted is None


def test_detects_legacy_filevault_corestorage_marker():
    data = bytearray(_header(corestorage=crypto.CORESTORAGE_MAGIC))
    struct.pack_into("<I", data, 172, 2)  # AES-XTS method in the CS header.

    info = crypto.inspect_container(data)
    assert info.kind == "filevault"
    assert info.signature == "CS"
    assert info.encrypted is True
    assert crypto.detect_container(data) == "filevault"


def test_unknown_data_is_not_guessed():
    assert crypto.detect_container(b"not an encrypted volume") == "unknown"


def test_adapter_checks_are_explicit_and_side_effect_free(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/usr/bin/" + name if name != "dislocker" else None

    statuses = crypto.check_adapters(which=fake_which)
    assert set(statuses) == {"cryptsetup", "dislocker", "apfs-fuse"}
    assert statuses["cryptsetup"].available is True
    assert statuses["dislocker"].available is False
    assert crypto.adapter_available("luks", which=fake_which) is True
    assert seen.count("cryptsetup") >= 2
    monkeypatch.setattr(crypto.shutil, "which", lambda _: None)
    assert crypto.available_adapters() == {
        "cryptsetup": False,
        "dislocker": False,
        "apfs-fuse": False,
    }


def test_luks_password_is_stdin_only(monkeypatch, tmp_path):
    image = tmp_path / "volume.img"
    image.write_bytes(b"x")
    calls = []

    def fake_which(name):
        assert name == "cryptsetup"
        return "/fake/cryptsetup"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(crypto.shutil, "which", fake_which)
    result = crypto.unlock_luks(
        image,
        "mountfile_test",
        password="secret-not-in-argv",
        runner=fake_run,
    )

    command, kwargs = calls[0]
    assert result == "/dev/mapper/mountfile_test"
    assert "secret-not-in-argv" not in command
    assert kwargs["input"] == b"secret-not-in-argv\n"
    assert kwargs["stdout"] is crypto.subprocess.PIPE
    assert kwargs["stderr"] is crypto.subprocess.PIPE


def test_bitlocker_password_is_not_in_command_arguments(monkeypatch, tmp_path):
    image = tmp_path / "volume.img"
    image.write_bytes(b"x")
    calls = []

    monkeypatch.setattr(crypto.shutil, "which", lambda name: "/fake/" + name)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    result = crypto.unlock_bitlocker(
        image,
        tmp_path / "fuse-out",
        password=b"recovery-secret",
        runner=fake_run,
    )
    command, kwargs = calls[0]
    assert result.endswith("fuse-out")
    assert b"recovery-secret" not in command
    assert "recovery-secret" not in " ".join(command)
    assert kwargs["input"] == b"recovery-secret\n"


def test_apfs_open_fails_explicitly_instead_of_claiming_decryption():
    container = crypto.CryptoContainer(_header(apfs=crypto.APFS_CONTAINER_MAGIC))
    with pytest.raises(crypto.CryptoOperationError, match="does not decrypt"):
        container.open_apfs("/tmp/not-mounted")
