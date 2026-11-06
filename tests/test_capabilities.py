"""Conformance tests for the canonical native filesystem capability matrix."""

import json
import struct
from types import SimpleNamespace

import pytest

from mountfile.capabilities import (
    CAPABILITY_MATRIX_VERSION,
    NATIVE_CAPABILITY_MATRIX,
    NATIVE_CAPABILITY_PROFILES,
    NATIVE_WRITE_PROFILES_BY_STRATEGY,
    OPERATION_VOCABULARY,
    RW_READ_CAPABILITIES,
    RW_WRITE_CAPABILITIES,
    VFS_READ_CAPABILITIES,
    VFS_WRITE_CAPABILITIES,
    get_native_capability_matrix,
)
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.safety import RefusalCode
from mountfile.universal_fs_rw import FSInfo, UniversalFSRW


def test_native_capability_matrix_is_unique_and_json_serialisable():
    profile_ids = [profile.profile_id for profile in NATIVE_CAPABILITY_MATRIX]
    assert len(profile_ids) == len(set(profile_ids))
    assert set(NATIVE_CAPABILITY_PROFILES) == set(profile_ids)

    snapshot = get_native_capability_matrix()
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["version"] == CAPABILITY_MATRIX_VERSION
    assert all(profile["constraints"] for profile in snapshot["profiles"])
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
    exfat = next(
        profile
        for profile in snapshot["profiles"]
        if profile["profile_id"] == "exfat-native-rw"
    )
    exfat["known_gates"]["feature_gates"][0]["supported_values"].append(3)
    live_exfat = next(
        profile
        for profile in get_native_capability_matrix()["profiles"]
        if profile["profile_id"] == "exfat-native-rw"
    )
    assert live_exfat["known_gates"]["feature_gates"][0]["supported_values"] == [1, 2]


def test_read_and_write_profiles_use_the_correct_api_vocabulary():
    for profile in NATIVE_CAPABILITY_MATRIX:
        decisions = {
            decision["operation"]: decision
            for decision in profile.to_dict()["operation_decisions"]
        }
        advertised = set(profile.read_capabilities) | set(profile.write_capabilities)
        assert set(decisions) == set(OPERATION_VOCABULARY)
        assert {
            operation
            for operation, decision in decisions.items()
            if decision["outcome"] == "allow"
        } == advertised
        if profile.interface == "vfs":
            assert profile.mode == "read-only"
            assert profile.read_capabilities == VFS_READ_CAPABILITIES
            assert profile.write_capabilities == VFS_WRITE_CAPABILITIES == ()
        else:
            assert profile.interface == "universal-fs-rw"
            assert profile.mode == "read-write"
            assert profile.read_capabilities == RW_READ_CAPABILITIES
            assert profile.write_capabilities == RW_WRITE_CAPABILITIES


def test_vfs_matrix_distinguishes_whole_file_and_range_reads():
    assert CAPABILITY_MATRIX_VERSION == 6
    assert VFS_READ_CAPABILITIES == (
        "list",
        "stat",
        "read-file",
        "read-range",
        "extract",
    )
    for profile in NATIVE_CAPABILITY_MATRIX:
        if profile.interface == "vfs":
            assert "read-file" in profile.read_capabilities
            assert "read-range" in profile.read_capabilities
            assert profile.to_dict()["known_gates"]["coverage"] == (
                "selected-known-gates-not-exhaustive"
            )
            assert profile.to_dict()["known_gates"]["limits"]["max_read_range_bytes"]
            decisions = profile.to_dict()["operation_decisions"]
            assert {decision["operation"] for decision in decisions} == set(
                OPERATION_VOCABULARY
            )
            assert all(
                decision["refusal_code"]
                for decision in decisions
                if decision["outcome"] == "refuse"
            )
    assert get_native_capability_matrix()["coverage"] == (
        "complete-operation-vocabulary; selected-feature-state-gates-not-exhaustive"
    )


def test_exfat_known_state_and_feature_gates_are_machine_readable():
    profiles = {
        profile["profile_id"]: profile
        for profile in get_native_capability_matrix()["profiles"]
    }
    exfat_ro = profiles["exfat-native-ro"]["known_gates"]
    assert {gate["state"] for gate in exfat_ro["state_gates"]} == {
        "volume_dirty",
        "media_failure",
    }
    assert exfat_ro["limits"]["max_read_range_bytes"] == 16 * 1024 * 1024
    exfat_rw = profiles["exfat-native-rw"]["known_gates"]
    feature = exfat_rw["feature_gates"][0]
    assert feature["supported_values"] == [1, 2]
    assert feature["selection"] == "validated_active_fat_and_mirror_updates"


