from __future__ import annotations

import hashlib

import pytest

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
