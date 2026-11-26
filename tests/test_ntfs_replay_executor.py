from __future__ import annotations

import hashlib
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import mountfile.native_write as native_write
import mountfile.ntfs_replay_executor as replay_executor
from mountfile.ntfs_logfile import NtfsLogFileAnalysis, ReplayStep, ReplayTarget
from mountfile.ntfs_mft_allocation import MftAllocationCandidate
from mountfile.ntfs_replay_executor import (
    NTFS_ALLOCATION_FAULT_POINTS,
    NTFS_EXECUTOR_FAULT_POINTS,
    NtfsReplayResolutionError,
    publish_ntfs_replay,
)


def _analysis(*steps: ReplayStep, executable: bool = True) -> NtfsLogFileAnalysis:
    return NtfsLogFileAnalysis(
        restart=None,  # type: ignore[arg-type] - synthetic bounded fixture
        page_size=4096,
        first_page=0,
        log_size=4096,
        scanned_bytes=4096,
        current_lsn=2,
        checkpoint_lsn=1,
        oldest_lsn=1,
        redo_plan=tuple(steps),
        undo_plan=(),
        truncated=False,
        executable=executable,
    )


def _step(
    payload: bytes = b"XYZ",
    *,
    operation: int = 0x07,
    target_attr: int = 128,
    target_vcn: int = 0,
) -> ReplayStep:
    return ReplayStep(
        lsn=1,
        transaction_id=1,
        kind="redo",
        operation=operation,
        length=len(payload),
        payload=payload,
        target_attr=target_attr,
        target_vcn=target_vcn,
        record_off=1024,
        attr_off=96,
        cluster_off=8,
        page_lcns=(42,),
    )


def _raw_mft_record(sequence: int, *, in_use: bool = False) -> bytes:
    flags = 0x0001 if in_use else 0
    record = bytearray(512)
    record[0:4] = b"FILE"
    struct.pack_into("<HH", record, 0x04, 0x2A, 2)
    struct.pack_into("<H", record, 0x10, sequence)
    struct.pack_into("<H", record, 0x14, 0x30)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<II", record, 0x18, 0x38, 512)
    struct.pack_into("<H", record, 0x2A, sequence)
    struct.pack_into("<H", record, 0x2C, 0x5A5A)
    struct.pack_into("<H", record, 510, sequence)
    return bytes(record)


def _allocation_step(payload: bytes) -> ReplayStep:
    return ReplayStep(
        lsn=1,
        transaction_id=1,
        kind="redo",
        operation=0x02,
        length=len(payload),
        payload=payload,
        target_attr=128,
        target_vcn=0,
        record_off=0,
        attr_off=0,
        cluster_off=0,
    )


