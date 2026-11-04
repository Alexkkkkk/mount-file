# SPDX-License-Identifier: MIT
"""Fail-closed tests for the read-only per-filesystem preflight report."""

import os
import struct

import pytest

from mountfile.capabilities import get_native_capability_matrix
from mountfile.ext_probe import probe_ext_superblock  # noqa: F401  (shared fixture use)
from mountfile.fs_preflight import (
    PREFLIGHT_SCHEMA_VERSION,
    GATE_FAIL,
    GATE_PASS,
    preflight_filesystem,
    preflight_image,
)
from mountfile.safety import RefusalCode, RefusalError
from mountfile.vfs import legacy_tsk_enabled

# --- synthetic fixtures ---------------------------------------------------


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    offset = (cluster * 3) // 2
    current = struct.unpack_from("<H", fat, offset)[0]
    if cluster & 1:
        current = (current & 0x000F) | ((value & 0x0FFF) << 4)
    else:
        current = (current & 0xF000) | (value & 0x0FFF)
    struct.pack_into("<H", fat, offset, current)


def _make_fat12(path) -> bytes:
    bps, total_sectors, reserved, fat_sectors, root_entries = 512, 3000, 1, 10, 224
    root_sectors = (root_entries * 32 + bps - 1) // bps
    with path.open("wb") as stream:
        stream.truncate(total_sectors * bps)
        boot = bytearray(bps)
        boot[:3] = b"\xeb\x3c\x90"
        boot[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", boot, 11, bps)
        boot[13] = 1
        struct.pack_into("<H", boot, 14, reserved)
        boot[16] = 1
        struct.pack_into("<H", boot, 17, root_entries)
        struct.pack_into("<H", boot, 19, total_sectors)
        boot[21] = 0xF8
        struct.pack_into("<H", boot, 22, fat_sectors)
        boot[510:512] = b"\x55\xaa"
        boot[54:62] = b"FAT12   "
        stream.write(boot)
        fat = bytearray(fat_sectors * bps)
        _set_fat12_entry(fat, 0, 0xFF8)
        _set_fat12_entry(fat, 1, 0xFFF)
        _set_fat12_entry(fat, 2, 0xFFF)
        stream.seek(reserved * bps)
        stream.write(fat)
        entry = bytearray(32)
        entry[:11] = b"HELLO   TXT"
        entry[11] = 0x20
        struct.pack_into("<H", entry, 26, 2)
        payload = b"preflight fixture\n"
        struct.pack_into("<I", entry, 28, len(payload))
        stream.seek((reserved + fat_sectors) * bps)
        stream.write(entry)
        stream.seek((reserved + fat_sectors + root_sectors) * bps)
        stream.write(payload.ljust(bps, b"\x00"))
    return payload


def _ext_image(*, state=1, compat=0x0008, incompat=0x0002, ro_compat=0x0003) -> bytes:
    block_size, blocks = 4096, 16
    image = bytearray(block_size * blocks)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 0x00, 16)
    struct.pack_into("<I", sb, 0x04, blocks)
    struct.pack_into("<I", sb, 0x14, 0)
    struct.pack_into("<I", sb, 0x18, block_size.bit_length() - 11)
    struct.pack_into("<I", sb, 0x20, 8)
    struct.pack_into("<I", sb, 0x28, 16)
    struct.pack_into("<H", sb, 0x38, 0xEF53)
    struct.pack_into("<H", sb, 0x3A, state)
    struct.pack_into("<I", sb, 0x4C, 1)
    struct.pack_into("<I", sb, 0x48, 0)
    struct.pack_into("<H", sb, 0x58, 128)
    struct.pack_into("<I", sb, 0x5C, compat)
    struct.pack_into("<I", sb, 0x60, incompat)
    struct.pack_into("<I", sb, 0x64, ro_compat)
    struct.pack_into("<I", sb, 0xE0, 8)
    sb[0x78:0x7C] = b"TEST"
    return bytes(image)


