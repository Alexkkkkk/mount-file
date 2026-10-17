# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Focused tests for the optional, read-only Registry facade."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mountfile import registry


@dataclass
class FakeValue:
    name: str
    value_type: str
    data: object


@dataclass
class FakeKey:
    name: str
    path: str
    values: tuple
    subkeys: tuple = ()


class FakeHandle:
    def __init__(self):
        child = FakeKey(
            name="Run",
            path=r"\\ROOT\\Software\\Run",
            values=(FakeValue("Example", "REG_SZ", "read-only"),),
        )
        self.root = FakeKey(
            name="ROOT",
            path=r"\\ROOT",
            values=(FakeValue("Version", "REG_SZ", "1"),),
            subkeys=(child,),
        )
        self.closed = False

    def close(self):
        self.closed = True


class FakeAdapter:
    name = "mock"

    def __init__(self):
        self.opened = []
        self.handle = None

    def open(self, source):
        self.opened.append(source)
        self.handle = FakeHandle()
        return self.handle

    def iter_keys(self, handle):
        return registry._walk_keys(handle.root)


def test_identifies_supported_names_and_rejects_guesses(tmp_path):
    assert (
        registry.identify_registry_file(tmp_path / "SAM").kind
        is registry.RegistryFileType.SAM
    )
    assert registry.identify_registry_file("ntuser.dat").is_registry_hive
    usn = registry.identify_registry_file("$UsnJrnl")
    assert usn.kind is registry.RegistryFileType.USN_JOURNAL
    assert not usn.is_registry_hive
    assert (
        registry.identify_registry_file(b"not-a-hive").kind
        is registry.RegistryFileType.UNKNOWN
    )
    assert (
        registry.identify_registry_file(b"regf-rest").kind
        is registry.RegistryFileType.GENERIC
    )


def test_injected_adapter_iterates_structured_keys_and_values_without_optional_packages(
    tmp_path,
):
    source = tmp_path / "SOFTWARE"
    adapter = FakeAdapter()

    with registry.open_registry(source, adapter=adapter) as parser:
        keys = list(parser.iter_keys())
        values = list(parser.iter_values(path=r"\\ROOT\\Software"))

    assert adapter.opened == [source]
    assert [key.path for key in keys] == [r"\\ROOT", r"\\ROOT\\Software\\Run"]
    assert [
        (value.key_path, value.name, value.value_type, value.data) for value in values
    ] == [(r"\\ROOT\\Software\\Run", "Example", "REG_SZ", "read-only")]
    assert adapter.handle.closed is True


def test_usn_journal_and_unknown_input_fail_clearly(tmp_path):
    with pytest.raises(registry.UnsupportedRegistryError, match="NTFS USN journal"):
        registry.open_registry(tmp_path / "$UsnJrnl", adapter=FakeAdapter())
    with pytest.raises(registry.UnsupportedRegistryError, match="expected SAM"):
        registry.open_registry(tmp_path / "notes.txt", adapter=FakeAdapter())


def test_missing_optional_backend_has_install_guidance(monkeypatch, tmp_path):
    monkeypatch.setattr(
        registry,
        "available_backends",
        lambda: {"python-registry": False, "regipy": False},
    )
    with pytest.raises(
        registry.OptionalDependencyError, match="python-registry.*regipy"
    ):
        registry.open_registry(tmp_path / "SYSTEM")


def test_adapter_specific_optional_dependency_error_is_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_module_available", lambda _: False)
    with pytest.raises(registry.OptionalDependencyError, match="python-registry"):
        registry.open_registry(tmp_path / "SAM", backend="python-registry")
