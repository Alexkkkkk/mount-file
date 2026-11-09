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
    RW_READ_CAPABILITIES,
    RW_WRITE_CAPABILITIES,
    VFS_READ_CAPABILITIES,
    VFS_WRITE_CAPABILITIES,
    api_operation_for_canonical_operation,
    canonical_operation_for_api_operation,
    get_native_capability_matrix,
)
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.ext2_reader import NativeExt2Image
from mountfile.ext3_reader import NativeExt3Image, NativeExt4Image
from mountfile.fat_reader import NativeFatImage
from mountfile.ntfs_reader import NativeNtfsMetadataImage
from mountfile.safety import RefusalCode, RefusalError, RefusalRuntimeError
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
            if profile.profile_id == "ext2-native-rw":
                assert profile.write_capabilities == (*RW_WRITE_CAPABILITIES, "link")
            else:
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


@pytest.mark.parametrize(
    ("interface", "api_operation", "canonical"),
    [
        ("vfs", "list", "list"),
        ("vfs", "read-file", "read-file"),
        ("vfs", "read-range", "read-range"),
        ("universal-fs-rw", "read", "read-file"),
        ("universal-fs-rw", "read_range", "read-range"),
        ("universal-fs-rw", "write_file", "write-file"),
        ("universal-fs-rw", "rename", "rename"),
        ("universal-fs-rw", "link", "hard-link"),
    ],
)
def test_operation_aliases_round_trip_to_canonical_cells(
    interface, api_operation, canonical
):
    assert api_operation_for_canonical_operation(interface, canonical) == api_operation
    assert canonical_operation_for_api_operation(interface, api_operation) == canonical
    assert api_operation_for_canonical_operation(interface, "unknown-operation") is None
    assert canonical_operation_for_api_operation(interface, "unknown-operation") is None


def test_v6_matrix_publishes_complete_operation_and_gate_contract():
    snapshot = get_native_capability_matrix()
    assert snapshot["operations"] == [
        "list",
        "stat",
        "read-file",
        "read-range",
        "extract",
        "write-file",
        "mkdir",
        "delete",
        "rename",
        "hard-link",
    ]
    for profile in snapshot["profiles"]:
        assert profile["constraint_ids"]
        assert profile["gates"]
        assert set(profile["operations"]) == set(snapshot["operations"])
        for operation in profile["operations"].values():
            assert operation["state"] in {"available", "refused"}
            if operation["state"] == "available":
                assert operation["api_operation"]
                assert operation["refusal"] is None
            else:
                assert operation["api_operation"] is None
                assert operation["refusal"]["code"] == "operation_not_supported"
                assert operation["refusal"]["category"] == "unsupported"

    profiles = {item["profile_id"]: item for item in snapshot["profiles"]}
    ext3_gate_ids = {item["id"] for item in profiles["ext3-native-ro"]["gates"]}
    assert {"bounded-extent", "clean-jbd2-state", "validated-jbd2"} <= ext3_gate_ids
    assert any(
        "triple-indirect blocks, up to 128 MiB" in constraint
        for constraint in profiles["ext2-native-rw"]["constraints"]
    )
    assert (
        "Explicit replay accepts classic 32-bit descriptor/revoke records and "
        "escaped data blocks in a validated no-checksum JBD2 overlay."
        in profiles["ext3-native-ro"]["constraints"]
    )
    assert (
        "Bounded 64BIT group-descriptor ext4 profiles are supported."
        in profiles["ext4-native-ro"]["constraints"]
    )
    assert not any(
        "Unsupported 64BIT" in constraint
        for constraint in profiles["ext4-native-ro"]["constraints"]
    )
    assert profiles["ext3-native-ro"]["operations"]["write-file"]["state"] == "refused"
    assert profiles["exfat-native-rw"]["operations"]["write-file"] == {
        "state": "available",
        "api_operation": "write_file",
        "refusal": None,
    }


def test_rw_facade_exposes_bounded_read_range_without_mutating_backend():
    class Backend:
        _reader = None

        @staticmethod
        def stat(_path):
            return SimpleNamespace(size=6, is_dir=False)

        @staticmethod
        def read(_path):
            return b"abcdef"

    facade = UniversalFSRW.__new__(UniversalFSRW)
    facade.backend = Backend()
    facade._closed = False

    assert facade.read_range("/file", 1, 3) == b"bcd"
    assert facade.read_range("/file", 6, 3) == b""
    with pytest.raises(RefusalError):
        facade.read_range("/file", 0, (1 << 20) + 1)


def test_advertised_operations_have_callable_interface_endpoints():
    # Matrix api_operation values are interface vocabulary; compare each value
    # to the actual public endpoint, not just to another matrix projection.
    interface_types = {
        "vfs": (
            NativeFatImage,
            NativeExt2Image,
            NativeExt3Image,
            NativeExt4Image,
            NativeExfatImage,
            NativeNtfsMetadataImage,
        ),
        "universal-fs-rw": UniversalFSRW,
    }
    endpoint_names = {
        "vfs": {
            "list": "list_dir",
            "stat": "stat",
            "read-file": "read_file",
            "read-range": "read_file_range",
            "extract": "extract",
        },
        "universal-fs-rw": {
            "list": "list",
            "stat": "stat",
            "read-file": "read",
            "read-range": "read_range",
            "extract": "extract",
            "write-file": "write_file",
            "mkdir": "mkdir",
            "delete": "delete",
            "rename": "rename",
            "hard-link": "link",
        },
    }
    for profile in NATIVE_CAPABILITY_MATRIX:
        api = interface_types[profile.interface]
        names = endpoint_names[profile.interface]
        implementations = api if isinstance(api, tuple) else (api,)
        for operation, cell in profile.operation_matrix().items():
            if cell["state"] != "available":
                continue
            assert all(
                callable(getattr(provider, names[operation], None))
                for provider in implementations
            ), (profile.profile_id, operation, cell["api_operation"])


