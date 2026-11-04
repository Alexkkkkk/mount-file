# SPDX-License-Identifier: MIT

import shutil
import subprocess

import pytest

from mountfile.ext2_reader import Ext2ReadError
from mountfile.ext3_reader import NativeExt4Image
from mountfile.ext_checksum import crc32c
from tests.test_ext2_reader import BLOCKS_COUNT, _make_image


def test_crc32c_castagnoli_known_vector():
    assert crc32c(b"123456789") == 0xE3069283


def test_crc32c_seed_chaining_is_deterministic():
    whole = crc32c(b"abcdef")
    chained = crc32c(b"def", crc32c(b"abc"))
    assert chained == whole


def test_ext4_metadata_checksum_profile_is_not_silently_downgraded(tmp_path):
    image_path = tmp_path / "checksum-ext4.img"
    _make_image(image_path, incompat=0x0042, ro_compat=0x0403, compat=0x0008)
    with pytest.raises(Ext2ReadError, match="checksum|unsupported"):
        NativeExt4Image(image_path, volume_size=BLOCKS_COUNT * 1024)


def test_real_ext4_metadata_checksums_support_bounded_directory_reads(tmp_path):
    mke2fs = shutil.which("mke2fs")
    if mke2fs is None:
        pytest.skip("mke2fs is not installed")
    image_path = tmp_path / "real-checksum-ext4.img"
    with image_path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    subprocess.run(
        [
            mke2fs,
            "-q",
            "-F",
            "-t",
            "ext4",
            "-b",
            "1024",
            "-O",
            "extent,^has_journal,^64bit,metadata_csum,^metadata_csum_seed,^dir_index,^flex_bg,^huge_file",
            str(image_path),
        ],
        check=True,
    )
    provider = NativeExt4Image(image_path, volume_size=image_path.stat().st_size)
    try:
        assert any(entry["name"] == "lost+found" for entry in provider.list_dir("/"))
    finally:
        provider.close()
