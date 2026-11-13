from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from mountfile.native_write_engine import (
    JournalWriter,
    TransactionalWriter,
    WriteOutcome,
    WritePlan,
    _sha256,
)


class FakeEngine:
    def __init__(self, *, prepare_error: bool = False, apply_error: bool = False):
        self.prepare_error = prepare_error
        self.apply_error = apply_error
        self.seen: list[Path] = []

    def prepare(self, copy: Path) -> None:
        self.seen.append(copy)
        if self.prepare_error:
            raise RuntimeError("prepare failed")

    def apply(self, copy: Path, plan: WritePlan) -> None:
        if self.apply_error:
            raise RuntimeError("apply failed")
        copy.write_bytes(copy.read_bytes() + b"|MUTATED|")


def plan(image: Path) -> WritePlan:
    return WritePlan(image=image, operation="put", args={"path": "/x"})


def test_commit_mutates_only_after_verification_and_records_hashes(tmp_path: Path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"ORIGINAL")
    before = _sha256(image)
    journal = JournalWriter(tmp_path / "audit.jsonl")

    receipt = TransactionalWriter(
        FakeEngine(),
        verify=lambda copy: b"MUTATED" in copy.read_bytes(),
        journal=journal,
    ).execute(plan(image))

    assert receipt.outcome is WriteOutcome.COMMITTED
    assert receipt.original_sha256 == before
    assert receipt.final_sha256 == _sha256(image)
    assert image.read_bytes() == b"ORIGINAL|MUTATED|"
    record = json.loads((tmp_path / "audit.jsonl").read_text())
    assert record["outcome"] == "committed"
    assert record["original_sha256"] == before


def test_prepare_failure_keeps_original_byte_identical(tmp_path: Path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"ORIGINAL")
    before = image.read_bytes()

    receipt = TransactionalWriter(
        FakeEngine(prepare_error=True), verify=lambda _: True
    ).execute(plan(image))

    assert receipt.outcome is WriteOutcome.FAILED_WRITE
    assert image.read_bytes() == before


def test_verify_failure_keeps_original_byte_identical(tmp_path: Path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"ORIGINAL")
    before = image.read_bytes()

    receipt = TransactionalWriter(FakeEngine(), verify=lambda _: False).execute(
        plan(image)
    )

    assert receipt.outcome is WriteOutcome.FAILED_VERIFY
    assert image.read_bytes() == before


def test_verifier_exception_is_fail_closed(tmp_path: Path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"ORIGINAL")

    def broken_verifier(_: Path) -> bool:
        raise ValueError("checker unavailable")

    receipt = TransactionalWriter(FakeEngine(), verify=broken_verifier).execute(
        plan(image)
    )

    assert receipt.outcome is WriteOutcome.FAILED_VERIFY
    assert "checker unavailable" in receipt.detail
    assert image.read_bytes() == b"ORIGINAL"


def test_locked_image_is_rejected_without_mutation(tmp_path: Path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"ORIGINAL")
    lock = image.with_name(image.name + ".lock")
    with lock.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="locked"):
            TransactionalWriter(FakeEngine(), verify=lambda _: True).execute(
                plan(image)
            )
    assert image.read_bytes() == b"ORIGINAL"
