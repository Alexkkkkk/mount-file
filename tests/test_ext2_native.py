# SPDX-License-Identifier: MIT
"""Regression coverage for the in-process, limited-profile ext2 writer."""

import shutil
import subprocess

import pytest

from mountfile.ext2_native import NativeExt2Error, NativeExt2ImageWriter

MKE2FS = shutil.which("mke2fs")
E2FSCK = shutil.which("e2fsck")
pytestmark = pytest.mark.skipif(
    not MKE2FS, reason="mke2fs is required for ext2 fixtures"
)


def _make_ext2(path, size=16 * 1024 * 1024, block_size=4096):
    with open(path, "wb") as stream:
        stream.truncate(size)
    subprocess.run(
        [
            MKE2FS,
            "-q",
            "-F",
            "-t",
            "ext2",
            "-b",
            str(block_size),
            "-O",
            "none,filetype,sparse_super,large_file",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _assert_e2fsck_clean(path):
    if not E2FSCK:
        pytest.skip("e2fsck is required for independent fixture validation")
    command = [E2FSCK, "-fn", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_ext2_file_and_directory_crud_is_consistent(tmp_path):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)

    writer.mkdir("/evidence")
    writer.write_file("/evidence/one.bin", b"first\x00payload")
    assert writer.read_file("/evidence/one.bin") == b"first\x00payload"
    writer.write_file("/evidence/one.bin", b"replacement" * 300)
    assert writer.read_file("/evidence/one.bin") == b"replacement" * 300
    writer.rename("/evidence/one.bin", "/evidence/final.bin")
    assert writer.read_file("/evidence/final.bin") == b"replacement" * 300
    assert [item["name"] for item in writer.list_dir("/evidence")] == ["final.bin"]
    writer.delete("/evidence/final.bin")
    writer.delete("/evidence")
    assert "evidence" not in {item["name"] for item in writer.list_dir("/")}
    _assert_e2fsck_clean(image)


@pytest.mark.parametrize("block_size", [1024, 2048, 4096])
def test_native_ext2_single_indirect_file_roundtrip_and_reclamation(
    tmp_path, block_size
):
    image = tmp_path / "single-indirect.ext2"
    _make_ext2(image, size=32 * 1024 * 1024, block_size=block_size)
    writer = NativeExt2ImageWriter(image)
    payload = bytes(index % 251 for index in range((12 + 7) * block_size + 37))

    writer.write_file("/indirect.bin", payload)
    assert writer.read_file("/indirect.bin") == payload
    _assert_e2fsck_clean(image)

    writer.write_file("/indirect.bin", b"smaller replacement")
    assert writer.read_file("/indirect.bin") == b"smaller replacement"
    _assert_e2fsck_clean(image)

    writer.delete("/indirect.bin")
    assert "indirect.bin" not in {item["name"] for item in writer.list_dir("/")}
    _assert_e2fsck_clean(image)


def test_native_ext2_works_at_nonzero_partition_offset_and_preserves_neighbors(
    tmp_path,
):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition, size=8 * 1024 * 1024)
    prefix = b"image-prefix" * 31
    suffix = b"next-partition-sentinel"
    image = tmp_path / "disk.img"
    with image.open("wb") as target, partition.open("rb") as source:
        target.write(prefix)
        shutil.copyfileobj(source, target)
        target.write(suffix)
    offset = len(prefix)
    volume_size = partition.stat().st_size

    writer = NativeExt2ImageWriter(image, offset=offset, volume_size=volume_size)
    writer.write_file("/offset-check.txt", b"partition data")
    assert writer.read_file("/offset-check.txt") == b"partition data"
    with image.open("rb") as stream:
        assert stream.read(offset) == prefix
        stream.seek(offset + volume_size)
        assert stream.read() == suffix
        stream.seek(offset)
        check_extent = stream.read(volume_size)
    assert len(check_extent) == volume_size
    check_image = tmp_path / "partition-after.ext2"
    check_image.write_bytes(check_extent)
    _assert_e2fsck_clean(check_image)


def test_native_ext2_requires_explicit_extent_for_nonzero_offset(tmp_path):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition, size=8 * 1024 * 1024)
    image = tmp_path / "disk.img"
    image.write_bytes(b"prefix" + partition.read_bytes() + b"suffix")
    with pytest.raises(NativeExt2Error, match="explicit selected partition size"):
        NativeExt2ImageWriter(image, offset=len(b"prefix"))


