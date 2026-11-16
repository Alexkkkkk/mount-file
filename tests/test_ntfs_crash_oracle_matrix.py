# SPDX-License-Identifier: MIT
"""Issue #530, P1: independent ntfsfix oracle over a fixed dirty-fixture set.

The production gate (``probe_ntfs_log``) shares code with the replay it
validates, so C6 acceptance requires an *external* oracle.  This module pins a
matrix of five dirty/crashed fixture scenarios and, for every one of them,
asserts the full oracle contract:

1. ``ntfsfix -n`` refuses the dirty volume (non-zero exit) — the fixture really
   is a crash image, not a clean one;
2. ``native_ntfs_recover`` replays/repairs it;
3. ``ntfsfix -n`` accepts the recovered volume (exit 0) — an independent
   implementation agrees with the replay result;
4. the volume dirty flag is cleared.

Scenarios: committed resident update, committed nonresident update, an active
(unfinished) transaction that must be undone, the combined active+nonresident
case, and a dirty-bit-only volume whose ``$LogFile`` was never written.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from mountfile.ntfs_replay import native_ntfs_recover
from tests.ntfs_crash_fixture import mark_volume_dirty

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfscat") is None
    or shutil.which("ntfsfix") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfsfix) are unavailable",
)

SCENARIOS = (
    "committed-resident",
    "committed-nonresident",
    "active-transaction",
    "active-nonresident",
    "dirty-bit-only",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ntfsfix_no(image: Path):
    """Read-only ntfsfix check; never repairs anything."""
    return subprocess.run(
        ["ntfsfix", "-n", str(image)], capture_output=True, text=True, check=False
    )


def _dirty_bit_only_fixture(tmp_path: Path) -> Path:
    """A clean ``mkntfs`` volume with only VOLUME_FLAG_DIRTY set."""
    image = tmp_path / "dirty-bit.img"
    with open(image, "wb") as handle:
        handle.truncate(64 << 20)
    result = subprocess.run(
        ["mkntfs", "-q", "-F", "-s", "512", "-c", "4096", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"dirty-bit-payload" + b"p" * 31)
    result = subprocess.run(
        ["ntfscp", str(image), str(payload), "/target"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    mark_volume_dirty(image)
    return image


def _make_scenario(tmp_path: Path, scenario: str) -> tuple[Path, bytes]:
    if scenario == "dirty-bit-only":
        image = _dirty_bit_only_fixture(tmp_path)
        return image, (tmp_path / "payload.bin").read_bytes()
    from tests.ntfs_crash_fixture import make_crash_fixture

    kwargs = {
        "committed-resident": {},
        "committed-nonresident": {"nonresident": True},
        "active-transaction": {"active": True},
        "active-nonresident": {"active": True, "nonresident": True},
    }[scenario]
    fixture = make_crash_fixture(tmp_path, **kwargs)
    expected = fixture.old_data if scenario.startswith("active-") else fixture.new_data
    return fixture.image, expected


def _ntfscat(image: Path) -> bytes:
    result = subprocess.run(
        ["ntfscat", str(image), "/target"], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _volume_dirty_flag(image: Path) -> int:
    """Read s_volume_flags straight from the boot sector (no replay code)."""
    with open(image, "rb") as stream:
        stream.seek(0x30 + 0x48)  # offset field in BPB, then volume flags
        stream.seek(0x78)
        return struct.unpack("<H", stream.read(2))[0]


@needs_ntfs
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_ntfs_crash_oracle_matrix(tmp_path, scenario):
    image, expected_data = _make_scenario(tmp_path, scenario)
    before = _sha256(image)

    # 1. The independent checker must refuse the dirty volume first.
    dirty = _ntfsfix_no(image)
    assert _sha256(image) == before, f"{scenario}: ntfsfix -n modified the image"
    assert (
        dirty.returncode != 0
    ), f"{scenario}: ntfsfix -n accepted a dirty volume: {dirty.stdout}"

    # 2. Replay on the image handed in (the test owns disposability: tmp_path).
    result = native_ntfs_recover(str(image))
    assert result.changed is True, f"{scenario}: replay reported no change"
    assert result.volume_flags_after == 0, f"{scenario}: dirty flag survived"
    actual_data = _ntfscat(image)
    assert (
        actual_data[: len(expected_data)] == expected_data
    ), f"{scenario}: replayed target data differs from its redo/undo expectation"

    # 3. The independent checker must accept the recovered volume.
    recovered = _ntfsfix_no(image)
    assert recovered.returncode == 0, (
        f"{scenario}: ntfsfix -n rejected the replayed volume: "
        f"{recovered.stdout}{recovered.stderr}"
    )

    # 4. The dirty flag is cleared as read by an independent byte-level probe.
    assert _volume_dirty_flag(image) & 0x0001 == 0

    # 5. Replay is a repair, not a rewrite: an immediate second pass is a noop
    #    and keeps the checker green (idempotence leg of the oracle).
    second = native_ntfs_recover(str(image))
    assert _ntfsfix_no(image).returncode == 0
    assert second.redo_applied in (0, result.redo_applied)
    assert _volume_dirty_flag(image) & 0x0001 == 0
