# SPDX-License-Identifier: MIT
"""FAT12/16 shared writer profile must retain the selected filesystem identity."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mountfile.capabilities import (
    CAPABILITY_MATRIX_VERSION,
    NATIVE_CAPABILITY_PROFILES,
    RW_READ_CAPABILITIES,
    RW_WRITE_CAPABILITIES,
    VFS_READ_CAPABILITIES,
    get_native_capability_matrix,
    get_native_capability_status,
)
from mountfile.fat_reader import NativeFatImage
from mountfile.safety import RefusalCode, RefusalRuntimeError
from mountfile.universal_fs_rw import Fat1216RW, FSInfo, UniversalFSRW
from tests.test_fat_reader import _make_small_fat

_WRITES = {
    "write-file": "write_file",
    "mkdir": "mkdir",
    "delete": "delete",
    "rename": "rename",
}


def _fat_info(fs_type, *, writable=True, backend="native-fat-rw"):
    return FSInfo(
        fs_type=fs_type,
        offset=0,
        backend=backend,
        write_strategy="native-fat12-16",
        writable=writable,
    )


@pytest.mark.parametrize(
    ("fs_type", "expected_filesystem"),
    [
        ("FAT12", "FAT12"),
        ("fat12", "FAT12"),
        ("FaT12", "FAT12"),
        ("FAT16", "FAT16"),
        ("fat16", "FAT16"),
        ("FaT16", "FAT16"),
    ],
)
@pytest.mark.parametrize("writable", [True, False], ids=["writable", "read-only"])
def test_shared_writer_status_reports_actual_fat_type(
    fs_type, expected_filesystem, writable
):
    info = _fat_info(fs_type, writable=writable)
    original_info = info.to_dict()
    profile = NATIVE_CAPABILITY_PROFILES["fat12-16-native-rw"]
    before_matrix = get_native_capability_matrix()

    status = UniversalFSRW.backend_status(info)

    # Keep the shared profile's full contract, changing only the selected
    # instance's filesystem identity. In particular, do not create a new
    # profile, change the version, or mutate the global matrix.
    expected = get_native_capability_status(
        profile,
        backend=info.backend,
        write_enabled=writable,
        write_rejection_reason=(
            None
            if writable
            else "native write strategy does not match the selected writable "
            "filesystem/backend profile"
        ),
        write_rejection_code=(
            None if writable else RefusalCode.OPERATION_NOT_SUPPORTED
        ),
    )
    expected["filesystem"] = expected_filesystem
    assert status == expected
    assert status["filesystem"] == expected_filesystem
    assert status["profile_id"] == "fat12-16-native-rw"
    assert status["matrix_version"] == CAPABILITY_MATRIX_VERSION
    assert status["mode"] == ("read-write" if writable else "read-only")
    assert status["read_capabilities"] == list(RW_READ_CAPABILITIES)
    assert status["write_capabilities"] == (
        list(RW_WRITE_CAPABILITIES) if writable else []
    )
    assert status["write_enabled"] is writable
    for canonical, api_operation in _WRITES.items():
        operation = status["operations"][canonical]
        if writable:
            assert operation == {
                "state": "available",
                "api_operation": api_operation,
                "refusal": None,
            }
        else:
            assert operation["state"] == "refused"
            assert operation["api_operation"] is None
            assert operation["refusal"]["code"] == "operation_not_supported"
    assert json.loads(json.dumps(status)) == status
    assert info.to_dict() == original_info
    assert get_native_capability_matrix() == before_matrix


@pytest.mark.parametrize("fat_type", [12, 16])
def test_real_universal_rw_routes_report_identity_and_allow_writes(
    tmp_path, monkeypatch, fat_type
):
    monkeypatch.setenv("HOME", str(tmp_path))
    image = tmp_path / f"fat{fat_type}.img"
    payload = _make_small_fat(image, fat_type)

    with UniversalFSRW(
        image, offset=0, volume_size=image.stat().st_size, auto_backup=False
    ) as fs:
        assert isinstance(fs.backend, Fat1216RW)
        backend_info = fs.backend.info()
        status = fs.capability_status()
        facade_info = fs.info()
        assert backend_info.fs_type == facade_info.fs_type == f"fat{fat_type}"
        assert status == UniversalFSRW.backend_status(backend_info)
        assert status == UniversalFSRW.backend_status(facade_info)
        assert status["filesystem"] == f"FAT{fat_type}"
        assert status["profile_id"] == "fat12-16-native-rw"
        assert facade_info.read_capabilities == RW_READ_CAPABILITIES
        assert facade_info.write_capabilities == RW_WRITE_CAPABILITIES
        assert facade_info.write_rejection_reason == ""
        assert facade_info.write_rejection_code is None
        assert fs.read("/HELLO.TXT") == payload
        # Exercise the status-based operation gate, not just its metadata.
        fs.write_file("/STATUS.TXT", b"identity preserved")
        assert fs.read("/STATUS.TXT") == b"identity preserved"
        assert fs.capability_status()["filesystem"] == f"FAT{fat_type}"


@pytest.mark.parametrize("fat_type", [12, 16])
def test_read_only_writer_status_rejects_mutation_before_backend_call(fat_type):
    info = _fat_info(f"fat{fat_type}", writable=False)
    backend_write = Mock()
    fs = UniversalFSRW.__new__(UniversalFSRW)
    fs.backend = SimpleNamespace(info=lambda: info, write_file=backend_write)

    status = fs.capability_status()
    facade_info = fs.info()
    assert status["filesystem"] == f"FAT{fat_type}"
    assert status["write_enabled"] is False
    assert status["write_rejection_code"] == "operation_not_supported"
    assert status["write_rejection"]["code"] == "operation_not_supported"
    assert facade_info.fs_type == f"fat{fat_type}"
    assert facade_info.writable is False
    assert facade_info.write_capabilities == ()
    assert facade_info.write_rejection_code == "operation_not_supported"
    with pytest.raises(RefusalRuntimeError) as error:
        fs.write_file("/must-not-write", b"data")
    assert error.value.code == RefusalCode.OPERATION_NOT_SUPPORTED
    backend_write.assert_not_called()


@pytest.mark.parametrize("fat_type", [12, 16])
def test_real_read_only_vfs_preserves_type_and_refuses_all_writes(tmp_path, fat_type):
    image = tmp_path / f"fat{fat_type}.img"
    payload = _make_small_fat(image, fat_type)
    provider = NativeFatImage(image)
    try:
        status = provider.backend_status
        assert status["backend"] == f"native-fat{fat_type}-ro"
        assert status["filesystem"] == f"FAT{fat_type}"
        assert status["profile_id"] == f"fat{fat_type}-native-ro"
        assert status["interface"] == "vfs"
        assert status["mode"] == "read-only"
        assert status["read_capabilities"] == list(VFS_READ_CAPABILITIES)
        assert status["write_capabilities"] == []
        assert status["write_enabled"] is False
        assert status["write_rejection_code"] == "operation_not_supported"
        assert status["write_rejection"]["code"] == "operation_not_supported"
        for canonical in _WRITES:
            assert status["operations"][canonical]["state"] == "refused"
            assert status["operations"][canonical]["refusal"]["code"] == (
                "operation_not_supported"
            )
        assert json.loads(json.dumps(status)) == status
        assert provider.read_file("/HELLO.TXT") == payload
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("fs_type", "backend"),
    [
        ("FAT32", "native-fat-rw"),
        ("exFAT", "native-fat-rw"),
        ("FAT12", "wrong-backend"),
        ("FAT16", "native-fat16-ro"),
    ],
)
@pytest.mark.parametrize("writable", [True, False])
def test_shared_strategy_fails_closed_for_mismatched_profiles(
    fs_type, backend, writable
):
    status = UniversalFSRW.backend_status(
        _fat_info(fs_type, backend=backend, writable=writable)
    )
    assert status["backend"] == backend
    assert status["mode"] == "read-only"
    assert status["write_enabled"] is False
    assert status["write_capabilities"] == []
    assert status["write_rejection_code"] == "unsupported_profile"
    assert "profile_id" not in status


@pytest.mark.parametrize(
    ("fs_type", "backend", "strategy", "profile_id"),
    [
        ("fat32", "native-fat-rw", "native", "fat32-native-rw"),
        ("EXT2", "native-ext2", "native-ext2", "ext2-native-rw"),
        ("exfat", "native-exfat-rw", "native-exfat-phase1", "exfat-native-rw"),
    ],
)
def test_other_native_writer_identities_are_unchanged(
    fs_type, backend, strategy, profile_id
):
    info = FSInfo(fs_type, 0, backend, writable=True, write_strategy=strategy)
    profile = NATIVE_CAPABILITY_PROFILES[profile_id]
    assert UniversalFSRW.backend_status(info) == get_native_capability_status(
        profile, backend=backend, write_enabled=True
    )


def test_returned_fat_status_is_detached_from_other_results_and_matrix():
    baseline = get_native_capability_matrix()
    fat12 = UniversalFSRW.backend_status(_fat_info("FAT12"))
    fat16 = UniversalFSRW.backend_status(_fat_info("FAT16"))
    fat12_expected = deepcopy(fat12)
    fat16["filesystem"] = "caller-mutation"
    fat16["constraints"].append("caller-mutation")
    fat16["operations"]["write-file"]["state"] = "caller-mutation"
    assert fat12 == fat12_expected
    assert UniversalFSRW.backend_status(_fat_info("FAT16"))["filesystem"] == "FAT16"
    assert get_native_capability_matrix() == baseline
