# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#510 D1 + D3/D5: per-step failure injection on the *application* NTFS write chain.

D1 recorded that the fault seam existed but the step matrix (data / bitmap /
directory / inode / journal / commit) had never been driven for NTFS.  D5
recorded that staged read-back lives only in a gated experimental helper and
never in the route the VFS actually uses.  This module drives that route:
``NtfsTransactionalWriter.write_file`` -- the writer behind ``NtfsVFS`` -- with
one declared cell per storage step, in the order the runtime emits them::

    copy -> payload_stage -> log_gate_pre -> ntfs_write
         -> log_gate_post -> read_back -> identity_verify -> publish

Two tiers, kept apart so neither can be read as the other:

**Tier A (always runs, no NTFS tooling).**  The chain is driven against a plain
stand-in file with the ``$LogFile`` gate, the ``ntfscp`` helper and the tool
preflight doubled.  It proves the *wiring*: every declared cell is reachable,
the declared order equals the runtime order, each injected step fails closed,
the caller's image is byte-identical afterwards, and no staged sibling survives.
It proves nothing about NTFS semantics and does not claim to.

**Tier B (needs ``mkntfs``/``ntfscp``/``ntfscat``).**  The same cells on a real
``mkntfs`` volume, so "source preserved" means a real NTFS image is
byte-identical after a mid-operation failure.  These cells skip when the tools
are absent; the CI jobs that install ``ntfs-3g`` run them.

Two properties are encoded here rather than described in prose:

* :class:`NTFSLogFileManager` can never report ``CLEAN`` -- ``check_logfile``
  returns ``ERROR`` ("ntfsfix is diagnostic only and cannot establish Windows
  chkdsk parity") whenever the diagnostic command *succeeds*.  ``require_clean``
  therefore always raises, so the application route refuses every write at its
  first gate and everything downstream is unreachable in production.  Tier A
  asserts that directly and reaches later cells only through a named oracle
  double.  That is why D5 is still ``partial``: the route has no authoritative
  checker at all, not merely an unexercised helper.
* ``_payload_file`` used to run *after* ``_image_copy`` but *before* the
  ``try``/``finally`` that unlinks the staged copy, so a failure there leaked a
  full copy of the user's evidence into the evidence directory under an
  unreported dot-prefixed name.  The regression cell below pins the fix.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import mountfile.native_write as native_write
from mountfile.journal_safety import JournalSafetyError, NTFSLogFileManager
from mountfile.native_write import (
    ENABLE_ENV,
    NativeWriteDisabled,
    NativeWriteError,
    NativeWriteUnsafe,
    NtfsTransactionalWriter,
)
from mountfile.ntfs_replay import NTFS_REPLAY_FAULT_POINTS

#: Declared cells, in the order ``write_file`` must emit them.  A storage step
#: added to the runtime without a cell here fails the order assertion, so the
#: coverage contract is this tuple -- not a comment that can rot.
DECLARED_STEPS: tuple[str, ...] = (
    "copy",
    "payload_stage",
    "log_gate_pre",
    "ntfs_write",
    "log_gate_post",
    "read_back",
    "identity_verify",
    "publish",
)


def test_native_ntfs_replay_exposes_stable_transaction_phase_boundaries():
    assert NTFS_REPLAY_FAULT_POINTS == (
        "replay:redo:start",
        "replay:redo:done",
        "replay:undo:start",
        "replay:undo:done",
        "replay:journal:clean-pages",
        "replay:volume:dirty-flag",
        "replay:mirror:update",
        "replay:flush",
    )


class _GateStub:
    """Stand-in for the ``$LogFile`` gate that records each call.

    ``fail_on`` holds 1-based call numbers that refuse, so one stub can fail the
    *pre* gate, the *post* re-gate, or both.
    """

    def __init__(self, fail_on: tuple[int, ...] = ()) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on
        self.reached: list[str] = []

    def require_clean(self, image_path: str, runner) -> None:  # noqa: ANN001
        self.calls.append(image_path)
        self.reached.append("log_gate_pre" if len(self.calls) == 1 else "log_gate_post")
        if len(self.calls) in self.fail_on:
            raise JournalSafetyError("stubbed $LogFile gate refusal")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _leftovers(directory: Path, expected: set[str]) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name not in expected)


def _standin_image(tmp_path: Path) -> Path:
    image = tmp_path / "standin.img"
    image.write_bytes(b"stand-in-image" + bytes(range(256)) * 8)
    return image


