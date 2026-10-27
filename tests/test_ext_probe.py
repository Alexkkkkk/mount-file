# SPDX-License-Identifier: MIT
"""Tests for the dependency-free, read-only ext superblock profile probe."""

import io
import struct

import pytest

from mountfile.ext_checksum import ext4_superblock_checksum
from mountfile.ext_probe import ExtProbeError, probe_ext_superblock


def _image(
    *,
    block_size=4096,
    blocks=16,
    state=1,
    compat=0x0008,
    incompat=0x0002,
    ro_compat=0x0003,
    blocks_high=0,
):
    image_size = block_size * blocks
    image = bytearray(image_size)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 0x00, 16)  # inode count
    struct.pack_into("<I", sb, 0x04, blocks)
    struct.pack_into("<I", sb, 0x14, 1 if block_size == 1024 else 0)
    struct.pack_into("<I", sb, 0x18, block_size.bit_length() - 11)
    struct.pack_into("<I", sb, 0x20, 8)
    struct.pack_into("<I", sb, 0x28, 16)
    struct.pack_into("<H", sb, 0x38, 0xEF53)
    struct.pack_into("<H", sb, 0x3A, state)
    struct.pack_into("<I", sb, 0x4C, 1)
    struct.pack_into("<H", sb, 0x58, 128)
    struct.pack_into("<I", sb, 0x5C, compat)
    struct.pack_into("<I", sb, 0x60, incompat)
    struct.pack_into("<I", sb, 0x64, ro_compat)
    struct.pack_into("<I", sb, 0x150, blocks_high)
    sb[0x78:0x7C] = b"TEST"
    return bytes(image)


def test_clean_allowlisted_ext2_is_only_a_candidate_for_full_writer_validation():
    profile = probe_ext_superblock(io.BytesIO(_image()))

    assert profile.fs_type == "ext2"
    assert profile.clean_unmounted
    assert profile.phase1_ext2_profile_candidate
    assert profile.write_gate_reasons == ()
    assert profile.label == "TEST"


def test_journal_and_recovery_flags_keep_profile_read_only():
    image = _image(compat=0x000C, incompat=0x0006)
    profile = probe_ext_superblock(io.BytesIO(image))

    assert profile.fs_type == "ext3"
    assert profile.has_journal
    assert profile.journal_recovery_needed
    assert not profile.clean_unmounted
    assert not profile.phase1_ext2_profile_candidate
    assert any("JBD2" in reason for reason in profile.write_gate_reasons)
    assert any("recovery" in reason for reason in profile.write_gate_reasons)


def test_external_journal_device_is_not_misclassified_as_unjournaled_ext2():
    profile = probe_ext_superblock(
        io.BytesIO(_image(compat=0x0008, incompat=0x0002 | 0x0008))
    )

    assert profile.fs_type == "ext3"
    assert profile.has_journal
    assert not profile.phase1_ext2_profile_candidate
    assert any("JBD2" in reason for reason in profile.write_gate_reasons)


def test_ext4_features_are_not_mistaken_for_ext2_block_pointers():
    profile = probe_ext_superblock(io.BytesIO(_image(incompat=0x0002 | 0x0040)))

    assert profile.fs_type == "ext4"
    assert not profile.phase1_ext2_profile_candidate
    assert any("ext4-featured" in reason for reason in profile.write_gate_reasons)


def test_metadata_checksum_profile_requires_a_valid_superblock_crc32c():
    image = bytearray(_image(ro_compat=0x0403))
    superblock = memoryview(image)[1024:2048]
    superblock[0x175] = 1
    checksum = ext4_superblock_checksum(bytes(superblock))
    struct.pack_into("<I", superblock, 0x3FC, checksum)

    profile = probe_ext_superblock(io.BytesIO(image))
    assert profile.fs_type == "ext4"

    image[1024 + 0x80] ^= 0x01
    with pytest.raises(ExtProbeError, match="checksum mismatch"):
        probe_ext_superblock(io.BytesIO(image))


def test_metadata_checksum_profile_exposes_no_write_eligibility_for_unknown_layout():
    image = bytearray(_image(ro_compat=0x0403))
    superblock = memoryview(image)[1024:2048]
    superblock[0x175] = 1
    struct.pack_into(
        "<I", superblock, 0x3FC, ext4_superblock_checksum(bytes(superblock))
    )

    profile = probe_ext_superblock(io.BytesIO(image))
    assert not profile.phase1_ext2_profile_candidate
    assert any("ext4-featured" in reason for reason in profile.write_gate_reasons)


def test_probe_rejects_checked_filesystem_size_arithmetic():
    image = bytearray(_image())
    struct.pack_into("<I", memoryview(image)[1024:2048], 0x04, 0xFFFFFFFF)
    with pytest.raises(ExtProbeError, match="partition bounds"):
        probe_ext_superblock(io.BytesIO(image))


def test_dirty_state_and_unknown_incompat_flags_fail_closed():
    profile = probe_ext_superblock(
        io.BytesIO(_image(state=0x0003, incompat=0x40000002))
    )

    assert not profile.clean_unmounted
    assert not profile.phase1_ext2_profile_candidate
    assert profile.unknown_incompat_features == 0x40000000
    assert any(
        "unknown incompatible" in reason for reason in profile.write_gate_reasons
    )


def test_unknown_read_only_compatible_flags_are_reported():
    profile = probe_ext_superblock(io.BytesIO(_image(ro_compat=0x80000003)))

    assert profile.unknown_ro_compat_features == 0x80000000
    assert not profile.phase1_ext2_profile_candidate
    assert any(
        "unknown read-only-compatible" in reason
        for reason in profile.write_gate_reasons
    )


def test_64bit_block_count_is_used_for_extent_bounds():
    image = _image(incompat=0x0002 | 0x0080, blocks_high=1)

    with pytest.raises(ExtProbeError, match="partition bounds"):
        probe_ext_superblock(io.BytesIO(image))


def test_partition_offset_is_applied_and_sentinels_are_not_touched():
    payload = _image()
    prefix = b"prefix" * 23
    suffix = b"next-partition"
    image = io.BytesIO(prefix + payload + suffix)
    profile = probe_ext_superblock(image, offset=len(prefix), volume_size=len(payload))

    assert profile.offset == len(prefix)
    assert profile.phase1_ext2_profile_candidate
    assert image.getvalue().startswith(prefix)
    assert image.getvalue().endswith(suffix)


def test_probe_rejects_extents_outside_image_or_volume_bounds():
    image = io.BytesIO(_image())

    with pytest.raises(ExtProbeError, match="partition bounds"):
        probe_ext_superblock(image, volume_size=4096)
    with pytest.raises(ExtProbeError, match="outside the image"):
        probe_ext_superblock(image, offset=len(_image()) + 1)


def test_probe_rejects_bad_signature_and_short_images():
    with pytest.raises(ExtProbeError, match="no ext superblock"):
        probe_ext_superblock(io.BytesIO(bytes(4096)))
    with pytest.raises(ExtProbeError, match="too small"):
        probe_ext_superblock(io.BytesIO(bytes(1024)))


def test_vfs_exports_probe_without_requiring_pytsk3(tmp_path, monkeypatch):
    from mountfile import vfs

    image_path = tmp_path / "ext2.img"
    image_path.write_bytes(_image())
    monkeypatch.setattr(vfs, "pytsk3", None)

    profile = vfs.probe_ext_profile(image_path)

    assert profile.fs_type == "ext2"
    assert profile.phase1_ext2_profile_candidate
