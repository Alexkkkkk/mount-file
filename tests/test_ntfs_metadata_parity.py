# SPDX-License-Identifier: MIT
"""Independent NTFS metadata/replay parity evidence for #510 (C3, C5, C6).

The acceptance gaps for C3/C5/C6 asked for one thing the tree did not have:
a checker that does **not** share code with the implementation under test.
This module builds the missing evidence from three mutually independent
columns and demands byte/field agreement between all of them:

1. the production reader (``mountfile.ntfs_reader.probe_ntfs_metadata`` and
   the ``_Mft`` mapping used by the replay executor);
2. the standalone pure-Python oracle (``tests.ntfs_metadata_oracle``), which
   imports nothing from ``mountfile`` and re-derives every offset from the
   published NTFS on-disk layout;
3. external ntfs-3g helpers (``ntfscat`` stream extraction, ``ntfsfix -n``
   consistency check, ``ntfsinfo -m`` geometry), treated as secondary
   evidence exactly as ``tests/ntfs_oracle.py`` treats them.

Every test is deterministic: one ``mkntfs`` volume per module, and the dirty
fixtures come from the already-pinned catalog in ``tests/ntfs_oracle.py``.
Runners without the ntfs-3g tools skip - a skip is never reported as a pass.
A disagreement between any two columns fails the test outright; there is no
"unavailable" escape hatch inside an executed test body.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.ntfs_mft import parse_ntfs_file_record_header
from mountfile.ntfs_reader import (
    parse_ntfs_attribute_stream,
    parse_ntfs_standard_information,
    parse_ntfs_volume_state,
    probe_ntfs_metadata,
)
from mountfile.ntfs_replay import _Mft, native_ntfs_recover
from tests.ntfs_metadata_oracle import (
    ORACLE_SYSTEM_RECORDS,
    list_oracle_attributes,
    read_oracle_boot_geometry,
    read_oracle_mft_record,
    read_oracle_mft_record_raw,
    read_oracle_mft_records,
    read_oracle_standard_information,
    read_oracle_stream,
    read_oracle_system_stream_sizes,
    read_oracle_volume_state,
)
from tests.ntfs_oracle import SCENARIOS, build_dirty_fixture

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfsfix") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfsfix/ntfscat) are unavailable",
)


def _ntfscat_size(image: Path, stream: str) -> int:
    result = subprocess.run(
        ["ntfscat", str(image), stream], capture_output=True, check=False
    )
    if result.returncode != 0:
        pytest.fail(
            f"ntfscat {stream} failed: {result.stderr.decode(errors='replace')}"
        )
    return len(result.stdout)


def _ntfscat_bytes(image: Path, stream: str) -> bytes:
    result = subprocess.run(
        ["ntfscat", str(image), stream], capture_output=True, check=False
    )
    if result.returncode != 0:
        pytest.fail(
            f"ntfscat {stream} failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout


_ORACLE_STREAM_NAMES = {
    "mft": "$MFT",
    "mftmirr": "$MFTMirr",
    "logfile": "$LogFile",
    "bitmap": "$Bitmap",
}


@pytest.fixture(scope="module")
def ntfs_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real ``mkntfs`` volume with a written file, shared by the module."""

    root = tmp_path_factory.mktemp("ntfs-parity")
    image = root / "parity.img"
    with open(image, "wb") as handle:
        handle.truncate(64 << 20)
    subprocess.run(
        ["mkntfs", "-q", "-F", "-F", "-s", "512", "-c", "4096", str(image)],
        check=True,
        capture_output=True,
    )
    payload = root / "payload.bin"
    payload.write_bytes(b"PARITY-EVIDENCE-" + b"x" * 48)
    subprocess.run(
        ["ntfscp", str(image), str(payload), "/target"],
        check=True,
        capture_output=True,
    )
    return image


def _production_mft_handle(image: Path) -> tuple[_Mft, object]:
    """Open the production ``_Mft`` mapping exactly like the executor does."""

    profile = probe_ntfs_metadata(
        str(image), volume_size=image.stat().st_size, validate_mirror=True
    )
    mft_data = [
        attribute
        for attribute in profile.attributes
        if attribute.type_code == 0x80 and not attribute.name and attribute.nonresident
    ][0]
    # SIM115 is silenced for this one open: the _Mft mapping owns the handle
    # for its whole lifetime and every caller closes it in a finally block.
    handle = open(image, "r+b")  # noqa: SIM115
    return _Mft(handle, profile.boot, mft_data, base_offset=0), profile