def test_exfat_matrix_matches_active_multifat_and_volume_flag_gates(
    tmp_path, monkeypatch
):
    profiles = {
        profile["profile_id"]: profile
        for profile in get_native_capability_matrix()["profiles"]
    }
    assert profiles["exfat-native-ro"]["constraints"] == [
        "Bounded one- or two-FAT profile; reads the validated selected active FAT.",
        "Dirty or media-failure volumes are refused; ClearToZero is allowed read-only.",
    ]
    assert profiles["exfat-native-rw"]["constraints"] == [
        "Bounded one- or two-FAT profile; reads the validated active FAT and "
        "mirrors FAT writes to all declared copies.",
        "Allows only a validated ActiveFat selection (VolumeFlags 0x0000, or "
        "0x0001 with two FATs); refuses dirty, media-failure, ClearToZero, and "
        "reserved flags.",
        "Unsupported metadata layouts are refused.",
    ]

    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(
        cluster_count=100,
        fat_offset=24,
        active_fat=1,
        fat_length=8,
        bytes_per_sector=512,
        number_of_fats=2,
    )
    fat_reads = []
    reader._read_rel = lambda offset, size: fat_reads.append(
        (offset, size)
    ) or struct.pack("<I", 0x12345678)
    assert reader._fat_entry(3) == 0x12345678
    assert fat_reads == [((24 + 8) * 512 + 3 * 4, 4)]

    writer = NativeExfatWriter.__new__(NativeExfatWriter)
    fat_writes = []
    writer._write_rel = lambda _stream, offset, data: fat_writes.append((offset, data))
    writer._fat_write(object(), reader, 3, 0x12345678)
    assert fat_writes == [
        (24 * 512 + 3 * 4, struct.pack("<I", 0x12345678)),
        (32 * 512 + 3 * 4, struct.pack("<I", 0x12345678)),
    ]

    image = tmp_path / "exfat.img"
    image.write_bytes(b"\0" * 512)
    reader_profile = SimpleNamespace(
        volume_flags=0x0008, volume_length=1, bytes_per_sector=512
    )
    monkeypatch.setattr(
        "mountfile.exfat_reader.probe_exfat_boot_region",
        lambda *_args, **_kwargs: reader_profile,
    )
    monkeypatch.setattr(NativeExfatImage, "system_metadata", lambda _self: {})
    clear_to_zero_reader = NativeExfatImage(image, volume_size=512)
    clear_to_zero_reader.close()
    for flags, message in ((0x0002, "volume is dirty"), (0x0004, "media failure")):
        reader_profile.volume_flags = flags
        with pytest.raises(ExfatReadError, match=message) as caught:
            NativeExfatImage(image, volume_size=512)
        expected_code = (
            RefusalCode.DIRTY_OR_RECOVERY_REQUIRED
            if flags == 0x0002
            else RefusalCode.INTEGRITY_CHECK_FAILED
        )
        assert caught.value.code is expected_code

    writer_profile = SimpleNamespace(
        volume_flags=0x0001, volume_length=1, bytes_per_sector=512
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer.probe_exfat_boot_region",
        lambda *_args, **_kwargs: writer_profile,
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer._image_identity",
        lambda *_args, **_kwargs: (512, b"x" * 32),
    )
    NativeExfatWriter(image, volume_size=512)
    for flags, message in (
        (0x0002, "volume is dirty"),
        (0x0004, "media failure"),
        (0x0008, "ClearToZero"),
        (0x0010, "reserved bits"),
    ):
        writer_profile.volume_flags = flags
        with pytest.raises(ExfatWriteError, match=message):
            NativeExfatWriter(image, volume_size=512)


def test_matrix_exposes_profile_gates_and_does_not_advertise_unsafe_writers():
    profiles = {
        profile["profile_id"]: profile
        for profile in get_native_capability_matrix()["profiles"]
    }
    assert not any(
        profile["filesystem"] in {"ext3", "ext4", "NTFS"}
        and profile["mode"] == "read-write"
        for profile in profiles.values()
    )
    assert (
        "No writes or complete crash-recovery guarantee."
        in profiles["ext3-native-ro"]["constraints"]
    )
    assert any(
        "metadata_csum" in item for item in profiles["ext4-native-ro"]["constraints"]
    )
    assert any("$LogFile" in item for item in profiles["ntfs-native-ro"]["constraints"])


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
    assert status["interface"] == "universal-fs-rw"
    assert status["mode"] == profile.mode
    assert status["profile_id"] == profile.profile_id
    assert status["matrix_version"] == CAPABILITY_MATRIX_VERSION
    assert status["read_capabilities"] == list(profile.read_capabilities)
    assert status["write_capabilities"] == list(profile.write_capabilities)
    assert status["write_enabled"] is True
    assert status["write_rejection_reason"] == ""
    assert status["write_rejection_code"] is None
    assert (
        tuple(status["write_capabilities"])
        == NATIVE_WRITE_PROFILES_BY_STRATEGY[strategy].write_capabilities
    )


@pytest.mark.parametrize("strategy", ["native", "native-ext2", "native-exfat-phase1"])
def test_native_strategy_does_not_enable_writes_without_writable_flag(strategy):
    profile = NATIVE_WRITE_PROFILES_BY_STRATEGY[strategy]
    info = FSInfo(
        fs_type=profile.filesystem,
        offset=0,
        backend=profile.backend,
        writable=False,
        write_strategy=strategy,
    )

    status = UniversalFSRW.backend_status(info)

    assert status["mode"] == "read-only"
    assert status["write_capabilities"] == []
    assert status["write_enabled"] is False
    assert status["write_rejection_reason"]
    assert status["write_rejection_code"] == "operation_not_supported"


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
    assert status["write_rejection_code"] == "unsupported_profile"