def test_resolution_failure_happens_before_source_publication(tmp_path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = hashlib.sha256(image.read_bytes()).digest()

    with pytest.raises(NtfsReplayResolutionError, match="resolver"):
        publish_ntfs_replay(
            str(image),
            _analysis(_step()),
            lambda _step: (_ for _ in ()).throw(
                RuntimeError("MFT record is unresolved")
            ),
        )

    assert hashlib.sha256(image.read_bytes()).digest() == before
    assert list(tmp_path.glob(".mountfile-image-*")) == []


def test_non_executable_analysis_is_refused_before_staging(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = image.read_bytes()
    with pytest.raises(NtfsReplayResolutionError, match="non-executable"):
        publish_ntfs_replay(
            str(image),
            _analysis(_step(), executable=False),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, 3),
        )
    assert image.read_bytes() == before
    assert list(tmp_path.glob(".mountfile-image-*")) == []


def test_replay_step_preserves_filesystem_target_hints() -> None:
    step = _step()
    assert step.record_off == 1024
    assert step.attr_off == 96
    assert step.cluster_off == 8
    assert step.page_lcns == (42,)


def test_resolver_backed_replay_publishes_only_after_readback(tmp_path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")

    publication = publish_ntfs_replay(
        str(image),
        _analysis(_step()),
        lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, step.length),
    )

    assert image.read_bytes() == b"abXYZf"
    assert publication.sha256_before == hashlib.sha256(b"abcdef").hexdigest()
    assert publication.sha256_after == hashlib.sha256(b"abXYZf").hexdigest()
    assert len(publication.provenance) == 1


def test_overlapping_distinct_targets_are_refused_before_staging_mutation(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = image.read_bytes()
    analysis = _analysis(_step(), _step(target_attr=129))

    with pytest.raises(NtfsReplayResolutionError, match="overlap"):
        publish_ntfs_replay(
            str(image),
            analysis,
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, 3),
        )

    assert image.read_bytes() == before
    assert list(tmp_path.glob(".mountfile-image-*")) == []


def test_mft_candidate_gate_validates_mirror_before_publication(tmp_path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    publication = publish_ntfs_replay(
        str(image),
        _analysis(_step()),
        lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, step.length),
        verify=lambda _staged: True,
        mft_candidate_provider=lambda _step: (
            _raw_mft_record(7),
            _raw_mft_record(7),
            (8 << 48) | 42,
            42,
        ),
    )
    assert publication.sha256_after == hashlib.sha256(b"abXYZf").hexdigest()


def test_mft_candidate_mirror_mismatch_is_refused_before_source_change(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = image.read_bytes()
    with pytest.raises(NtfsReplayResolutionError, match="MFT allocation refused"):
        publish_ntfs_replay(
            str(image),
            _analysis(_step()),
            lambda step: ReplayTarget(
                step.target_attr, step.target_vcn, 2, step.length
            ),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: (
                _raw_mft_record(7),
                _raw_mft_record(8),
                (8 << 48) | 42,
                42,
            ),
        )
    assert image.read_bytes() == before
    assert list(tmp_path.glob(".mountfile-image-*")) == []


@pytest.mark.parametrize(
    "operation",
    [0x02, 0x03, *range(0x0C, 0x17), *range(0x21, 0x25)],
)
@pytest.mark.parametrize("provider_kind", ("absent", "none", "valid"))
def test_allocation_sensitive_operations_refuse_before_resolution(
    tmp_path, operation, provider_kind
):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = image.read_bytes()
    provider_calls = []
    publish_kwargs = {}

    if provider_kind != "absent":

        def candidate_provider(_step):
            provider_calls.append(True)
            if provider_kind == "none":
                return None
            record = _raw_mft_record(7)
            return record, record, (8 << 48) | 42, 42

        publish_kwargs["mft_candidate_provider"] = candidate_provider

    def resolver(_step):
        pytest.fail("allocation-sensitive step must not reach the resolver")

    with pytest.raises(
        NtfsReplayResolutionError, match="allocation-sensitive operation"
    ):
        publish_ntfs_replay(
            str(image),
            _analysis(_step(operation=operation)),
            resolver,
            **publish_kwargs,
        )

    if operation != 0x02:
        assert provider_calls == []
    assert image.read_bytes() == before
    assert list(tmp_path.glob(".mountfile-image-*")) == []


def test_publication_boundaries_are_emitted_once(tmp_path, monkeypatch) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    trace: list[str] = []
    monkeypatch.setattr(native_write, "_fault", trace.append)

    publish_ntfs_replay(
        str(image),
        _analysis(_step()),
        lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, step.length),
    )

    assert trace.count("publish:start") == 1
    assert trace.count("publish:done") == 1


def test_executor_phase_hooks_cover_preflight_verify_and_source_identity(
    tmp_path,
):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    trace: list[str] = []
    previous = native_write.FAULT_HOOK
    native_write.fault_hook_installed(trace.append)
    try:
        publish_ntfs_replay(
            str(image),
            _analysis(_step()),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, 3),
            verify=lambda staged: True,
        )
    finally:
        native_write.fault_hook_installed(previous)
    assert NTFS_EXECUTOR_FAULT_POINTS == (
        "executor:preflight:done",
        "executor:verify:start",
        "executor:verify:done",
        "executor:source:unchanged",
    )
    assert all(point in trace for point in NTFS_EXECUTOR_FAULT_POINTS)