@pytest.mark.parametrize("operation", ["write_file", "mkdir", "delete", "rename"])
def test_read_only_profile_mutations_share_typed_refusal(operation):
    class ReadOnlyBackend:
        @staticmethod
        def info():
            return FSInfo(
                fs_type="ext3",
                offset=0,
                backend="native-ext3-ro",
                writable=False,
                write_strategy="read-only",
            )

    facade = UniversalFSRW.__new__(UniversalFSRW)
    facade.backend = ReadOnlyBackend()
    facade._closed = False
    with pytest.raises(RefusalRuntimeError) as caught:
        facade._require_operation(operation)
    assert caught.value.code is RefusalCode.OPERATION_NOT_SUPPORTED
    assert caught.value.to_dict()["code"] == "operation_not_supported"


def test_v5_matrix_publishes_evidence_bounded_recovery_contracts():
    profiles = {
        item["profile_id"]: item for item in get_native_capability_matrix()["profiles"]
    }
    for profile in profiles.values():
        recovery = profile["recovery"]
        assert set(recovery) == {
            "rollback",
            "process_kill",
            "power_loss",
            "quarantine",
        }
        assert recovery["power_loss"] == "unsupported"
    assert profiles["ext2-native-rw"]["recovery"] == {
        "rollback": "tested_on_failure",
        "process_kill": "tested_with_recovery_sidecar",
        "power_loss": "unsupported",
        "quarantine": "tested_on_recovery_failure",
    }
    for profile_id in (
        "fat12-16-native-rw",
        "fat32-native-rw",
        "exfat-native-rw",
    ):
        assert profiles[profile_id]["recovery"]["process_kill"] == "not_proven"
        assert profiles[profile_id]["recovery"]["quarantine"] == "not_proven"


def test_exfat_matrix_matches_single_fat_and_volume_flag_gates(tmp_path, monkeypatch):
    profiles = {
        profile["profile_id"]: profile
        for profile in get_native_capability_matrix()["profiles"]
    }
    assert profiles["exfat-native-ro"]["constraints"] == [
        "Bounded one-FAT profile only; two-FAT TexFAT is refused for native browsing and writes.",
        "Dirty or media-failure volumes are refused; ClearToZero is allowed read-only.",
        "A present root volume-label entry is validated; malformed or duplicate "
        "labels are refused.",
    ]
    assert profiles["exfat-native-rw"]["constraints"] == [
        "Bounded one-FAT profile only; two-FAT TexFAT is refused for native browsing and writes.",
        "Requires ActiveFat=0; refuses dirty, media-failure, ClearToZero, and reserved flags.",
        "Unsupported metadata layouts are refused.",
        "A single root volume label is validated and exposed through FSInfo.label.",
    ]
    for profile_id in ("exfat-native-ro", "exfat-native-rw"):
        gate = next(
            gate
            for gate in profiles[profile_id]["gates"]
            if gate["id"] == "single-exfat-fat"
        )
        assert gate["state"] == "required"
        assert gate["refusal"]["code"] == "unsupported_profile"
        assert gate["refusal"]["category"] == "unsupported"

    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(
        cluster_count=100,
        fat_offset=24,
        active_fat=0,
        fat_length=8,
        bytes_per_sector=512,
        number_of_fats=1,
    )
    fat_reads = []
    reader._read_rel = lambda offset, size: fat_reads.append(
        (offset, size)
    ) or struct.pack("<I", 0x12345678)
    assert reader._fat_entry(3) == 0x12345678
    assert fat_reads == [(24 * 512 + 3 * 4, 4)]

    writer = NativeExfatWriter.__new__(NativeExfatWriter)
    fat_writes = []
    writer._write_rel = lambda _stream, offset, data: fat_writes.append((offset, data))
    writer._fat_write(object(), reader, 3, 0x12345678)
    assert fat_writes == [
        (24 * 512 + 3 * 4, struct.pack("<I", 0x12345678)),
    ]

    image = tmp_path / "exfat.img"
    image.write_bytes(b"\0" * 512)
    reader_profile = SimpleNamespace(
        number_of_fats=1, volume_flags=0x0008, volume_length=1, bytes_per_sector=512
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
        with pytest.raises(ExfatReadError, match=message):
            NativeExfatImage(image, volume_size=512)

    writer_profile = SimpleNamespace(
        number_of_fats=1, volume_flags=0, volume_length=1, bytes_per_sector=512
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
    assert status["mode"] == profile.mode
    assert status["profile_id"] == profile.profile_id
    assert status["matrix_version"] == CAPABILITY_MATRIX_VERSION
    assert status["read_capabilities"] == list(profile.read_capabilities)
    assert status["write_capabilities"] == list(profile.write_capabilities)
    assert status["write_enabled"] is True
    assert status["write_rejection_reason"] == ""
    assert status["write_rejection_code"] is None
    assert status["write_rejection"] is None
    assert status["operations"]["write-file"] == {
        "state": "available",
        "api_operation": "write_file",
        "refusal": None,
    }
    assert status["operations"]["stat"] == {
        "state": "available",
        "api_operation": "stat",
        "refusal": None,
    }
    assert status["constraint_ids"]
    assert status["gates"]
    assert status["recovery"] == profile.recovery_contract
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
    assert status["operations"]["write-file"]["state"] == "refused"
    assert status["operations"]["write-file"]["refusal"]["code"] == (
        "operation_not_supported"
    )


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
    assert status["write_rejection"]["category"] == "unsupported"
