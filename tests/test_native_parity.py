# SPDX-License-Identifier: MIT
"""Smoke the shared bounded read contract across native filesystem providers."""

from pathlib import Path

import pytest

from mountfile import native_parity
from mountfile.fat32 import Fat32
from mountfile.native_parity import ParityCase, run_parity_matrix
from tests.test_exfat_probe import VOLUME_SIZE as EXFAT_VOLUME_SIZE
from tests.test_exfat_probe import _tree_image
from tests.test_ext2_reader import _make_image as make_ext2_image
from tests.test_ext3_reader import (
    _make_clean_journal_image,
    _make_ext4_extent_image,
)
from tests.test_fat32 import make_fat32_image
from tests.test_fat_reader import _make_small_fat


def _volume_size(image: Path) -> int:
    return image.stat().st_size


def test_native_read_contract_across_supported_filesystem_profiles(tmp_path):
    cases = []

    for fat_type in (12, 16):
        image = tmp_path / f"fat{fat_type}.img"
        payload = _make_small_fat(image, fat_type)
        cases.append(
            ParityCase(
                fs_name=f"fat{fat_type}",
                image_path=image,
                volume_size=_volume_size(image),
                expected_files={"/HELLO.TXT": payload},
            )
        )

    fat32_image = tmp_path / "fat32.img"
    make_fat32_image(fat32_image)
    fat32_payload = b"native FAT32 parity\n"
    with fat32_image.open("r+b") as stream:
        Fat32(stream).write_file("/", "A long native name.txt", fat32_payload)
    cases.append(
        ParityCase(
            fs_name="fat32",
            image_path=fat32_image,
            volume_size=_volume_size(fat32_image),
            expected_files={"/A long native name.txt": fat32_payload},
        )
    )

    exfat_image = tmp_path / "exfat.img"
    exfat_payload = _tree_image(exfat_image)
    cases.append(
        ParityCase(
            fs_name="exfat",
            image_path=exfat_image,
            volume_size=EXFAT_VOLUME_SIZE,
            expected_files={"/HELLO.TXT": exfat_payload},
        )
    )

    ext2_image = tmp_path / "ext2.img"
    make_ext2_image(ext2_image)
    cases.append(
        ParityCase(
            fs_name="ext2",
            image_path=ext2_image,
            volume_size=_volume_size(ext2_image),
            expected_files={"/hello.txt": b"hello ext2!"},
        )
    )

    ext3_image = tmp_path / "ext3.img"
    _make_clean_journal_image(ext3_image)
    cases.append(
        ParityCase(
            fs_name="ext3-clean-journal",
            image_path=ext3_image,
            volume_size=_volume_size(ext3_image),
            expected_files={"/hello.txt": b"hello ext2!"},
        )
    )

    ext4_image = tmp_path / "ext4.img"
    _make_ext4_extent_image(ext4_image)
    cases.append(
        ParityCase(
            fs_name="ext4-clean-extents",
            image_path=ext4_image,
            volume_size=_volume_size(ext4_image),
            expected_files={"/hello.txt": b"hello ext4!"},
        )
    )

    report = run_parity_matrix(cases)

    assert report.ok, report.failures
    assert report.ran == [case.fs_name for case in cases]
    assert report.checks == len(cases) * 5


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
