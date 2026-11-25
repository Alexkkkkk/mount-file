# SPDX-License-Identifier: MIT
"""Catalog of dirty NTFS crash fixtures with an external-oracle harness.

Issue #530 (P1) / #595 requires at least five distinct dirty-fixture
scenarios and a replay run of the production executor on each of them. The
Linux checks here combine exact ``ntfscat`` read-back, ``ntfsfix -n`` as a
secondary consistency checker, and byte comparisons between two runs of the
same executor. These checks are not an independent replay oracle and do not
establish Windows/macOS parity. ``chkdsk`` remains the authoritative Windows
checker; Linux evidence reports it as unavailable rather than claiming parity.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests.ntfs_crash_fixture import corrupt_mft_mirror, make_crash_fixture

#: Scenario name -> constructor kwargs applied on top of the base crash image.
SCENARIOS: dict[str, dict[str, object]] = {
    # committed update, checkpoint already written
    "resident-commit": {"active": False},
    # commit record present but the checkpoint was never written
    "resident-active": {"active": True},
    # committed nonresident $DATA update
    "nonresident-commit": {"nonresident": True},
    # commit without checkpoint on a nonresident stream (compound)
    "nonresident-active": {"active": True, "nonresident": True},
    # A partial nonresident update spans adjacent clusters; cover redo and undo.
    "nonresident-cross-cluster-commit": {
        "nonresident": True,
        "nonresident_update_offset": 4096 - 32,
    },
    "nonresident-cross-cluster-active": {
        "active": True,
        "nonresident": True,
        "nonresident_update_offset": 4096 - 32,
    },
    # the volume dirty flag was set without a $LogFile flush
    "volume-dirty-no-log": {"dirty_only": True},
    # $MFTMirr record 0 destroyed on top of a committed update
    "mft-mirr-corrupt": {"corrupt_mirror": True},
}


@dataclass(frozen=True)
class DirtyFixture:
    scenario: str
    image: Path
    record_number: int
    record_size: int
    old_data: bytes
    new_data: bytes
    active: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_dirty_fixture(root: Path, scenario: str) -> DirtyFixture:
    """Materialize one named dirty fixture under ``root``."""
    options = SCENARIOS[scenario]
    work = root / scenario
    work.mkdir(parents=True, exist_ok=True)
    if options.get("dirty_only"):
        # A clean volume whose dirty flag is set without any $LogFile flush.
        from tests.ntfs_crash_fixture import mark_volume_dirty

        image = work / "dirty.img"
        with open(image, "wb") as handle:
            handle.truncate(64 << 20)
        subprocess.run(
            ["mkntfs", "-q", "-F", "-s", "512", "-c", "4096", str(image)],
            check=True,
            capture_output=True,
        )
        payload = work / "payload.bin"
        payload.write_bytes(b"CLEAN-VOLUME-" + b"p" * 40)
        subprocess.run(
            ["ntfscp", str(image), str(payload), "/target"],
            check=True,
            capture_output=True,
        )
        mark_volume_dirty(image)
        return DirtyFixture(
            scenario=scenario,
            image=image,
            record_number=0,
            record_size=1024,
            old_data=payload.read_bytes(),
            new_data=payload.read_bytes(),
            active=False,
        )
    fixture = make_crash_fixture(
        work,
        active=bool(options.get("active")),
        nonresident=bool(options.get("nonresident")),
        nonresident_update_offset=int(options.get("nonresident_update_offset", 0)),
    )
    if options.get("corrupt_mirror"):
        corrupt_mft_mirror(fixture.image)
    return DirtyFixture(
        scenario=scenario,
        image=fixture.image,
        record_number=fixture.record_number,
        record_size=fixture.record_size,
        old_data=fixture.old_data,
        new_data=fixture.new_data,
        active=bool(options.get("active")),
    )


def build_all_fixtures(root: Path) -> list[DirtyFixture]:
    """Materialize every cataloged scenario (>= 5 by contract)."""
    return [build_dirty_fixture(root, name) for name in sorted(SCENARIOS)]


def _ntfscat(image: Path, name: str) -> bytes:
    result = subprocess.run(
        ["ntfscat", str(image), name], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def _ntfsfix_check(image: Path) -> tuple[bool, str]:
    """Run the external ``ntfsfix -n`` oracle; return (clean, output)."""
    result = subprocess.run(
        ["ntfsfix", "-n", str(image)], capture_output=True, check=False
    )
    output = (result.stdout + result.stderr).decode(errors="replace")
    return result.returncode == 0, output.strip()


def _mft_record_stream(image: Path, fixture: DirtyFixture) -> bytes:
    mft = _ntfscat(image, "$MFT")
    start = fixture.record_number * fixture.record_size
    return mft[start : start + fixture.record_size]


def _source_integrity(image: Path, source_hash_before: str) -> dict[str, object]:
    """Prove (not assert) that the dirty fixture was left untouched."""
    source_hash_after = _sha256(image)
    return {
        "source_hash_before": source_hash_before,
        "source_hash_after": source_hash_after,
        "source_untouched": source_hash_before == source_hash_after,
    }


def _target_content_consistent(
    actual: bytes | None, expected: bytes, *, returncode: int = 0
) -> bool | None:
    """Return exact read-back equality, or ``None`` when reading failed."""
    if actual is None or returncode != 0:
        return None
    return actual == expected


def _oracle_verdict(
    *,
    mft_bytes_equal: bool,
    bitmap_equal: bool,
    clean: bool,
    content_correct: bool | None,
    source_untouched: bool,
) -> str:
    """Fail closed unless every required byte-level check was established."""
    if not source_untouched:
        return "divergence"
    if content_correct is None:
        return "unavailable"
    if mft_bytes_equal and bitmap_equal and clean and content_correct:
        return "pass"
    return "divergence"


def run_oracle(fixture: DirtyFixture, work: Path) -> dict[str, object]:
    """Replay the fixture twice independently and compare with the oracle.

    Returns a machine-readable verdict for the evidence artifact.  All byte
    comparisons are against raw streams extracted with external ``ntfscat``,
    never with our own parser.
    """
    source_hash_before = _sha256(fixture.image)
    ours = work / f"{fixture.scenario}-ours.img"
    twin = work / f"{fixture.scenario}-twin.img"
    shutil.copyfile(fixture.image, ours)
    shutil.copyfile(fixture.image, twin)

    from mountfile.ntfs_replay import native_ntfs_recover

    native_ntfs_recover(str(ours))
    native_ntfs_recover(str(twin))

    ours_mft = _mft_record_stream(ours, fixture)
    twin_mft = _mft_record_stream(twin, fixture)
    mft_bytes_equal = ours_mft == twin_mft
    try:
        bitmap_equal = _ntfscat(ours, "$Bitmap") == _ntfscat(twin, "$Bitmap")
    except RuntimeError:
        bitmap_equal = False
    clean, ntfsfix_output = _ntfsfix_check(ours)

    content_correct: bool | None = None
    try:
        written = subprocess.run(
            ["ntfscat", str(ours), "/target"], capture_output=True, check=False
        )
        expected = fixture.old_data if fixture.active else fixture.new_data
        content_correct = _target_content_consistent(
            written.stdout, expected, returncode=written.returncode
        )
    except OSError:
        content_correct = None

    source_integrity = _source_integrity(fixture.image, source_hash_before)
    verdict = _oracle_verdict(
        mft_bytes_equal=mft_bytes_equal,
        bitmap_equal=bitmap_equal,
        clean=clean,
        content_correct=content_correct,
        source_untouched=source_integrity["source_untouched"] is True,
    )

    return {
        "scenario": fixture.scenario,
        "replay": "pass",
        "ntfsfix_check": "pass" if clean else "reported_issues",
        "ntfsfix_output": ntfsfix_output[:400],
        "mft_record_bytes_equal": mft_bytes_equal,
        "bitmap_bytes_equal": bitmap_equal,
        "target_content_consistent": content_correct,
        # Proven, not asserted: the fixture hash is snapshotted before any
        # executor call and compared afterwards.  A previous revision
        # short-circuited this comparison with a constant-true expression,
        # a tautology that could never report tampering.
        **source_integrity,
        "replay_mutated_copy": _sha256(ours) != source_hash_before,
        "verdict": verdict,
    }


__all__ = [
    "DirtyFixture",
    "SCENARIOS",
    "build_all_fixtures",
    "build_dirty_fixture",
    "run_oracle",
]
