"""Issue #390 status-drift gate: README rows must match the runtime matrix.

The README may only claim native write/replay support for a filesystem once
the runtime capability matrix publishes write capabilities for it.  Flipping
one side alone (docs without runtime, or runtime without docs) fails this
gate, so the product status can no longer silently drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mountfile.capabilities import get_native_capability_matrix

README = Path(__file__).resolve().parents[1] / "README.md"

# README native-support table row -> profiles whose write capabilities count.
ROWS = {
    "ext3/ext4": ("ext3", "ext4"),
    "NTFS": ("ntfs",),
}


def _readme_write_claims() -> dict[str, bool]:
    claims: dict[str, bool] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        for row in ROWS:
            if line.startswith(f"| {row} "):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                claims[row] = cells[2].lower() not in {"no", "—", "-", ""}
    return claims


def _runtime_write_flags() -> dict[str, bool]:
    matrix = get_native_capability_matrix()
    flags: dict[str, bool] = {}
    for profile in matrix["profiles"]:
        if isinstance(profile, dict):
            fs = profile.get("filesystem")
            writes = profile.get("write_capabilities") or ()
        else:
            fs = profile.filesystem
            writes = profile.write_capabilities
        flags[fs] = flags.get(fs, False) or bool(writes)
    return flags


def test_readme_write_status_matches_runtime_capability_matrix():
    claims = _readme_write_claims()
    assert set(claims) == set(ROWS), "README native-support table rows went missing"
    runtime = _runtime_write_flags()
    for row, fs_names in ROWS.items():
        supported = any(runtime.get(fs, False) for fs in fs_names)
        if claims[row] and not supported:
            pytest.fail(
                f"README claims native write/replay support for {row}, but the "
                "runtime capability matrix publishes no write capabilities for "
                f"{fs_names} — flip mountfile/capabilities.py first (issue #390)"
            )
        if not claims[row] and supported:
            pytest.fail(
                f"runtime capability matrix exposes write capabilities for {row} "
                f"({fs_names}) while README still says No — synchronize the "
                "table (issue #390)"
            )
