# SPDX-License-Identifier: MIT
"""Static contract tests for the real Windows NTFS acceptance workflow."""

from pathlib import Path

import pytest

from mountfile.ntfs_reader import NtfsMetadataError
from scripts.windows_ntfs_raw_snapshot import (
    _validate_device_window,
    _validate_sector_size,
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ntfs-windows-acceptance.yml"
SCRIPT = ROOT / "scripts/windows_ntfs_acceptance.ps1"
SNAPSHOT = ROOT / "scripts/windows_ntfs_raw_snapshot.py"
COMPARE = ROOT / "scripts/compare_ntfs_snapshots.py"


def test_windows_acceptance_uses_native_chkdsk_and_vhdx():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    assert "runs-on: windows-latest" in workflow
    assert "windows_native" in workflow
    assert "python -m pip install --no-deps ." in workflow
    assert "chkdsk" in script
    assert "diskpart.exe" in script
    assert "create vdisk" in script
    assert "format fs=ntfs" in script
    assert "windows_ntfs_raw_snapshot.py" in script
    assert "compare_ntfs_snapshots.py" in script
    assert "PhysicalSectorSize" in script
    assert script.count("--sector-size $sectorSize") == 3


def test_windows_acceptance_has_clean_dirty_and_repair_stages():
    script = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "chkdsk-scan-clean-volume",
        "mark-volume-dirty",
        "chkdsk-scan-dirty-volume",
        "chkdsk-repair-dirty-volume",
        "chkdsk-scan-after-repair",
    ):
        assert marker in script
    assert "sentinel hash changed during checker lifecycle" in script
    assert "windows-child-process-terminated-during-ntfs-write" in script
    assert "Stop-Process -Id $writer.Id -Force" in script
    assert "$rawCrash" in script
    assert "raw-crash-repair-comparison.json" in script


def test_windows_acceptance_does_not_claim_replay_parity():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "does not prove NTFS replay parity" in script
    assert "ntfsfix" not in script.lower()
    assert 'status = "blocked"' in script


def test_raw_snapshot_tools_are_bounded_and_byte_comparison_is_explicit():
    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    compare = COMPARE.read_text(encoding="utf-8")
    assert "NativeNtfsMetadataImage" in snapshot
    assert "WindowsRawDeviceImageSource" in snapshot
    assert "source_factory=source_factory" in snapshot
    assert '"MFT.bin"' in snapshot
    assert '"BITMAP.bin"' in snapshot
    assert "byte_identical" in compare
    assert "differing_bytes" in compare
    assert "not proof that replay is correct" in compare


def test_raw_snapshot_accepts_the_bounded_ci_volume_window():
    assert _validate_device_window(4096, 64 << 30) == (4096 + (64 << 30))
    assert _validate_sector_size(4096) == 4096


@pytest.mark.parametrize("sector_size", [True, 0, 256, 768, 1 << 21])
def test_raw_snapshot_rejects_invalid_sector_sizes(sector_size):
    with pytest.raises(NtfsMetadataError):
        _validate_sector_size(sector_size)


@pytest.mark.parametrize(
    ("offset", "volume_size"),
    [
        (-1, 512),
        (0, 0),
        (True, 512),
        (0, True),
        (0, (128 << 30) + 1),
        ((1 << 63) - 1, 1),
    ],
)
def test_raw_snapshot_rejects_invalid_or_unbounded_device_windows(offset, volume_size):
    with pytest.raises(NtfsMetadataError):
        _validate_device_window(offset, volume_size)
