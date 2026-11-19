# SPDX-License-Identifier: MIT
"""Issue #510, D3: systematic hash preservation across declared paths.

For the staged NTFS writer, every path is classified and each class carries
an explicit sha256 contract:

* ``failure:<step>`` for every declared step -- the source image must stay
  byte-for-byte identical;
* ``success`` -- exactly the atomic publish may change the image, and only
  the declared leftovers may remain in the staging directory;
* ``concurrent-change`` -- a source modified between copy and publish is
  refused: the staged bytes are never published and the concurrent
  modification is what remains on disk.

The ext4 profile already carries per-point process-kill hash evidence in
``tests/test_ext4_writer_fault_coverage.py``; this module adds the
step-level systematic pass for NTFS so no declared cell is left implicit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mountfile.journal_safety import JournalSafetyError
from mountfile.native_write import (
    ENABLE_ENV,
    ENABLE_VALUE,
    NativeWriteUnsafe,
    NtfsTransactionalWriter,
)
from tests.test_ntfs_write_phase_injection import (
    DECLARED_STEPS,
    _install_doubles,
    _leftovers,
    _standin_image,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matrix_row(cell: str, preserved: bool) -> dict[str, object]:
    return {"cell": cell, "hash_preserved": preserved, "typed": True}


@pytest.mark.parametrize("step", DECLARED_STEPS)
def test_source_hash_preserved_on_every_declared_failure(
    tmp_path, monkeypatch, step
) -> None:
    image = _standin_image(tmp_path)
    before = _sha256(image)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail=step)
    with pytest.raises((NativeWriteUnsafe, JournalSafetyError)):
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    row = _matrix_row(f"failure:{step}", _sha256(image) == before)
    assert row["hash_preserved"] is True


def test_success_changes_image_exactly_once_and_leaves_no_staging_leftovers(
    tmp_path, monkeypatch
) -> None:
    image = _standin_image(tmp_path)
    before = _sha256(image)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail="")
    NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    row = _matrix_row("success", _sha256(image) != before)
    assert row["hash_preserved"] is False  # the one canonical change
    expected = {image.name, "payload.bin"}
    assert _leftovers(tmp_path, expected) == []


def test_concurrent_source_change_is_refused_with_original_bytes_winning(
    tmp_path, monkeypatch
) -> None:
    image = _standin_image(tmp_path)
    before = _sha256(image)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail="")

    import mountfile.native_write as native_write

    # A concurrent writer touches the source after the copy/payload staging
    # but *before* the identity gate -- exactly the window the runtime guards
    # with _require_source_unchanged().  The previous revision tampered the
    # source inside the publish double, i.e. *after* the gate had already
    # passed, so the refusal could never fire and the cell never exercised
    # the guard it was written for (CI incident #571).
    tampered = b"TAMPERED"
    real_payload = native_write._payload_file

    def payload_with_interference(data: bytes, directory: str) -> str:
        result = real_payload(data, directory)
        with open(image, "r+b") as handle:
            handle.seek(0)
            handle.write(tampered)
        return result

    monkeypatch.setattr(native_write, "_payload_file", payload_with_interference)

    with pytest.raises((NativeWriteUnsafe, JournalSafetyError)):
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")

    # The refusal must happen at the identity gate, before any publish: the
    # staged bytes (computed from the pre-tamper image) are never published,
    # and what remains on disk is exactly the concurrent writer's state.
    assert "identity_verify" in order
    assert "publish" not in order
    with open(image, "rb") as handle:
        assert handle.read(len(tampered)) == tampered
    assert _sha256(image) != before
    # Cell invariant: the writer published nothing over the changed source,
    # so no staged bytes ever landed on disk.
    row = _matrix_row("concurrent-change", "publish" not in order)
    assert row["hash_preserved"] is True


def test_matrix_is_serializable_and_exhaustive() -> None:
    cells = [f"failure:{step}" for step in DECLARED_STEPS]
    cells += ["success", "concurrent-change"]
    report = {
        "schema": "mountfile.hash-preservation-matrix/v1",
        "profile": "ntfs-native-rw",
        "cells": [_matrix_row(cell, True) for cell in cells[:-2]]
        + [_matrix_row("success", False), _matrix_row("concurrent-change", True)],
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    assert json.loads(encoded)["schema"].startswith(
        "mountfile.hash-preservation-matrix/"
    )
    assert len(report["cells"]) == len(DECLARED_STEPS) + 2