def _image_hash(path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gate_states(report):
    return {gate.gate_id: gate.state for gate in report.gates}


# --- fail-closed checks ---------------------------------------------------


def test_fat12_preflight_reports_read_eligibility_without_writes(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)
    report = preflight_filesystem(image)

    assert report.filesystem == "FAT12"
    assert report.eligible_for_read is True
    assert report.eligible_for_write is False
    assert report.write_enabled is False
    assert report.write_capabilities == []
    assert report.backend == "native-fat12-ro"
    assert report.profile_id == "fat12-native-ro"
    assert report.native_write_profile == "fat12-16-native-rw"
    assert report.write_refusal["code"] == RefusalCode.OPERATION_NOT_SUPPORTED.value
    assert report.operations["list"]["state"] == "available"
    assert report.operations["write-file"]["state"] == "refused"


def test_preflight_never_modifies_the_image(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)
    before = _image_hash(image)

    preflight_filesystem(image)
    preflight_image(image)

    assert _image_hash(image) == before


def test_missing_image_is_refused_without_raising(tmp_path):
    report = preflight_filesystem(tmp_path / "absent.img")

    assert report.eligible_for_read is False
    assert report.refusal["code"] == RefusalCode.UNSAFE.value
    assert _gate_states(report)["image-readable"] == GATE_FAIL


def test_offset_outside_image_is_a_bounded_range_refusal(tmp_path):
    image = tmp_path / "small.img"
    image.write_bytes(b"\x00" * 1024)

    report = preflight_filesystem(image, offset=4096)

    assert report.eligible_for_read is False
    assert report.refusal["code"] == RefusalCode.RANGE_OUT_OF_BOUNDS.value


def test_extent_larger_than_image_is_refused(tmp_path):
    image = tmp_path / "small.img"
    image.write_bytes(b"\x00" * 4096)

    report = preflight_filesystem(image, offset=0, volume_size=8192)

    assert report.eligible_for_read is False
    assert _gate_states(report)["bounded-extent"] == GATE_FAIL
    assert report.refusal["code"] == RefusalCode.RANGE_OUT_OF_BOUNDS.value


def test_unrecognized_signature_is_refused_and_never_claims_a_backend(tmp_path):
    image = tmp_path / "blank.img"
    image.write_bytes(b"\x00" * (256 * 1024))

    report = preflight_filesystem(image)

    assert report.filesystem == "unknown"
    assert report.detection == "none"
    assert report.eligible_for_read is False
    assert report.backend is None
    assert report.refusal["code"] == RefusalCode.UNSUPPORTED_PROFILE.value
    assert report.notes, "the refusal must explain the fallback policy"


def test_preflight_note_matches_the_active_legacy_fallback_policy(tmp_path):
    image = tmp_path / "blank.img"
    image.write_bytes(b"\x00" * (256 * 1024))

    report = preflight_filesystem(image)
    note = " ".join(report.notes)

    if legacy_tsk_enabled():
        assert "enabled by environment" in note
    else:
        assert "fallback is disabled" in note


def test_nonzero_offset_without_extent_is_unknown_not_eligible(tmp_path):
    image = tmp_path / "big.img"
    image.write_bytes(b"\x00" * (4 * 1024 * 1024))

    report = preflight_filesystem(image, offset=1024 * 1024)

    assert report.volume_size is None
    assert report.eligible_for_read is False
    assert _gate_states(report)["bounded-extent"] == "unknown"
    assert report.filesystem == "unknown"


def test_clean_ext2_profile_is_eligible_read_only(tmp_path):
    image = tmp_path / "ext2.img"
    payload = _ext_image()
    image.write_bytes(payload)

    report = preflight_filesystem(image, offset=0, volume_size=len(payload))

    assert report.filesystem == "ext2"
    assert report.profile_id == "ext2-native-ro"
    assert report.eligible_for_read is True
    assert report.native_write_profile == "ext2-native-rw"
    assert report.eligible_for_write is False
    states = _gate_states(report)
    assert states["clean-volume"] == GATE_PASS
    assert states["ext2-allowlist-profile"] == GATE_PASS


def test_dirty_ext_volume_is_refused_as_recovery_required(tmp_path):
    image = tmp_path / "ext2-dirty.img"
    payload = _ext_image(state=0)
    image.write_bytes(payload)

    report = preflight_filesystem(image, offset=0, volume_size=len(payload))

    assert report.eligible_for_read is False
    assert _gate_states(report)["clean-volume"] == GATE_FAIL
    assert report.refusal["code"] == RefusalCode.DIRTY_OR_RECOVERY_REQUIRED.value


def test_ext3_with_pending_journal_replay_is_refused(tmp_path):
    image = tmp_path / "ext3.img"
    payload = _ext_image(compat=0x000C, incompat=0x0006, state=0)
    image.write_bytes(payload)

    report = preflight_filesystem(image, offset=0, volume_size=len(payload))

    assert report.filesystem == "ext3"
    assert report.eligible_for_read is False
    assert report.native_write_profile is None
    assert _gate_states(report)["jbd2-no-replay"] == GATE_FAIL


def test_ext3_and_ext4_publish_no_native_write_profile(tmp_path):
    for name, kwargs in (
        ("ext3.img", {"compat": 0x000C, "incompat": 0x0002, "state": 1}),
        ("ext4.img", {"compat": 0x0048, "incompat": 0x0042, "state": 1}),
    ):
        payload = _ext_image(**kwargs)
        image = tmp_path / name
        image.write_bytes(payload)
        report = preflight_filesystem(image, offset=0, volume_size=len(payload))

        assert report.native_write_profile is None
        assert report.eligible_for_write is False
        assert report.write_refusal["code"] == (
            RefusalCode.OPERATION_NOT_SUPPORTED.value
        )


def test_unknown_ext_feature_bits_fail_closed(tmp_path):
    image = tmp_path / "ext2-unknown.img"
    payload = _ext_image(incompat=0x0002 | 0x40000000)
    image.write_bytes(payload)

    report = preflight_filesystem(image, offset=0, volume_size=len(payload))

    assert _gate_states(report)["known-features"] == GATE_FAIL
    assert report.eligible_for_read is False


def test_refusal_message_classification_is_preserved_for_callers(tmp_path):
    image = tmp_path / "ext2-dirty.img"
    payload = _ext_image(state=0)
    image.write_bytes(payload)

    report = preflight_filesystem(image, offset=0, volume_size=len(payload))
    refusal = RefusalError(
        report.refusal["message"], RefusalCode(report.refusal["code"])
    )

    assert refusal.category.value == "dirty_recovery"
    assert refusal.to_dict() == report.refusal


# --- report contract ------------------------------------------------------


def test_report_is_json_serialisable_and_versioned(tmp_path):
    import json

    image = tmp_path / "fat12.img"
    _make_fat12(image)
    report = preflight_filesystem(image)

    payload = json.loads(report.to_json())

    assert payload == report.to_dict()
    assert payload["schema_version"] == PREFLIGHT_SCHEMA_VERSION
    assert set(payload) >= {
        "eligible_for_read",
        "eligible_for_write",
        "write_refusal",
        "gates",
        "operations",
    }


def test_reported_profile_id_exists_in_the_capability_matrix(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)
    report = preflight_filesystem(image)

    matrix = get_native_capability_matrix()
    profile_ids = {profile["profile_id"] for profile in matrix["profiles"]}

    assert report.profile_id in profile_ids


def test_preflight_image_validates_every_partition_extent(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)

    payload = preflight_image(image)

    assert payload["schema_version"] == PREFLIGHT_SCHEMA_VERSION
    assert len(payload["volumes"]) >= 1
    assert payload["volumes"][0]["eligible_for_write"] is False


def test_preflight_image_with_explicit_offset_reports_one_volume(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)

    payload = preflight_image(image, offset=0, volume_size=os.path.getsize(image))

    assert len(payload["volumes"]) == 1
    assert payload["volumes"][0]["filesystem"] == "FAT12"


def test_preflight_rejects_non_integer_offset(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)

    with pytest.raises(RefusalError):
        preflight_filesystem(image, offset="0")


def test_preflight_rejects_boolean_offset(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)

    with pytest.raises(RefusalError):
        preflight_filesystem(image, offset=True)


def test_preflight_rejects_non_positive_volume_size(tmp_path):
    image = tmp_path / "fat12.img"
    _make_fat12(image)

    with pytest.raises(RefusalError):
        preflight_filesystem(image, volume_size=0)
