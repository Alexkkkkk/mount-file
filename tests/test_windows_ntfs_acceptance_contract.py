# SPDX-License-Identifier: MIT
"""Static contract tests for the real Windows NTFS acceptance workflow."""

from pathlib import Path

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
    assert "chkdsk" in script
    assert "diskpart.exe" in script
    assert "create vdisk" in script
    assert "format fs=ntfs" in script
    assert "windows_ntfs_raw_snapshot.py" in script
    assert "compare_ntfs_snapshots.py" in script


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


def test_windows_acceptance_does_not_claim_replay_parity():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "does not prove NTFS replay parity" in script
    assert "ntfsfix" not in script.lower()
    assert 'status = "blocked"' in script


def test_raw_snapshot_tools_are_bounded_and_byte_comparison_is_explicit():
    snapshot = SNAPSHOT.read_text(encoding="utf-8")
    compare = COMPARE.read_text(encoding="utf-8")
    assert "NativeNtfsMetadataImage" in snapshot
    assert '"MFT.bin"' in snapshot
    assert '"BITMAP.bin"' in snapshot
    assert "byte_identical" in compare
    assert "differing_bytes" in compare
    assert "not proof that replay is correct" in compare
