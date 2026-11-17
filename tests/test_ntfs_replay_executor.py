from __future__ import annotations

import hashlib
import struct

import pytest

import mountfile.native_write as native_write
from mountfile.ntfs_logfile import NtfsLogFileAnalysis, ReplayStep, ReplayTarget
from mountfile.ntfs_replay_executor import (
    NtfsReplayResolutionError,
    publish_ntfs_replay,
)


def _analysis(*steps: ReplayStep) -> NtfsLogFileAnalysis:
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
        executable=False,
    )


def _step(payload: bytes = b"XYZ") -> ReplayStep:
    return ReplayStep(
        lsn=1,
        transaction_id=1,
        kind="redo",
        operation=0x07,
        length=len(payload),
        payload=payload,
        target_attr=128,
        target_vcn=0,
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


def test_mft_candidate_gate_validates_mirror_before_publication(tmp_path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcdef")
    publication = publish_ntfs_replay(
        str(image),
        _analysis(_step()),
        lambda step: ReplayTarget(step.target_attr, step.target_vcn, 2, step.length),
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
            mft_candidate_provider=lambda _step: (
                _raw_mft_record(7),
                _raw_mft_record(8),
                (8 << 48) | 42,
                42,
            ),
        )
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