def test_native_ext2_rejects_root_inode_marked_free(tmp_path):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    with image.open("r+b") as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
        block_size = 1024 << int.from_bytes(superblock[24:28], "little")
        first_data_block = int.from_bytes(superblock[20:24], "little")
        gdt = (first_data_block + 1) * block_size
        stream.seek(gdt + 4)
        inode_bitmap_block = int.from_bytes(stream.read(4), "little")
        stream.seek(inode_bitmap_block * block_size)
        bitmap = bytearray(stream.read(block_size))
        bitmap[0] &= ~(1 << 1)  # inode 2
        stream.seek(inode_bitmap_block * block_size)
        stream.write(bitmap)
    with pytest.raises(NativeExt2Error, match="free-inode count|marked free"):
        NativeExt2ImageWriter(image)


def test_native_ext2_rejects_unsafe_paths_oversize_and_existing_dirty_image(
    tmp_path,
):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    before = image.read_bytes()

    with pytest.raises(NativeExt2Error, match="traversal"):
        writer.write_file("/../escape", b"bad")
    max_bytes = (12 + writer.block_size // 4) * writer.block_size
    with pytest.raises(NativeExt2Error, match="single-indirect"):
        writer.write_file("/too-large.bin", b"x" * (max_bytes + 1))
    assert image.read_bytes() == before


def test_native_ext2_refuses_ext4_and_unsupported_feature_profiles(tmp_path):
    ext4 = tmp_path / "filesystem.ext4"
    with ext4.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run(
        [MKE2FS, "-q", "-F", "-t", "ext4", str(ext4)],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(NativeExt2Error, match="feature flags|FILETYPE"):
        NativeExt2ImageWriter(ext4)

    unsupported = tmp_path / "indexed.ext2"
    _make_ext2(unsupported)
    # A directory-index feature bit is intentionally outside phase 1.
    with unsupported.open("r+b") as stream:
        stream.seek(1024 + 92)
        compat = int.from_bytes(stream.read(4), "little") | 0x20
        stream.seek(1024 + 92)
        stream.write(compat.to_bytes(4, "little"))
    with pytest.raises(NativeExt2Error, match="compatible feature"):
        NativeExt2ImageWriter(unsupported)


def test_native_ext2_routes_gui_and_universal_facade_without_debugfs(
    tmp_path, monkeypatch
):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    import mountfile.ext_debugfs_writer as ext_writer_module

    def no_debugfs_lookup(*_args, **_kwargs):
        raise AssertionError("debugfs discovery must not run for native ext2")

    monkeypatch.setattr(ext_writer_module, "_find_debugfs", no_debugfs_lookup)
    from mountfile.ext_debugfs_writer import Ext234ImageWriter

    gui_writer = Ext234ImageWriter(image)
    assert gui_writer.write_strategy == "native-ext2"
    gui_writer.write_file("/from-gui.txt", b"native")
    assert gui_writer.read_file("/from-gui.txt") == b"native"

    from mountfile.universal_fs_rw import UniversalFSRW

    with UniversalFSRW(image, offset=0, auto_backup=False) as filesystem:
        assert filesystem.info().write_strategy == "native-ext2"
        filesystem.mkdir("/from-api")
        filesystem.write_file("/from-api/file", b"API")
        assert filesystem.read("/from-api/file") == b"API"
        assert [entry.name for entry in filesystem.list("/from-api")] == ["file"]
        flush_result = filesystem.flush()
        assert "no external repair tool" in flush_result["fsck"]["skipped"]
    _assert_e2fsck_clean(image)


def test_native_ext2_staging_failure_does_not_modify_original(tmp_path, monkeypatch):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    before = image.read_bytes()

    def fail_commit(*_args, **_kwargs):
        raise OSError("injected commit failure")

    monkeypatch.setattr(writer, "_commit", fail_commit)
    with pytest.raises(OSError, match="injected commit failure"):
        writer.write_file("/not-committed.txt", b"staged only")
    assert image.read_bytes() == before


def test_large_file_high_size_is_not_misreported_as_empty(tmp_path):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    writer.write_file("/large.bin", b"x")
    inode = next(
        item["inode"] for item in writer.list_dir("/") if item["name"] == "large.bin"
    )

    with image.open("r+b") as stream:
        stream.seek(1024)
        sb = stream.read(1024)
        block_size = 1024 << int.from_bytes(sb[24:28], "little")
        inodes_per_group = int.from_bytes(sb[40:44], "little")
        inode_size = int.from_bytes(sb[88:90], "little")
        first_data_block = int.from_bytes(sb[20:24], "little")
        group = (inode - 1) // inodes_per_group
        index = (inode - 1) % inodes_per_group
        gdt = (first_data_block + 1) * block_size
        stream.seek(gdt + group * 32 + 8)
        inode_table = int.from_bytes(stream.read(4), "little")
        high_size_offset = inode_table * block_size + index * inode_size + 108
        stream.seek(high_size_offset)
        stream.write((1).to_bytes(4, "little"))

    assert (
        next(
            item["size"] for item in writer.list_dir("/") if item["name"] == "large.bin"
        )
        == (1 << 32) + 1
    )
    with pytest.raises(NativeExt2Error, match="single-indirect"):
        writer.read_file("/large.bin")


def test_native_ext2_rejects_double_indirect_file_blocks(tmp_path):
    image = tmp_path / "double-indirect.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    writer.write_file("/file.bin", b"direct")
    inode = next(
        item["inode"] for item in writer.list_dir("/") if item["name"] == "file.bin"
    )
    with image.open("r+b") as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
        block_size = 1024 << int.from_bytes(superblock[24:28], "little")
        inodes_per_group = int.from_bytes(superblock[40:44], "little")
        inode_size = int.from_bytes(superblock[88:90], "little")
        first_data_block = int.from_bytes(superblock[20:24], "little")
        group = (inode - 1) // inodes_per_group
        index = (inode - 1) % inodes_per_group
        gdt = (first_data_block + 1) * block_size
        stream.seek(gdt + group * 32 + 8)
        inode_table = int.from_bytes(stream.read(4), "little")
        inode_offset = inode_table * block_size + index * inode_size
        stream.seek(inode_offset + 40 + 13 * 4)
        stream.write((1).to_bytes(4, "little"))

    reader = NativeExt2ImageWriter(image)
    with pytest.raises(NativeExt2Error, match="double/triple"):
        reader.read_file("/file.bin")


def test_dirty_ext2_is_read_only_even_if_debugfs_is_available(tmp_path, monkeypatch):
    image = tmp_path / "dirty.ext2"
    _make_ext2(image)
    with image.open("r+b") as stream:
        stream.seek(1024 + 58)
        stream.write((2).to_bytes(2, "little"))
    before = image.read_bytes()

    import mountfile.ext_debugfs_writer as ext_writer_module

    monkeypatch.setattr(
        ext_writer_module, "_find_debugfs", lambda *_args, **_kwargs: ("debugfs", None)
    )
    from mountfile.ext_debugfs_writer import Ext234ImageWriter, ExtWriteError

    writer = Ext234ImageWriter(image)
    assert not writer.writable
    assert writer.write_strategy == "read-only-unsafe"
    with pytest.raises(ExtWriteError, match="safety gate"):
        writer.write_file("/must-not-write", b"blocked")
    assert image.read_bytes() == before


def test_universal_facade_passes_partition_bounds_to_native_ext2(tmp_path, monkeypatch):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition, size=8 * 1024 * 1024)
    prefix = b"PREFIX" * 89
    suffix = b"FOLLOWING-PARTITION" * 100
    image = tmp_path / "disk.img"
    with image.open("wb") as target, partition.open("rb") as source:
        target.write(prefix)
        shutil.copyfileobj(source, target)
        target.write(suffix)
    offset = len(prefix)
    before = image.read_bytes()

    import mountfile.firmware as firmware

    actual_size = partition.stat().st_size
    monkeypatch.setattr(
        firmware,
        "detect_volumes",
        lambda _image: [firmware.FoundVolume(offset, "ext2", size=actual_size - 4096)],
    )
    monkeypatch.setattr(firmware, "probe_filesystem", lambda _image, _offset: "ext2")
    from mountfile.universal_fs_rw import UniversalFSRW

    with pytest.raises(RuntimeError, match="selected partition bounds"):
        UniversalFSRW(image, offset=offset, auto_backup=False)
    assert image.read_bytes() == before


def test_write_engine_does_not_call_external_checker_for_native_ext2(
    tmp_path, monkeypatch
):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    import mountfile.write_engine as write_engine_module

    engine = write_engine_module.WriteEngine(
        image,
        read_only=False,
        backup=False,
        snapshot_dir=tmp_path / "snapshots",
        journal_path=tmp_path / "write-engine.jsonl",
    )
    try:
        assert engine.info().write_strategy == "native-ext2"

        def external_tool_must_not_run(*_args, **_kwargs):
            pytest.fail("native ext2 verification must not invoke e2fsck")

        monkeypatch.setattr(
            write_engine_module.shutil, "which", external_tool_must_not_run
        )
        report = engine.verify()
        checker = next(
            item for item in report["checks"] if item["name"] == "filesystem checker"
        )
        assert checker["ok"] is None
        assert "external e2fsck not run" in checker["detail"]
    finally:
        engine.close()
