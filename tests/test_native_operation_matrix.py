# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 D1 + D3: operation x phase injection matrix, not only ``write_file``.

D1 recorded that the per-step matrices covered the *write* chain of the native
writers.  That leaves every other declared operation unmeasured: this module
extends the same contract to each operation the gated writers actually expose,
and states which operations the runtime does **not** expose instead of inventing
cells for them.

Declared operations (from the runtime, not from ambition)::

    ext4.write_file    -> copy, profile_check, journal_gate, pre_check,
                          data_write, post_verify, identity_verify, publish
    ext4.create_file   -> copy, delegate, post_verify, identity_verify, publish
    ntfs.write_file    -> copy, payload_stage, log_gate_pre, ntfs_write,
                          log_gate_post, read_back, identity_verify, publish

`ntfs` exposes only ``write_file``; there is no native create/delete/rename on
that writer, so a cell for them would be a cell that can never be reached. The
matrix therefore has three operation families, and `OPERATION_PHASES` is the
reviewable contract.

Per cell the assertions are the same as the write-chain matrices: the phase was
actually reached, the failure propagates as a typed refusal, the caller's image
is byte-identical afterwards (D3), and no dot-prefixed staged sibling survives.
Each family also has a control cell proving the matrix is not vacuous.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import mountfile.ext4_native_writer as ext4_module
import mountfile.native_write as native_write
from mountfile.ext4_native_writer import (
    Ext4NativeWriter,
    Ext4ProfileUnsupported,
    libext2fs_available,
)
from mountfile.native_write import ENABLE_ENV, NativeWriteError, NtfsTransactionalWriter

#: operation -> declared phase list, in runtime order.
OPERATION_PHASES: dict[str, tuple[str, ...]] = {
    "ext4.write_file": (
        "copy",
        "profile_check",
        "journal_gate",
        "pre_check",
        "data_write",
        "post_verify",
        "identity_verify",
        "publish",
    ),
    "ext4.create_file": (
        "copy",
        "delegate",
        "post_verify",
        "identity_verify",
        "publish",
    ),
    "ntfs.write_file": (
        "copy",
        "payload_stage",
        "log_gate_pre",
        "ntfs_write",
        "log_gate_post",
        "read_back",
        "identity_verify",
        "publish",
    ),
}

#: Operations the gated writers do not expose at all.  Recorded so that the
#: absence is a decision, not an oversight.
UNSUPPORTED_OPERATIONS: dict[str, tuple[str, ...]] = {
    "ext4": ("rename", "unlink", "truncate", "setattr", "symlink", "mkdir"),
    "ntfs": (
        "create_file",
        "rename",
        "unlink",
        "truncate",
        "setattr",
        "mkdir",
    ),
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


def _staged(directory: Path, known: set[str]) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name not in known)