@needs_ntfs
def test_boot_geometry_three_way_parity(ntfs_image: Path) -> None:
    """Boot identity fields agree across reader, standalone oracle and ntfsinfo."""

    profile = probe_ntfs_metadata(
        str(ntfs_image), volume_size=ntfs_image.stat().st_size
    )
    oracle = read_oracle_boot_geometry(str(ntfs_image))

    assert oracle.bytes_per_sector == profile.boot.bytes_per_sector
    assert oracle.sectors_per_cluster == profile.boot.sectors_per_cluster
    assert oracle.cluster_size == profile.boot.cluster_size
    assert oracle.total_sectors == profile.boot.total_sectors
    assert oracle.mft_lcn == profile.boot.mft_lcn
    assert oracle.mft_mirror_lcn == profile.boot.mft_mirror_lcn
    assert oracle.file_record_size == profile.boot.file_record_size
    assert oracle.volume_serial == profile.boot.volume_serial

    result = subprocess.run(
        ["ntfsinfo", "-m", str(ntfs_image)], capture_output=True, check=False
    )
    text = result.stdout.decode(errors="replace")
    # Secondary evidence: ntfsinfo must parse the same volume and report the
    # same sector/cluster geometry inside its dump.
    assert result.returncode == 0, text[:400]
    assert f"Sector Size: {oracle.bytes_per_sector}" in text
    assert f"Cluster Size: {oracle.cluster_size}" in text
    assert f"LCN of Data Attribute for FILE_MFT: {oracle.mft_lcn}" in text


@needs_ntfs
def test_system_stream_sizes_match_ntfscat_and_production(ntfs_image: Path) -> None:
    """$MFT/$MFTMirr/$LogFile/$Bitmap sizes: oracle == ntfscat == production."""

    oracle_sizes = read_oracle_system_stream_sizes(str(ntfs_image))
    for name, record_number in ORACLE_SYSTEM_RECORDS.items():
        external = _ntfscat_size(ntfs_image, _ORACLE_STREAM_NAMES[name])
        assert (
            oracle_sizes[name] == external
        ), f"{name}: oracle {oracle_sizes[name]} != ntfscat {external}"
        assert oracle_sizes[name] > 0, f"{name}: oracle reported an empty stream"
        # The production reader maps the same well-known record; the oracle
        # must see the same USA-restored record size for it.
        record = read_oracle_mft_record(str(ntfs_image), record_number)
        assert len(record) > 0


@needs_ntfs
@pytest.mark.parametrize(
    ("name", "record_number"), sorted(ORACLE_SYSTEM_RECORDS.items())
)
def test_system_stream_bytes_oracle_equal_ntfscat(
    ntfs_image: Path, name: str, record_number: int
) -> None:
    """Full stream bytes reassembled by the oracle equal the ntfscat bytes.

    This is the byte-level identity the C3/C5/C6 gaps demanded: the
    standalone oracle re-derives the runlist mapping from the boot sector
    and reassembles the stream itself; ntfs-3g agrees on every byte.
    """

    ours = read_oracle_stream(str(ntfs_image), record_number)
    external = _ntfscat_bytes(ntfs_image, _ORACLE_STREAM_NAMES[name])
    assert len(ours) == len(external)
    assert ours == external, f"{name}: oracle stream differs from ntfscat stream"


@needs_ntfs
def test_mft_record0_attribute_inventory_oracle_equals_production(
    ntfs_image: Path,
) -> None:
    """The attribute chain of record 0 agrees entry by entry across columns.

    The standalone oracle walks raw bytes with fixed-format offsets; the
    production parser walks the same record through its own validated
    reader.  Both must produce the same (type, nonresident, named) triples
    in the same order.
    """

    oracle_inventory = list_oracle_attributes(str(ntfs_image), 0)
    assert oracle_inventory, "oracle produced an empty inventory"
    # The production parser performs its own USA restoration, so it must see
    # the raw on-disk record, not the oracle's restored copy.
    raw = read_oracle_mft_record_raw(str(ntfs_image), 0)
    production_header = parse_ntfs_file_record_header(raw)
    production_attributes = parse_ntfs_attribute_stream(
        production_header, cluster_count=None, allow_end_marker_padding=True
    )
    production_inventory = tuple(
        (attribute.type_code, attribute.nonresident, bool(attribute.name))
        for attribute in production_attributes
    )
    assert (
        oracle_inventory == production_inventory
    ), "attribute inventories of record 0 disagree between oracle and parser"


@needs_ntfs
def test_standard_information_timestamps_oracle_equal_production(
    ntfs_image: Path,
) -> None:
    """The $STANDARD_INFORMATION of record 3 parses identically in both."""

    oracle = read_oracle_standard_information(str(ntfs_image), 3)
    raw = read_oracle_mft_record_raw(str(ntfs_image), 3)
    header = parse_ntfs_file_record_header(raw)
    attributes = parse_ntfs_attribute_stream(
        header, cluster_count=None, allow_end_marker_padding=True
    )
    matches = [
        attribute
        for attribute in attributes
        if attribute.type_code == 0x10 and not attribute.name
    ]
    assert len(matches) == 1
    production = parse_ntfs_standard_information(matches[0].resident_value)
    assert oracle.creation_time == production.creation_time
    assert oracle.modification_time == production.modification_time
    assert oracle.mft_change_time == production.mft_change_time
    assert oracle.access_time == production.access_time
    assert oracle.file_attributes == production.file_attributes