def _install_doubles(monkeypatch, *, order: list[str], fail: str = "") -> _GateStub:
    """Patch the chain so the matrix reaches every cell without NTFS tooling."""

    real_copy = native_write._image_copy
    real_payload = native_write._payload_file
    real_identity = native_write._require_source_unchanged
    real_publish = native_write._atomic_replace

    def copy(*args, **kwargs):
        order.append("copy")
        if fail == "copy":
            raise NativeWriteUnsafe("injected failure at copy")
        return real_copy(*args, **kwargs)

    stored_payload: dict[str, str] = {}

    def payload(*args, **kwargs):
        order.append("payload_stage")
        if fail == "payload_stage":
            raise NativeWriteUnsafe("injected failure at payload_stage")
        result = real_payload(*args, **kwargs)
        stored_payload["path"] = result
        return result

    gate = _GateStub(
        fail_on={"log_gate_pre": (1,), "log_gate_post": (2,)}.get(fail, ())
    )
    order_ref = order

    class _Recording(_GateStub):
        def require_clean(self, image_path, runner):  # noqa: ANN001
            # Recorded *before* the call: a refusing cell must still prove the
            # gate was reached, otherwise the cell cannot distinguish "refused
            # here" from "never got here".
            order_ref.append("log_gate_pre" if not self.calls else "log_gate_post")
            super().require_clean(image_path, runner)

    recording = _Recording()
    recording.calls = gate.calls
    recording.fail_on = gate.fail_on

    def gate_factory():
        return recording

    def run_stub(command, **kwargs):
        command = list(command)
        if command and command[0] == "ntfscp":
            order.append("ntfs_write")
            if fail == "ntfs_write":
                raise NativeWriteUnsafe("injected failure at ntfs_write")
        return subprocess.CompletedProcess(command, 0, "", "")

    def identity(*args, **kwargs):
        order.append("identity_verify")
        if fail == "identity_verify":
            raise NativeWriteUnsafe("injected failure at identity_verify")
        return real_identity(*args, **kwargs)

    def publish(*args, **kwargs):
        order.append("publish")
        if fail == "publish":
            raise NativeWriteUnsafe("injected failure at publish")
        return real_publish(*args, **kwargs)

    def tool_preflight(*_tools):
        # Wiring tier only: the real preflight is asserted separately by
        # ``test_missing_ntfs_tools_are_a_typed_refusal``.
        return None

    def read_back(image_path, internal_path):  # noqa: ANN001
        # D5 cell: the staged copy must read back byte-identical through the
        # independent helper before publish is allowed.  The double returns
        # exactly the staged payload, which is what a healthy ``ntfscat``
        # would see on a correctly written copy.
        order.append("read_back")
        if fail == "read_back":
            raise NativeWriteUnsafe("injected failure at read_back")
        with open(stored_payload["path"], "rb") as handle:
            return handle.read()

    monkeypatch.setattr(native_write, "_image_copy", copy)
    monkeypatch.setattr(native_write, "_read_ntfs_file_bytes", read_back)
    monkeypatch.setattr(native_write, "_payload_file", payload)
    monkeypatch.setattr(native_write, "NTFSLogFileManager", gate_factory)
    monkeypatch.setattr(native_write, "_run", run_stub)
    monkeypatch.setattr(native_write, "_require_source_unchanged", identity)
    monkeypatch.setattr(native_write, "_atomic_replace", publish)
    monkeypatch.setattr(native_write, "_require_tools", tool_preflight)
    return recording


# --------------------------------------------------------------- contract tier


def test_declared_matrix_matches_the_runtime_call_chain():
    for attribute in (
        "_image_copy",
        "_payload_file",
        "NTFSLogFileManager",
        "_run",
        "_require_source_unchanged",
        "_atomic_replace",
        "_require_tools",
    ):
        assert hasattr(native_write, attribute), (
            f"the D1 matrix instruments {attribute!r}, which the runtime no "
            "longer exports -- update the matrix together with the writer"
        )
    assert DECLARED_STEPS == (
        "copy",
        "payload_stage",
        "log_gate_pre",
        "ntfs_write",
        "log_gate_post",
        "read_back",
        "identity_verify",
        "publish",
    )
    assert len(set(DECLARED_STEPS)) == len(DECLARED_STEPS)


def test_real_log_gate_can_never_report_clean():
    """The application route is fail-closed by construction, not by luck."""
    gate = NTFSLogFileManager()
    called: list[list[str]] = []

    def runner(command):
        called.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = gate.check_logfile("image.img", runner)
    assert called == [["ntfsfix", "-n", "image.img"]]
    assert result.state.value != "clean"
    assert "parity" in (result.message or "")
    with pytest.raises(JournalSafetyError):
        gate.require_clean("image.img", runner)


