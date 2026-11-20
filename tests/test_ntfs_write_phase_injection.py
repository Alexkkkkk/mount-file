# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 D1 + D3: per-step failure injection on the NTFS writer operation chain.

The ext4 chain has a declared injection matrix
(``tests/test_ext4_write_phase_injection.py``); the NTFS chain did not, which
is exactly the D1 gap ("NTFS steps uninstrumented") and half of the D3 gap
(hash preservation proven only for selected NTFS cases).

This module closes both gaps for ``NtfsNativeWriter.write_file`` on a clean
volume without touching the runtime: every storage-touching step is replaced
by an injector that records the hit and then raises.  Per cell:

* the injector was actually reached (an unreached step fails the cell);
* the writer fails closed -- it never reports success;
* the caller's image is byte-for-byte identical after the failure (D3);
* no disposable stage survives the failed attempt.

The declared list is the coverage contract: adding a storage step to the
writer without adding a cell here is a visible, reviewable omission.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import mountfile.ntfs_native_writer as writer_module
from mountfile.native_write import ENABLE_ENV
from mountfile.ntfs_native_writer import NtfsNativeWriter

#: ``step`` -> (module attribute, 0-based probe-call index or None).
#: ``probe_ntfs_log`` is called once before the write (gate) and once after
#: (post-verify); the index picks the call the cell injects at, the other
#: call passes through to the real probe.
INJECTION_PHASES: dict[str, tuple[str, int | None]] = {
    "copy": ("_image_copy", None),
    "gate_pre": ("probe_ntfs_log", 0),
    "data_write": ("ntfs_write_file_native", None),
    "post_verify": ("probe_ntfs_log", 1),
    "identity_verify": ("_require_source_unchanged", None),
    "publish": ("_atomic_replace", None),
}

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None,
    reason="mkntfs (ntfs-3g) is unavailable",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_clean_image(tmp_path: Path) -> Path:
    image = tmp_path / "clean.img"
    with open(image, "wb") as handle:
        handle.truncate(64 << 20)
    result = subprocess.run(
        ["mkntfs", "-q", "-F", "-s", "512", "-c", "4096", str(image)],
        check=True,
        capture_output=True,
    )
    assert result.returncode == 0
    return image


def test_declared_injection_matrix_matches_the_writer_call_chain():
    """Every declared step must still exist in the runtime it instruments."""
    for step, (attribute, _index) in INJECTION_PHASES.items():
        assert hasattr(writer_module, attribute), (
            f"step {step!r} instruments {attribute!r}, which the runtime no "
            "longer exports -- update the D1 matrix together with the writer"
        )
    # The six steps below are the NTFS operation chain D1 enumerates; the set
    # is asserted so that a silent removal is a test failure, not a stale claim.
    assert set(INJECTION_PHASES) == {
        "copy",
        "gate_pre",
        "data_write",
        "post_verify",
        "identity_verify",
        "publish",
    }


@needs_ntfs
@pytest.mark.parametrize("step", sorted(INJECTION_PHASES))
def test_ntfs_step_failure_is_typed_and_preserves_the_source(
    tmp_path, monkeypatch, step
):
    """D1 + D3: fail closed at ``step`` and keep the image byte-identical."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
    before = _sha256(image)
    reached: list[str] = []
    attribute, probe_index = INJECTION_PHASES[step]
    original = getattr(writer_module, attribute)

    if probe_index is None:

        def injector(*args, **kwargs):
            reached.append(step)
            raise RuntimeError(f"injected failure at {step}")

    else:
        calls = 0

        def injector(*args, **kwargs):
            nonlocal calls
            if calls == probe_index:
                reached.append(step)
                raise RuntimeError(f"injected failure at {step}")
            calls += 1
            return original(*args, **kwargs)

    monkeypatch.setattr(writer_module, attribute, injector)
    with pytest.raises(Exception) as caught:
        NtfsNativeWriter(recover="native").write_file(
            str(image), "/target", b"phase injection\n"
        )

    assert reached == [step], f"step {step!r} was never reached"
    # Injected infrastructure faults propagate; what is forbidden is a silent
    # success or a swallowed error.
    assert caught.value is not None
    after = _sha256(image)
    assert after == before, f"failure at {step!r} mutated the source image"
    # The disposable stage must not survive the failed attempt.
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["clean.img"], leftovers
    # And the original function is restored for the next cell.
    monkeypatch.undo()
    assert getattr(writer_module, attribute) is original


@needs_ntfs
def test_control_run_succeeds_so_the_matrix_is_not_vacuous(tmp_path, monkeypatch):
    """Without injection the same image and payload must publish successfully."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
    before = _sha256(image)
    written = NtfsNativeWriter(recover="native").write_file(
        str(image), "/target", b"phase control\n"
    )
    assert written == len(b"phase control\n")
    assert _sha256(image) != before
    from mountfile.ntfs_native_writer import probe_ntfs_log

    assert probe_ntfs_log(str(image)).clean is True
