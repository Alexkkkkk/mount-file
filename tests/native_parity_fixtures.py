# SPDX-License-Identifier: MIT
"""Portable filesystem images used by native read-parity evidence."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

from mountfile.fat32 import Fat32
from mountfile.native_parity import ParityCase
from tests.test_exfat_probe import VOLUME_SIZE as EXFAT_VOLUME_SIZE
from tests.test_exfat_probe import _tree_image
from tests.test_ext2_reader import _make_image as make_ext2_image
from tests.test_ext3_reader import _make_clean_journal_image, _make_ext4_extent_image
from tests.test_fat32 import make_fat32_image
from tests.test_fat_reader import _make_small_fat

NTFS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ntfs-readonly-32m.img.gz"
NTFS_FIXTURE_SHA256 = "bb66068f8e0fde7c4d7fb31c14d965c6a82057a5c65f85ce13818ad438658bbc"
NTFS_IMAGE_SHA256 = "42a2fca7986e78ca0b2b9cc69250249386001e1bbd939c54649aa9fb9f465087"
NTFS_IMAGE_SIZE_BYTES = 32 * 1024 * 1024
NTFS_PAYLOAD = b"mount-file portable NTFS fixture\n"


def _volume_size(image: Path) -> int:
    return image.stat().st_size


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_native_reader_cases(tmp_path: Path) -> list[ParityCase]:
    """Build bounded read fixtures for every current native VFS profile.

    Seven profiles use compact synthetic images assembled by the existing
    Python test builders. NTFS uses a compressed, real-format image so the
    native NTFS path is exercised on Windows and macOS without platform tools.
    """
    cases: list[ParityCase] = []

    for fat_type in (12, 16):
        image = tmp_path / f"fat{fat_type}.img"
        payload = _make_small_fat(image, fat_type)
        cases.append(
            ParityCase(
                fs_name=f"fat{fat_type}",
                profile_id=f"fat{fat_type}-native-ro",
                image_path=image,
                volume_size=_volume_size(image),
                expected_files={"/HELLO.TXT": payload},
                fixture_source="tests.test_fat_reader::_make_small_fat",
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
            profile_id="fat32-native-ro",
            image_path=fat32_image,
            volume_size=_volume_size(fat32_image),
            expected_files={"/A long native name.txt": fat32_payload},
            fixture_source="tests.test_fat32::make_fat32_image plus Fat32.write_file",
        )
    )

    exfat_image = tmp_path / "exfat.img"
    exfat_payload = _tree_image(exfat_image)
    cases.append(
        ParityCase(
            fs_name="exfat",
            profile_id="exfat-native-ro",
            image_path=exfat_image,
            volume_size=EXFAT_VOLUME_SIZE,
            expected_files={"/HELLO.TXT": exfat_payload},
            fixture_source="tests.test_exfat_probe::_tree_image",
        )
    )

    ext2_image = tmp_path / "ext2.img"
    make_ext2_image(ext2_image)
    cases.append(
        ParityCase(
            fs_name="ext2",
            profile_id="ext2-native-ro",
            image_path=ext2_image,
            volume_size=_volume_size(ext2_image),
            expected_files={"/hello.txt": b"hello ext2!"},
            fixture_source="tests.test_ext2_reader::_make_image",
        )
    )

    ext3_image = tmp_path / "ext3.img"
    _make_clean_journal_image(ext3_image)
    cases.append(
        ParityCase(
            fs_name="ext3-clean-journal",
            profile_id="ext3-native-ro",
            image_path=ext3_image,
            volume_size=_volume_size(ext3_image),
            expected_files={"/hello.txt": b"hello ext2!"},
            fixture_source="tests.test_ext3_reader::_make_clean_journal_image",
        )
    )

    ext4_image = tmp_path / "ext4.img"
    _make_ext4_extent_image(ext4_image)
    cases.append(
        ParityCase(
            fs_name="ext4-clean-extents",
            profile_id="ext4-native-ro",
            image_path=ext4_image,
            volume_size=_volume_size(ext4_image),
            expected_files={"/hello.txt": b"hello ext4!"},
            fixture_source="tests.test_ext3_reader::_make_ext4_extent_image",
        )
    )

    compressed_sha256 = _file_sha256(NTFS_FIXTURE_PATH)
    if compressed_sha256 != NTFS_FIXTURE_SHA256:
        raise ValueError("pinned NTFS compressed fixture SHA-256 mismatch")
    ntfs_image = tmp_path / "ntfs.img"
    image_bytes = 0
    with gzip.open(NTFS_FIXTURE_PATH, "rb") as source, ntfs_image.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            image_bytes += len(chunk)
            if image_bytes > NTFS_IMAGE_SIZE_BYTES:
                raise ValueError("NTFS fixture expands beyond its declared image size")
            target.write(chunk)
    if image_bytes != NTFS_IMAGE_SIZE_BYTES:
        raise ValueError("NTFS fixture expands to an unexpected image size")
    cases.append(
        ParityCase(
            fs_name="ntfs-prebuilt-real",
            profile_id="ntfs-native-ro",
            image_path=ntfs_image,
            volume_size=_volume_size(ntfs_image),
            expected_files={"/HELLO.TXT": NTFS_PAYLOAD},
            fixture_kind="real-prebuilt",
            fixture_source="tests/fixtures/ntfs-readonly-32m.img.gz",
            source_fixture_sha256=compressed_sha256,
            expected_image_sha256=NTFS_IMAGE_SHA256,
        )
    )

    return cases


__all__ = [
    "NTFS_FIXTURE_PATH",
    "NTFS_FIXTURE_SHA256",
    "NTFS_IMAGE_SHA256",
    "NTFS_IMAGE_SIZE_BYTES",
    "NTFS_PAYLOAD",
    "build_native_reader_cases",
]