def test_allocation_fault_boundaries_are_emitted_in_write_order(tmp_path):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )
    trace: list[str] = []
    previous = native_write.FAULT_HOOK
    native_write.fault_hook_installed(trace.append)
    try:
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )
    finally:
        native_write.fault_hook_installed(previous)
    phases = [
        "executor:preflight:done",
        "executor:allocation:mft:start",
        "executor:allocation:mft:done",
        "executor:allocation:mirror:start",
        "executor:allocation:mirror:done",
        "executor:allocation:bitmap:start",
        "executor:allocation:bitmap:done",
        "executor:mutation:done",
        "executor:source:unchanged",
    ]
    assert [item for item in trace if item in phases] == phases
    assert all(point in trace for point in NTFS_ALLOCATION_FAULT_POINTS)


def test_mft_allocation_publication_requires_independent_verifier(tmp_path):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )

    with pytest.raises(
        NtfsReplayResolutionError, match="independent staged-image verifier"
    ):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            mft_candidate_provider=lambda _step: candidate,
        )

    assert image.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["image.bin"]


@pytest.mark.parametrize("verification_result", [None, False, 1, "pass"])
def test_mft_allocation_refuses_verifier_without_explicit_true(
    tmp_path, verification_result
):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )

    with pytest.raises(
        NtfsReplayResolutionError, match="did not explicitly approve replay"
    ):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: verification_result,
            mft_candidate_provider=lambda _step: candidate,
        )

    assert image.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["image.bin"]


def test_generic_replay_refuses_verifier_that_does_not_approve(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    before = image.read_bytes()

    with pytest.raises(
        NtfsReplayResolutionError, match="did not explicitly approve replay"
    ):
        publish_ntfs_replay(
            str(image),
            _analysis(_step()),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, 3),
            verify=lambda _staged: False,
        )

    assert image.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["image.bin"]


@pytest.mark.parametrize("phase", NTFS_ALLOCATION_FAULT_POINTS)
def test_allocation_phase_failure_preserves_source_and_cleans_stage(tmp_path, phase):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )
    reached: list[str] = []
    previous = native_write.FAULT_HOOK

    def fail_at(name: str) -> None:
        if name == phase:
            reached.append(name)
            raise RuntimeError(f"injected failure at {name}")

    native_write.fault_hook_installed(fail_at)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            publish_ntfs_replay(
                str(image),
                _analysis(_allocation_step(initialized)),
                lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
                verify=lambda _staged: True,
                mft_candidate_provider=lambda _step: candidate,
            )
    finally:
        native_write.fault_hook_installed(previous)

    assert reached == [phase]
    assert image.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["image.bin"]


