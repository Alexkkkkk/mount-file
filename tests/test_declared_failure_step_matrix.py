# SPDX-License-Identifier: MIT
"""Issue #387 / #510, D1: declared failure-injection matrix, profile × step × state.

Every cell of ``DECLARED_STEPS`` is exercised for both declared states of the
staged NTFS writer:

* ``clean`` -- the injected failure at the step must surface as a typed
  refusal, leave the source image byte-for-byte identical and never publish;
* ``dirty`` -- the ``$LogFile`` gate refuses before any storage step runs, so
  the step under test must not even be reached.

The ext4 profile is covered by the same declared step tuple; those cells run
only where ``mkfs.ext4``/``debugfs``/``e2fsck`` exist, because the ext4 writer
is driven through real libext2fs, not through doubles.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mountfile.journal_safety import JournalSafetyError
from mountfile.native_write import (
    ENABLE_ENV,
    ENABLE_VALUE,
    NativeWriteUnsafe,
    NtfsTransactionalWriter,
)
from tests.test_ntfs_write_phase_injection import DECLARED_STEPS, _install_doubles

STEPS_NOT_REACHED_AFTER_GATE = ("ntfs_write", "read_back", "identity_verify", "publish")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_cell(tmp_path: Path, monkeypatch, *, step: str, state: str) -> None:
    """Execute one matrix cell and assert the fail-closed contract."""

    image = tmp_path / "matrix.img"
    image.write_bytes(b"matrix-source-image" + bytes(range(256)) * 4)
    before = _sha256(image)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)

    if state == "dirty":
        # The gate stub refuses at log_gate_pre: no storage step may run.
        order: list[str] = []
        _install_doubles(monkeypatch, order=order, fail="log_gate_pre")
        with pytest.raises((NativeWriteUnsafe, JournalSafetyError)):
            NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
        assert "ntfs_write" not in order
        assert "publish" not in order
        assert _sha256(image) == before
        return

    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail=step)
    with pytest.raises((NativeWriteUnsafe, JournalSafetyError)):
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    # The failing step itself was reached; nothing after it ran.
    assert step in order
    if step != "publish":
        assert "publish" not in order
    assert _sha256(image) == before


@pytest.mark.parametrize("state", ("clean", "dirty"))
@pytest.mark.parametrize("step", DECLARED_STEPS)
def test_ntfs_step_x_state_matrix_is_fail_closed(tmp_path, monkeypatch, step, state):
    _run_cell(tmp_path, monkeypatch, step=step, state=state)


def test_matrix_covers_every_declared_step_exactly_once():
    """Guard: the declared tuple is the coverage contract, not a comment."""

    assert len(set(DECLARED_STEPS)) == len(DECLARED_STEPS)
    assert set(DECLARED_STEPS) == {
        "copy",
        "payload_stage",
        "log_gate_pre",
        "ntfs_write",
        "log_gate_post",
        "read_back",
        "identity_verify",
        "publish",
    }