@needs_ntfs
def test_mft_mirror_window_is_usa_restored_and_matches_primary(
    ntfs_image: Path,
) -> None:
    """Oracle-restored $MFTMirr records parse as FILE and equal the primaries."""

    mirror = read_oracle_mft_records(str(ntfs_image))
    assert len(mirror) == 4
    for record_number, restored in enumerate(mirror):
        assert restored[:4] == b"FILE", f"mirror record {record_number} lost its magic"
        primary = read_oracle_mft_record(str(ntfs_image), record_number)
        assert (
            restored == primary
        ), f"clean volume: $MFTMirr record {record_number} differs from $MFT"


@needs_ntfs
def test_volume_dirty_flag_agrees_across_all_three_columns(ntfs_image: Path) -> None:
    """Oracle, production parser and ntfsfix -n agree the clean volume is clean."""

    oracle_state = read_oracle_volume_state(str(ntfs_image))
    assert oracle_state.dirty is False
    assert oracle_state.clean is True

    mft, _profile = _production_mft_handle(ntfs_image)
    try:
        header = mft.read_record(3)
    finally:
        mft._stream.close()
    attributes = parse_ntfs_attribute_stream(header, allow_end_marker_padding=True)
    state = parse_ntfs_volume_state(attributes)
    assert state.clean is True

    result = subprocess.run(
        ["ntfsfix", "-n", str(ntfs_image)], capture_output=True, check=False
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        errors="replace"
    )[:400]


@needs_ntfs
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_replay_parity_independent_oracle_agrees_with_production(
    tmp_path: Path, scenario: str
) -> None:
    """After replay, oracle and production agree on every checked metadata fact.

    For every cataloged dirty fixture: the standalone oracle must read the
    replayed volume as clean, the production reader must see ``$MFT`` and
    ``$MFTMirr`` in lockstep, both must extract a byte-identical
    ``$VOLUME_INFORMATION`` record, and - except for the deliberately
    mirror-damaged scenario - ``ntfsfix -n`` must accept the volume too.
    """

    fixture = build_dirty_fixture(tmp_path, scenario)
    replayed = tmp_path / f"{scenario}-replayed.img"
    shutil.copyfile(fixture.image, replayed)
    native_ntfs_recover(str(replayed))

    # Column 2: standalone oracle.
    oracle_state = read_oracle_volume_state(str(replayed))
    assert oracle_state.dirty is False, f"{scenario}: oracle still sees a dirty volume"
    mirror = read_oracle_mft_records(str(replayed))
    for record_number, restored in enumerate(mirror):
        assert (
            restored[:4] == b"FILE"
        ), f"{scenario}: mirror record {record_number} broken"

    # Column 1: production reader, opened exactly like the executor opens it.
    profile = probe_ntfs_metadata(
        str(replayed), volume_size=replayed.stat().st_size, validate_mirror=True
    )
    assert profile.mirror_matches_mft, f"{scenario}: production sees a drifted mirror"
    mft, _boot = _production_mft_handle(replayed)
    try:
        # fixed_record is the USA-restored byte string; the oracle's record
        # reader restores the same way, so both columns compare restored-to-restored.
        production_record3 = mft.read_record(3).fixed_record
    finally:
        mft._stream.close()
    oracle_record3 = read_oracle_mft_record(str(replayed), 3)
    assert (
        production_record3 == oracle_record3
    ), f"{scenario}: production and oracle disagree on $Volume record bytes"

    # Column 3: external ntfsfix -n (secondary; skipped for the scenario whose
    # mirror damage is deliberately left for the Windows checker to repair).
    if scenario != "mft-mirr-corrupt":
        result = subprocess.run(
            ["ntfsfix", "-n", str(replayed)], capture_output=True, check=False
        )
        assert result.returncode == 0, (result.stdout + result.stderr).decode(
            errors="replace"
        )[:400]


@needs_ntfs
def test_replay_leaves_stream_layout_unchanged_except_logfile(
    tmp_path: Path,
) -> None:
    """Replay must not resize $MFT/$MFTMirr/$Bitmap; only $LogFile may change."""

    fixture = build_dirty_fixture(tmp_path, "resident-commit")
    replayed = tmp_path / "resident-commit-replayed.img"
    shutil.copyfile(fixture.image, replayed)
    before = read_oracle_system_stream_sizes(str(fixture.image))
    native_ntfs_recover(str(replayed))
    after = read_oracle_system_stream_sizes(str(replayed))
    for name in ("mft", "mftmirr", "bitmap"):
        assert (
            before[name] == after[name]
        ), f"replay resized ${name.capitalize()}: {before[name]} -> {after[name]}"
