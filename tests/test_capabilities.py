"""Conformance tests for the canonical native filesystem capability matrix."""

import json

import pytest

from mountfile.capabilities import (
    CAPABILITY_MATRIX_VERSION,
    NATIVE_CAPABILITY_MATRIX,
    NATIVE_CAPABILITY_PROFILES,
    NATIVE_WRITE_PROFILES_BY_STRATEGY,
    RW_READ_CAPABILITIES,
    RW_WRITE_CAPABILITIES,
    VFS_READ_CAPABILITIES,
    VFS_WRITE_CAPABILITIES,
    get_native_capability_matrix,
)
from mountfile.universal_fs_rw import FSInfo, UniversalFSRW


def test_native_capability_matrix_is_unique_and_json_serialisable():
    profile_ids = [profile.profile_id for profile in NATIVE_CAPABILITY_MATRIX]
    assert len(profile_ids) == len(set(profile_ids))
    assert set(NATIVE_CAPABILITY_PROFILES) == set(profile_ids)

    snapshot = get_native_capability_matrix()
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["version"] == CAPABILITY_MATRIX_VERSION
    assert {profile["filesystem"] for profile in snapshot["profiles"]} == {
        "FAT12",
        "FAT16",
        "FAT32",
        "ext2",
        "ext3",
        "ext4",
        "NTFS",
        "exFAT",
    }
    snapshot["profiles"][0]["read_capabilities"].append("caller-mutation")
    assert (
        "caller-mutation"
        not in get_native_capability_matrix()["profiles"][0]["read_capabilities"]
    )


def test_read_and_write_profiles_use_the_correct_api_vocabulary():
    for profile in NATIVE_CAPABILITY_MATRIX:
        if profile.interface == "vfs":
            assert profile.mode == "read-only"
            assert profile.read_capabilities == VFS_READ_CAPABILITIES
            assert profile.write_capabilities == VFS_WRITE_CAPABILITIES == ()
        else:
            assert profile.interface == "universal-fs-rw"
            assert profile.mode == "read-write"
            assert profile.read_capabilities == RW_READ_CAPABILITIES
            assert profile.write_capabilities == RW_WRITE_CAPABILITIES


@pytest.mark.parametrize(
    ("strategy", "profile_id"),
    [
        ("native", "fat32-native-rw"),
        ("native-ext2", "ext2-native-rw"),
        ("native-exfat-phase1", "exfat-native-rw"),
    ],
)
def test_native_writer_status_matches_machine_readable_profile_matrix(
    strategy, profile_id
):
    profile = NATIVE_CAPABILITY_PROFILES[profile_id]
    info = FSInfo(
        fs_type=profile.filesystem,
        offset=0,
        backend=profile.backend,
        writable=True,
        write_strategy=strategy,
    )

    status = UniversalFSRW.backend_status(info)

    assert status["backend"] == profile.backend
    assert status["mode"] == profile.mode
    assert status["read_capabilities"] == list(profile.read_capabilities)
    assert status["write_capabilities"] == list(profile.write_capabilities)
    assert status["write_enabled"] is True
    assert status["write_rejection_reason"] == ""
    assert (
        tuple(status["write_capabilities"])
        == NATIVE_WRITE_PROFILES_BY_STRATEGY[strategy].write_capabilities
    )


@pytest.mark.parametrize("strategy", ["native", "native-ext2", "native-exfat-phase1"])
def test_native_strategy_does_not_enable_writes_without_writable_flag(strategy):
    info = FSInfo(
        fs_type="unknown",
        offset=0,
        backend="native-candidate",
        writable=False,
        write_strategy=strategy,
    )

    status = UniversalFSRW.backend_status(info)

    assert status["mode"] == "read-only"
    assert status["write_capabilities"] == []
    assert status["write_enabled"] is False
    assert status["write_rejection_reason"]


@pytest.mark.parametrize(
    ("strategy", "fs_type", "backend"),
    [
        ("native", "fat16", "native-fat-rw"),
        ("native", "fat32", "wrong-backend"),
        ("native-ext2", "ext4", "native-ext2"),
        ("native-exfat-phase1", "ntfs", "native-exfat-rw"),
    ],
)
def test_native_strategy_rejects_mismatched_filesystem_profile(
    strategy, fs_type, backend
):
    info = FSInfo(
        fs_type=fs_type,
        offset=0,
        backend=backend,
        writable=True,
        write_strategy=strategy,
    )

    status = UniversalFSRW.backend_status(info)

    assert status["mode"] == "read-only"
    assert status["write_capabilities"] == []
    assert status["write_enabled"] is False
    assert status["write_rejection_reason"]
