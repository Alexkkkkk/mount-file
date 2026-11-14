"""Tests for the quarantined optional dfir_ntfs adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from mountfile.experimental.dfir_ntfs_adapter import (
    DfirNtfsAdapter,
    ExperimentalAdapterUnavailable,
)


def test_missing_external_parser_is_typed_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    image = tmp_path / "$LogFile"
    image.write_bytes(b"not-a-log")
    monkeypatch.setitem(__import__("sys").modules, "dfir_ntfs", None)
    with pytest.raises(ExperimentalAdapterUnavailable, match="unavailable"):
        DfirNtfsAdapter(image)


def test_adapter_does_not_expose_mutation_api() -> None:
    assert not hasattr(DfirNtfsAdapter, "write")
    assert not hasattr(DfirNtfsAdapter, "replay")