_SIGKILL_ALLOCATION_CHILD = r"""
import os
import signal
import sys

import mountfile.native_write as native_write
from mountfile.ntfs_logfile import ReplayTarget
from mountfile.ntfs_replay_executor import publish_ntfs_replay
from tests.test_ntfs_replay_executor import (
    _allocation_step,
    _analysis,
    _raw_mft_record,
)
from mountfile.ntfs_mft_allocation import MftAllocationCandidate

image, target_phase = sys.argv[1:]
free = _raw_mft_record(7)
mirror = _raw_mft_record(7)
initialized = _raw_mft_record(8, in_use=True)
candidate = MftAllocationCandidate(
    free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
)

def kill_at(name):
    if name == target_phase:
        os.kill(os.getpid(), signal.SIGKILL)

native_write.fault_hook_installed(kill_at)
publish_ntfs_replay(
    image,
    _analysis(_allocation_step(initialized)),
    lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
    verify=lambda _staged: True,
    mft_candidate_provider=lambda _step: candidate,
)
"""


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.parametrize("phase", NTFS_ALLOCATION_FAULT_POINTS)
@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="SIGKILL is a POSIX-only signal; abrupt-termination coverage on Windows is provided by the Windows NTFS acceptance harness (ntfs-windows-acceptance.yml), which kills a real child writer and validates the volume with chkdsk",
)
def test_allocation_phase_sigkill_preserves_source(tmp_path, phase):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", _SIGKILL_ALLOCATION_CHILD, str(image), phase],
        cwd=environment["PYTHONPATH"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == -signal.SIGKILL, (
        phase,
        result.stdout,
        result.stderr,
    )
    assert image.read_bytes() == before
    # SIGKILL prevents the child from running the executor's finally block.
    # The staged copy must therefore remain unpublished and disposable; the
    # harness removes it after proving source preservation.
    stages = sorted(tmp_path.glob(".mountfile-image-*.tmp"))
    assert len(stages) == 1
    stages[0].unlink()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["image.bin"]


def test_allocation_refuses_mismatched_mirror_record_size(tmp_path):
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    image.write_bytes(free + b"\x00" * 1024)
    candidate = MftAllocationCandidate(
        free, free[:-1], (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )
    with pytest.raises(NtfsReplayResolutionError, match="sizes differ"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(_raw_mft_record(8, in_use=True))),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )


def test_bounded_mft_allocation_updates_record_and_mirror_on_stage(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = bytearray(_raw_mft_record(8, in_use=True))
    struct.pack_into("<Q", initialized, 0x08, 1)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )
    range_reads = []
    read_candidate_range = replay_executor._read_candidate_range

    def track_range_read(stream, offset, expected, name):
        range_reads.append((offset, len(expected), name))
        return read_candidate_range(stream, offset, expected, name)

    monkeypatch.setattr(replay_executor, "_read_candidate_range", track_range_read)

    publication = publish_ntfs_replay(
        str(image),
        _analysis(_allocation_step(bytes(initialized))),
        lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
        verify=lambda _staged: True,
        mft_candidate_provider=lambda _step: candidate,
    )

    assert publication.sha256_after != publication.sha256_before
    raw = image.read_bytes()
    assert raw[:512] == raw[1000:1512]
    assert raw[1512:1528] == bytes((0, 0, 0, 0, 0, 0x04, 0, 0)) + bytes(8)
    assert struct.unpack_from("<H", raw, 0x10)[0] == 8
    assert range_reads == [
        (0, 512, "MFT"),
        (1000, 512, "MFT mirror"),
        (1512, 16, "bitmap"),
    ]


def test_bounded_mft_allocation_refuses_changed_candidate_source(tmp_path) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(b"X" * 512 + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )

    with pytest.raises(NtfsReplayResolutionError, match="source identity"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )
    assert image.read_bytes() == before


def test_bounded_mft_allocation_refuses_allocated_bitmap_bit(tmp_path) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    bitmap = bytearray(16)
    bitmap[5] = 0x04
    image.write_bytes(free + b"\x00" * 488 + mirror + bytes(bitmap))
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(bitmap), 42
    )

    with pytest.raises(NtfsReplayResolutionError, match="already allocated"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )


def test_bounded_mft_allocation_refuses_bitmap_range_out_of_bounds(tmp_path) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 16)
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 2020, bytes(16), 42
    )

    with pytest.raises(NtfsReplayResolutionError, match="out of bounds"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )


def test_bounded_mft_allocation_refuses_overlapping_metadata_extents(tmp_path) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 512)
    candidate = MftAllocationCandidate(
        free, free, (8 << 48) | 42, 42, 0, 0, 1024, bytes(16), 42
    )

    with pytest.raises(NtfsReplayResolutionError, match="candidate extents overlap"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )
    assert image.read_bytes() == free + b"\x00" * 512


def test_bounded_mft_allocation_publish_failure_leaves_source_unchanged(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "image.bin"
    free = _raw_mft_record(7)
    mirror = _raw_mft_record(7)
    initialized = _raw_mft_record(8, in_use=True)
    image.write_bytes(free + b"\x00" * 488 + mirror + b"\x00" * 512)
    before = image.read_bytes()
    candidate = MftAllocationCandidate(
        free, mirror, (8 << 48) | 42, 42, 0, 1000, 1512, bytes(16), 42
    )

    def fail_publish(_source, _temporary):
        raise OSError("publish failed")

    monkeypatch.setattr(replay_executor, "_atomic_replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        publish_ntfs_replay(
            str(image),
            _analysis(_allocation_step(initialized)),
            lambda step: ReplayTarget(step.target_attr, step.target_vcn, 0, 512),
            verify=lambda _staged: True,
            mft_candidate_provider=lambda _step: candidate,
        )
    assert image.read_bytes() == before
