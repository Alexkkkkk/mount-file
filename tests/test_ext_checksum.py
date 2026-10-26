# SPDX-License-Identifier: MIT

import pytest

from mountfile.ext3_reader import Ext3ReadError, NativeExt4Image
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
    with pytest.raises(Ext3ReadError, match="checksum|unsupported"):
        NativeExt4Image(image_path, volume_size=BLOCKS_COUNT * 1024)
