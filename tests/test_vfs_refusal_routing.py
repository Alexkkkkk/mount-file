# SPDX-License-Identifier: MIT
"""Preserve native refusal reasons across the public VFS routing boundary."""

from types import SimpleNamespace

import pytest

from mountfile import vfs
from mountfile.exfat_reader import ExfatReadError
from mountfile.ext2_reader import Ext2ReadError
from mountfile.ext3_reader import Ext3ReadError
from mountfile.fat_reader import FatReadError
from mountfile.ntfs_reader import NtfsMetadataError
from mountfile.safety import RefusalCategory, RefusalCode, RefusalError


@pytest.mark.parametrize(
    "fs_type,exception_type,reader",
    [
        ("ext2", Ext2ReadError, "open_native_ext2"),
        ("ext3", Ext3ReadError, "open_native_ext3"),
        ("ext4", Ext3ReadError, "open_native_ext3"),
        ("exfat", ExfatReadError, "NativeExfatImage"),
        ("fat", FatReadError, "NativeFatImage"),
        ("ntfs", NtfsMetadataError, "open_native_ntfs_metadata"),
    ],
)
@pytest.mark.parametrize(
    "code,category",
    [
        (RefusalCode.MALFORMED_METADATA, RefusalCategory.CORRUPT),
        (RefusalCode.RANGE_OUT_OF_BOUNDS, RefusalCategory.OUT_OF_BOUNDS),
        (RefusalCode.DIRTY_OR_RECOVERY_REQUIRED, RefusalCategory.DIRTY_RECOVERY),
    ],
)
def test_recognized_format_preserves_native_refusal_without_fallback(
    tmp_path, monkeypatch, fs_type, exception_type, reader, code, category
):
    image = tmp_path / "image.raw"
    image.write_bytes(b"\0" * 4096)
    for family in ("ext_superblock", "exfat_boot", "ntfs_boot", "fat_boot"):
        expected = (
            fs_type in ("ext2", "ext3", "ext4")
            if family == "ext_superblock"
            else (fs_type == family.split("_")[0])
        )
        monkeypatch.setattr(
            vfs, f"_has_{family}_magic", lambda *_args, result=expected: result
        )
    monkeypatch.setattr(
        vfs,
        "probe_ext_superblock",
        lambda *_args, **_kwargs: SimpleNamespace(
            fs_type=fs_type, clean_unmounted=True, journal_recovery_needed=False
        ),
    )
    monkeypatch.setattr(vfs, "probe_exfat_profile", lambda *_args, **_kwargs: None)

    def fail_reader(*_args, **_kwargs):
        raise exception_type("metadata problem with neutral wording", code=code)

    monkeypatch.setattr(vfs, reader, fail_reader)
    monkeypatch.setattr(
        vfs, "TskImage", lambda *_args, **_kwargs: pytest.fail("fallback")
    )
    with pytest.raises(vfs.RecognizedFilesystemError) as caught:
        vfs.open_image_provider(image, volume_size=4096, allow_legacy_fallback=True)
    assert caught.value.code is code
    assert caught.value.category is category
    assert caught.value.to_dict()["code"] == code.value
    assert isinstance(caught.value.__cause__, exception_type)


def test_typed_exfat_probe_refusal_is_preserved(tmp_path, monkeypatch):
    image = tmp_path / "image.raw"
    image.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(vfs, "_has_ext_superblock_magic", lambda *_args: False)
    monkeypatch.setattr(vfs, "_has_exfat_boot_magic", lambda *_args: True)

    def fail_probe(*_args, **_kwargs):
        raise RefusalError("neutral wording", RefusalCode.INTEGRITY_CHECK_FAILED)

    monkeypatch.setattr(vfs, "probe_exfat_profile", fail_probe)
    with pytest.raises(vfs.RecognizedFilesystemError) as caught:
        vfs.open_image_provider(image, volume_size=4096, allow_legacy_fallback=True)
    assert caught.value.code is RefusalCode.INTEGRITY_CHECK_FAILED


def test_untyped_probe_error_keeps_legacy_message_classification(tmp_path, monkeypatch):
    image = tmp_path / "image.raw"
    image.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(vfs, "_has_ext_superblock_magic", lambda *_args: False)
    monkeypatch.setattr(vfs, "_has_exfat_boot_magic", lambda *_args: True)

    def fail_probe(*_args, **_kwargs):
        raise ValueError("checksum mismatch")

    monkeypatch.setattr(vfs, "probe_exfat_profile", fail_probe)
    with pytest.raises(vfs.RecognizedFilesystemError) as caught:
        vfs.open_image_provider(image, volume_size=4096)
    assert caught.value.code is RefusalCode.MALFORMED_METADATA
