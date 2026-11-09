# SPDX-License-Identifier: MIT
"""Smoke the shared bounded read contract across native filesystem providers."""

import pytest

from mountfile import native_parity
from mountfile.native_parity import ParityCase, run_parity_matrix
from tests.native_parity_fixtures import build_native_reader_cases


def test_native_read_contract_across_supported_filesystem_profiles(tmp_path):
    cases = build_native_reader_cases(tmp_path)

    report = run_parity_matrix(cases)

    assert report.ok, report.failures
    assert report.ran == [case.fs_name for case in cases]
    assert report.checks == len(cases) * 5
    assert len(report.cases) == 8
    assert {case["profile_id"] for case in report.cases} == {
        "fat12-native-ro",
        "fat16-native-ro",
        "fat32-native-ro",
        "exfat-native-ro",
        "ext2-native-ro",
        "ext3-native-ro",
        "ext4-native-ro",
        "ntfs-native-ro",
    }
    for case in report.cases:
        assert case["native_status"] == "pass"
        assert case["independent_oracle"]["status"] == "not_claimed"
        assert case["fixture"]["sha256"] == case["fixture"]["sha256_after"]
        assert case["fixture"]["source_unchanged"] is True
        assert set(case["operations"].values()) == {"pass"}


def test_parity_matrix_rejects_empty_input():
    with pytest.raises(ValueError, match="at least one case"):
        run_parity_matrix([])


def test_parity_matrix_rejects_duplicate_names_before_opening_images(
    tmp_path, monkeypatch
):
    image = tmp_path / "placeholder.img"
    image.write_bytes(b"x")
    case = ParityCase(
        fs_name="duplicate",
        image_path=image,
        expected_files={"/file": b"x"},
        volume_size=1,
    )
    opened = []

    def record_open(*args, **kwargs):
        opened.append((args, kwargs))

    monkeypatch.setattr(native_parity.vfs, "open_image_provider", record_open)

    with pytest.raises(ValueError, match="non-empty and unique"):
        run_parity_matrix([case, case])

    assert opened == []


def test_parity_matrix_records_out_of_bounds_extent_without_opening_provider(
    tmp_path, monkeypatch
):
    image = tmp_path / "short.img"
    image.write_bytes(b"x")

    def fail_open(*args, **kwargs):
        pytest.fail("invalid extent reached the VFS provider")

    monkeypatch.setattr(native_parity.vfs, "open_image_provider", fail_open)
    report = run_parity_matrix(
        [
            ParityCase(
                fs_name="out-of-bounds",
                image_path=image,
                expected_files={"/file": b"x"},
                volume_size=2,
            )
        ]
    )

    assert not report.ok
    assert not report.ran
    assert "filesystem extent is invalid" in report.failures["out-of-bounds"]