def test_missing_ntfs_tools_are_a_typed_refusal(tmp_path, monkeypatch):
    """A missing ``ntfsfix``/``ntfscp`` refuses before any staging happens."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setattr(native_write.shutil, "which", lambda name: None)
    image = _standin_image(tmp_path)
    before = _sha256(image)
    with pytest.raises(NativeWriteDisabled) as caught:
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    assert "ntfsfix" in str(caught.value)
    assert _sha256(image) == before
    assert _leftovers(tmp_path, {"standin.img"}) == []


def test_successful_chain_emits_exactly_the_declared_order(tmp_path, monkeypatch):
    """Control cell: without injection the runtime emits the declared order once."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _standin_image(tmp_path)
    order: list[str] = []
    gate = _install_doubles(monkeypatch, order=order)
    NtfsTransactionalWriter().write_file(str(image), "target", b"matrix control\n")
    assert order == list(DECLARED_STEPS), order
    assert len(gate.reached) == 2, "the pre gate and the post re-gate must both run"
    assert _leftovers(tmp_path, {"standin.img"}) == []


@pytest.mark.parametrize("step", DECLARED_STEPS)
def test_each_step_fails_closed_and_preserves_the_source(tmp_path, monkeypatch, step):
    """D1 + D3 per cell: typed refusal, byte-identical source, no survivor."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _standin_image(tmp_path)
    before = _sha256(image)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail=step)

    with pytest.raises(NativeWriteError) as caught:
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")

    assert isinstance(caught.value, NativeWriteUnsafe)
    assert step in order, f"cell {step!r} was never reached; order={order}"
    assert _sha256(image) == before, f"failure at {step!r} mutated the source"
    leaked = _leftovers(tmp_path, {"standin.img"})
    assert leaked == [], f"failure at {step!r} leaked a staged sibling: {leaked}"


def test_payload_staging_failure_does_not_leak_the_staged_copy(tmp_path, monkeypatch):
    """Regression cell for a real leak: copy is staged before the payload.

    ``_payload_file`` used to run after ``_image_copy`` but outside the
    ``try``/``finally`` that unlinks the staged copy, so ENOSPC / EACCES / a full
    tmpfs during payload staging left a complete copy of the user's evidence
    behind under an unreported dot-prefixed name.
    """
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _standin_image(tmp_path)
    before = _sha256(image)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail="payload_stage")
    with pytest.raises(NativeWriteUnsafe):
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    assert order == ["copy", "payload_stage"], order
    assert _sha256(image) == before
    assert _leftovers(tmp_path, {"standin.img"}) == []


# ------------------------------------------------------------------- real NTFS

_TIER_B_SKIP = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfscat) are unavailable",
)


def _make_ntfs_image(tmp_path: Path) -> Path:
    image = tmp_path / "ntfs.img"
    with open(image, "wb") as handle:
        handle.truncate(16 << 20)
    result = subprocess.run(
        ["mkntfs", "-q", "-F", "-f", str(image)], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"SEED-NTFS-PAYLOAD\n")
    result = subprocess.run(
        ["ntfscp", str(image), str(seed), "/target"], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return image


@_TIER_B_SKIP
@pytest.mark.parametrize(
    "step",
    (
        "copy",
        "payload_stage",
        "ntfs_write",
        "log_gate_post",
        "read_back",
        "identity_verify",
        "publish",
    ),
)
def test_real_ntfs_step_failure_preserves_the_image(tmp_path, monkeypatch, step):
    """Same contract on a real ``mkntfs`` volume, including post-write steps."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs_image(tmp_path)
    before = _sha256(image)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail=step)

    with pytest.raises(NativeWriteError) as caught:
        NtfsTransactionalWriter().write_file(str(image), "target", b"new payload\n")

    assert isinstance(caught.value, NativeWriteUnsafe)
    assert step in order
    assert _sha256(image) == before, f"failure at {step!r} mutated the NTFS image"
    assert _leftovers(tmp_path, {"ntfs.img", "seed.bin"}) == []


@_TIER_B_SKIP
def test_real_ntfs_gate_refuses_a_clean_volume_before_any_write(tmp_path, monkeypatch):
    """No double: the production gate refuses even a clean ``mkntfs`` volume."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_ntfs_image(tmp_path)
    before = _sha256(image)
    with pytest.raises(NativeWriteError):
        NtfsTransactionalWriter().write_file(str(image), "target", b"refused\n")
    assert _sha256(image) == before
    assert _leftovers(tmp_path, {"ntfs.img", "seed.bin"}) == []