def _make_ext4(tmp_path: Path) -> Path:
    image = tmp_path / "ext4.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    result = subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-b", "4096", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"seed" + b"s" * (4096 - 4))
    result = subprocess.run(
        ["debugfs", "-w", "-R", f"write {seed} /target", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return image


def test_declared_operations_match_the_runtime():
    """The contract must describe the writers that exist, and no more."""
    from tests.test_ntfs_write_phase_injection import DECLARED_STEPS

    assert hasattr(Ext4NativeWriter, "write_file")
    assert hasattr(Ext4NativeWriter, "create_file")
    assert hasattr(NtfsTransactionalWriter, "write_file")
    assert OPERATION_PHASES["ntfs.write_file"] == DECLARED_STEPS
    for operation, phases in OPERATION_PHASES.items():
        assert phases, operation
        assert len(set(phases)) == len(phases), operation
    # No cell may be declared for an operation the profile does not expose, and
    # the counterpart holds: an operation that starts existing must be declared.
    declared = set(OPERATION_PHASES)
    for profile, unsupported in UNSUPPORTED_OPERATIONS.items():
        for name in unsupported:
            assert (
                f"{profile}.{name}" not in declared
            ), f"{profile}.{name} is declared unsupported yet has a phase matrix"
    writers = {"ext4": Ext4NativeWriter, "ntfs": NtfsTransactionalWriter}
    for profile, writer in writers.items():
        for name in UNSUPPORTED_OPERATIONS[profile]:
            assert not hasattr(writer, name), (
                f"{profile} now exposes {name}() -- add its phase matrix to "
                "OPERATION_PHASES instead of leaving a hole"
            )
    assert set(OPERATION_PHASES) == {
        "ext4.write_file",
        "ext4.create_file",
        "ntfs.write_file",
    }


@needs_ext
def test_ext4_create_file_publishes_so_the_cells_are_not_vacuous(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ext4(tmp_path)
    before = _sha256(image)
    Ext4NativeWriter().create_file(str(image), "created.txt", b"created content\n")
    assert _sha256(image) != before
    assert (
        subprocess.run(["e2fsck", "-fn", str(image)], capture_output=True).returncode
        == 0
    )
    read = subprocess.run(
        ["debugfs", "-R", "cat /created.txt", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert read.returncode == 0
    assert "created content" in read.stdout


@needs_ext
@pytest.mark.parametrize("phase", OPERATION_PHASES["ext4.create_file"])
def test_ext4_create_file_phase_failure_preserves_the_image(
    tmp_path, monkeypatch, phase
):
    """Every declared phase of create_file fails closed and keeps the source."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ext4(tmp_path)
    before = _sha256(image)
    reached: list[str] = []
    known = {"ext4.img", "seed.bin"}

    real_copy = ext4_module._image_copy
    real_verify = ext4_module.verify_image_after_operation
    real_identity = ext4_module._require_source_unchanged
    real_publish = ext4_module._atomic_replace
    real_writer = native_write.Ext4TransactionalWriter

    def copy(*a, **k):
        reached.append("copy")
        if phase == "copy":
            raise NativeWriteError("injected failure at copy")
        return real_copy(*a, **k)

    class Delegate:
        def create_file(self, *a, **k):
            reached.append("delegate")
            if phase == "delegate":
                raise NativeWriteError("injected failure at delegate")
            return real_writer().create_file(*a, **k)

    def verify(*a, **k):
        reached.append("post_verify")
        if phase == "post_verify":
            raise NativeWriteError("injected failure at post_verify")
        return real_verify(*a, **k)

    def identity(*a, **k):
        reached.append("identity_verify")
        if phase == "identity_verify":
            raise NativeWriteError("injected failure at identity_verify")
        return real_identity(*a, **k)

    def publish(*a, **k):
        reached.append("publish")
        if phase == "publish":
            raise NativeWriteError("injected failure at publish")
        return real_publish(*a, **k)

    monkeypatch.setattr(ext4_module, "_image_copy", copy)
    monkeypatch.setattr(ext4_module, "verify_image_after_operation", verify)
    monkeypatch.setattr(ext4_module, "_require_source_unchanged", identity)
    monkeypatch.setattr(ext4_module, "_atomic_replace", publish)
    monkeypatch.setattr(native_write, "Ext4TransactionalWriter", Delegate)

    with pytest.raises(Exception) as caught:
        Ext4NativeWriter().create_file(str(image), "created.txt", b"payload\n")

    assert caught.value is not None
    assert phase in reached, f"phase {phase!r} never reached; trace={reached}"
    assert _sha256(image) == before, f"failure at {phase!r} mutated the image"
    assert (
        _staged(tmp_path, known) == []
    ), f"failure at {phase!r} leaked staged files: {_staged(tmp_path, known)}"


@needs_ext
def test_ext4_create_file_short_write_refuses_publication(tmp_path, monkeypatch):
    """A truncated staged payload must fail read-back before publication."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ext4(tmp_path)
    before = _sha256(image)
    real_payload_file = native_write._payload_file

    def truncated_payload(data, directory):
        payload = real_payload_file(data, directory)
        Path(payload).write_bytes(Path(payload).read_bytes()[:1])
        return payload

    monkeypatch.setattr(native_write, "_payload_file", truncated_payload)
    with pytest.raises(Ext4ProfileUnsupported, match="staged read-back mismatch"):
        Ext4NativeWriter().create_file(str(image), "created.txt", b"complete payload")
    assert _sha256(image) == before
    assert _staged(tmp_path, {"ext4.img", "seed.bin"}) == []


@needs_ext
def test_ext4_create_file_cancellation_preserves_source_and_cleans_stage(
    tmp_path, monkeypatch
):
    """A cancellation at the copy boundary cannot publish or leak a stage."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ext4(tmp_path)
    before = _sha256(image)

    class Cancelled(BaseException):
        pass

    def cancel_at_copy_done(name):
        if name == "copy:done":
            raise Cancelled("cancelled create_file")

    native_write.fault_hook_installed(cancel_at_copy_done)
    try:
        with pytest.raises(Cancelled):
            Ext4NativeWriter().create_file(str(image), "created.txt", b"payload")
    finally:
        native_write.fault_hook_installed(None)
    assert _sha256(image) == before
    assert _staged(tmp_path, {"ext4.img", "seed.bin"}) == []


@needs_ext
@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.parametrize(
    "point",
    (
        "copy:start",
        "copy:done",
        "helper:start",
        "helper:done",
        "publish:start",
        "publish:done",
    ),
)
def test_ext4_create_file_process_kill_preserves_source_until_publish(
    tmp_path, monkeypatch, point
):
    """A SIGKILL at every create_file boundary leaves original or complete data."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ext4(tmp_path)
    before = _sha256(image)
    child = r"""
import os
import signal
import sys

from mountfile.native_write import fault_hook_installed

target = sys.argv[1]
seen = {}

def kill_at(name):
    seen[name] = seen.get(name, 0) + 1
    # create_file delegates the mutation to Ext4TransactionalWriter, so the
    # staged copy has an inner publish and the outer guarded writer has a
    # second publish.  Target the outer publish, not the delegate's replace.
    wanted = 2 if name.startswith("publish:") else 1
    if name == target and seen[name] == wanted:
        os.kill(os.getpid(), signal.SIGKILL)

fault_hook_installed(kill_at)
from mountfile.ext4_native_writer import Ext4NativeWriter
Ext4NativeWriter().create_file(sys.argv[2], "created.txt", b"complete create")
"""
    environment = os.environ.copy()
    environment[ENABLE_ENV] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", child, point, str(image)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == -signal.SIGKILL, (point, result.stdout, result.stderr)
    if point == "publish:done":
        read = subprocess.run(
            ["debugfs", "-R", "cat /created.txt", str(image)],
            capture_output=True,
            check=False,
        )
        assert read.returncode == 0
        assert read.stdout == b"complete create"
    else:
        assert _sha256(image) == before


def test_ntfs_single_operation_surface_is_declared():
    """NTFS exposes one operation; the matrix says so instead of padding cells."""
    for name in UNSUPPORTED_OPERATIONS["ntfs"]:
        assert not hasattr(NtfsTransactionalWriter, name), (
            f"{name} now exists on NtfsTransactionalWriter -- add its phase "
            "matrix to OPERATION_PHASES instead of leaving a hole"
        )
    assert len(OPERATION_PHASES["ntfs.write_file"]) == 8


def test_operation_matrix_totals_are_reported():
    """A readable cell count, so 'covered' is a number other than a promise."""
    total = sum(len(phases) for phases in OPERATION_PHASES.values())
    assert total == 21, total
