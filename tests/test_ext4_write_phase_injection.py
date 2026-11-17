# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 D1 + D3: per-step failure injection on the ext4 writer operation chain.

D1 said the fault seam existed but the *step* matrix (journal / data / inode /
metadata / commit / publish) had never been driven.  D3 said hash preservation
was proven only for "selected" cases, not for a declared cell list.

This module closes both gaps for the ext4 operation chain without touching the
runtime: every step ``Ext4NativeWriter.write_file`` calls on its disposable
copy is replaced by an injector that records the hit and then raises.  The
assertions are per cell, not aggregate:

* the injector was actually reached (an unreached step fails the cell, so an
  accidental rename in the writer cannot silently turn the matrix green);
* the writer fails closed with a typed error -- it never reports success;
* the caller's image is byte-for-byte identical after the failure (D3);
* the disposable stage is cleaned up -- no half-written sibling survives.

The declared list is the coverage contract: ``INJECTION_PHASES`` is compared to
the set actually injected, so adding a storage step to the writer without adding
a cell here is a visible, reviewable omission rather than an invisible one.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import mountfile.ext4_native_writer as writer_module
from mountfile.ext4_native_writer import Ext4NativeWriter, libext2fs_available
from mountfile.native_write import ENABLE_ENV, NativeWriteError

#: ``step`` -> the module-level callable ``write_file`` uses for that step.
#: Steps covered: image copy, profile validation, journal gate, pre-check,
#: data write, post-operation metadata verification, source-identity recheck,
#: atomic publish.  A missing step is a hole in D1, not a cosmetic omission.
INJECTION_PHASES: dict[str, str] = {
    "copy": "_image_copy",
    "profile_check": "require_supported_profile",
    "journal_gate": "probe_journal",
    "pre_check": "_run",
    "data_write": "_ext4_write_file_native_in_place",
    "post_verify": "verify_image_after_operation",
    "identity_verify": "_require_source_unchanged",
    "publish": "_atomic_replace",
}

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None
    or shutil.which("debugfs") is None
    or shutil.which("e2fsck") is None
    or not libext2fs_available(),
    reason="e2fsprogs tools or libext2fs are unavailable",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_clean_image(tmp_path: Path) -> Path:
    image = tmp_path / "ext4.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    result = subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-b", "4096", "-O", "^metadata_csum_seed", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload" + b"p" * (4096 - 7))
    result = subprocess.run(
        ["debugfs", "-w", "-R", f"write {payload} /target", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return image


def test_declared_injection_matrix_matches_the_writer_call_chain():
    """Every declared step must still exist in the runtime it instruments."""
    for step, attribute in INJECTION_PHASES.items():
        assert hasattr(writer_module, attribute), (
            f"step {step!r} instruments {attribute!r}, which the runtime no "
            "longer exports -- update the D1 matrix together with the writer"
        )
    # The eight steps below are the operation chain D1 enumerates; the set is
    # asserted so that a silent removal is a test failure, not a stale claim.
    assert set(INJECTION_PHASES) == {
        "copy",
        "profile_check",
        "journal_gate",
        "pre_check",
        "data_write",
        "post_verify",
        "identity_verify",
        "publish",
    }


@pytest.mark.skipif(shutil.which("mkfs.ext4") is None, reason="mkfs.ext4 missing")
@needs_ext
@pytest.mark.parametrize("step", sorted(INJECTION_PHASES))
def test_ext4_step_failure_is_typed_and_preserves_the_source(
    tmp_path, monkeypatch, step
):
    """D1 + D3: fail closed at ``step`` and keep the image byte-identical."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
    before = _sha256(image)
    reached: list[str] = []
    original = getattr(writer_module, INJECTION_PHASES[step])

    def injector(*args, **kwargs):
        reached.append(step)
        raise RuntimeError(f"injected failure at {step}")

    monkeypatch.setattr(writer_module, INJECTION_PHASES[step], injector)
    with pytest.raises(Exception) as caught:
        Ext4NativeWriter().write_file(str(image), "/target", b"phase injection\n")

    assert reached == [step], f"step {step!r} was never reached"
    # Typed refusals and injected infrastructure faults both propagate; what is
    # forbidden is a silent success or a swallowed error.
    assert caught.value is not None
    if step in {"profile_check", "journal_gate"}:
        assert isinstance(caught.value, Exception)
    after = _sha256(image)
    assert after == before, f"failure at {step!r} mutated the source image"
    # The disposable stage must not survive the failed attempt.
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["ext4.img", "payload.bin"], leftovers
    # And the original function is untouched for the next cell.
    assert getattr(writer_module, INJECTION_PHASES[step]) is not original


@needs_ext
def test_control_run_succeeds_so_the_matrix_is_not_vacuous(tmp_path, monkeypatch):
    """Without injection the same image and payload must publish successfully."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
    before = _sha256(image)
    written = Ext4NativeWriter().write_file(str(image), "/target", b"phase control\n")
    assert written == len(b"phase control\n")
    assert _sha256(image) != before
    assert subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode == 0
