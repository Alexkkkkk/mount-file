# SPDX-License-Identifier: MIT
"""Negative coverage for the #510 oracle source-invariant fix.

The oracle previously asserted ``source_untouched`` with a tautological
``... or True``, which made the "source is never mutated" claim unprovable
and silently undermined every D3/C6 item leaning on that invariant.  These
tests pin the real behaviour: a replay that scribbles on the source fixture
must be reported as ``source_untouched is False``, and an intact source
must stay ``True``.  They are fully offline: every external-tool touchpoint
of ``run_oracle`` is stubbed, so only the invariant itself is exercised.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import tests.ntfs_oracle as oracle
from tests.ntfs_oracle import DirtyFixture, run_oracle


def _fixture(tmp_path: Path, payload: bytes) -> DirtyFixture:
    image = tmp_path / "fixture.img"
    image.write_bytes(payload)
    return DirtyFixture(
        scenario="unit",
        image=image,
        record_number=0,
        record_size=512,
        old_data=payload,
        new_data=payload,
    )


@pytest.fixture()
def stub_external_tools(monkeypatch):
    """Remove every external-tool dependency from ``run_oracle``."""
    monkeypatch.setattr("mountfile.ntfs_replay.native_ntfs_recover", lambda *_: None)
    monkeypatch.setattr(oracle, "_mft_record_stream", lambda *_: b"stub")
    monkeypatch.setattr(oracle, "_ntfscat", lambda *_: b"stub")
    monkeypatch.setattr(oracle, "_ntfsfix_check", lambda *_: (True, "stub"))


def test_source_untouched_is_false_when_replay_mutates_source(
    tmp_path, monkeypatch, stub_external_tools
):
    fixture = _fixture(tmp_path, b"ORIGINAL-FIXTURE-PAYLOAD")

    def mutating_recover(_image):
        with open(fixture.image, "r+b") as handle:
            handle.seek(0)
            handle.write(b"MUTATED")

    monkeypatch.setattr("mountfile.ntfs_replay.native_ntfs_recover", mutating_recover)

    result = run_oracle(fixture, tmp_path)

    assert result["source_untouched"] is False


def test_source_untouched_is_true_when_source_stays_intact(
    tmp_path, stub_external_tools
):
    fixture = _fixture(tmp_path, b"INTACT-FIXTURE-PAYLOAD")

    result = run_oracle(fixture, tmp_path)

    assert result["source_untouched"] is True


def test_the_tautology_stays_removed():
    """Regression guard: the ``or True`` must never come back."""
    source = inspect.getsource(oracle)
    assert "or True" not in source
