# SPDX-License-Identifier: MIT
"""Regression coverage for the in-process, limited-profile ext2 writer."""

import json
import os
import shutil
import subprocess
import sys

import pytest

import mountfile.ext2_native as ext2_native
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


def _set_inode_flags(path, inode_number, flags):
    with open(path, "r+b") as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
        block_size = 1024 << int.from_bytes(superblock[24:28], "little")
        first_data_block = int.from_bytes(superblock[20:24], "little")
        inodes_per_group = int.from_bytes(superblock[40:44], "little")
        inode_size = int.from_bytes(superblock[88:90], "little")
        group, index = divmod(inode_number - 1, inodes_per_group)
        gdt = (first_data_block + 1) * block_size
        stream.seek(gdt + group * 32 + 8)
        inode_table = int.from_bytes(stream.read(4), "little")
        stream.seek(inode_table * block_size + index * inode_size + 32)
        stream.write(flags.to_bytes(4, "little"))


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


def test_native_ext2_directory_grows_through_single_indirect_block(
    tmp_path, monkeypatch
):
    image = tmp_path / "indirect-directory.ext2"
    _make_ext2(image, size=16 * 1024 * 1024, block_size=1024)
    writer = NativeExt2ImageWriter(image)
    names = [f"{index:03d}-" + "x" * 235 for index in range(60)]

    def populate_many(fs, file_names):
        fs.mkdir("/crowded")
        for name in file_names:
            fs.write_file("/crowded/" + name, b"")

    from mountfile.ext2_native import _Ext2FS

    monkeypatch.setattr(_Ext2FS, "populate_many_for_test", populate_many, raising=False)
    writer._mutate("populate_many_for_test", names)

    entries = writer.list_dir("/crowded")
    assert {entry["name"] for entry in entries} == set(names)
    _, raw, _ = writer._fs._resolve("/crowded")
    assert raw is not None
    assert writer._fs._u32(raw, 40 + 12 * 4) != 0
    assert len(writer._fs._inode_blocks(raw, allow_indirect=True)) > 12
    _assert_e2fsck_clean(image)

    def remove_many(fs, file_names):
        for name in file_names:
            fs.delete("/crowded/" + name)
        fs.delete("/crowded")

    monkeypatch.setattr(_Ext2FS, "remove_many_for_test", remove_many, raising=False)
    writer._mutate("remove_many_for_test", names)
    assert "crowded" not in {entry["name"] for entry in writer.list_dir("/")}
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


def test_native_ext2_grows_and_deletes_directory_through_single_indirect_block(
    tmp_path, monkeypatch
):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition, size=8 * 1024 * 1024, block_size=1024)
    prefix = b"before-selected-extent" * 17
    suffix = b"after-selected-extent" * 17
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + partition.read_bytes() + suffix)

    writer = NativeExt2ImageWriter(
        image, offset=len(prefix), volume_size=partition.stat().st_size
    )
    original_free_space = writer.free_space()
    writer.mkdir("/grown")
    entry_size = (8 + 255 + 3) & ~3
    entries_per_block = (writer.block_size - 24) // entry_size
    names = [f"{index:04d}-" + "x" * 250 for index in range(12 * entries_per_block + 4)]
    direct_names = names[: 12 * entries_per_block]
    for name in direct_names:
        writer.write_file("/grown/" + name, b"")

    before_transition = image.read_bytes()
    original_write_block = ext2_native._Ext2FS._write_block
    staged_writes = 0

    def fail_pointer_table_write(fs, block, data):
        nonlocal staged_writes
        staged_writes += 1
        if staged_writes == 2:
            raise OSError("injected indirect-table write failure")
        return original_write_block(fs, block, data)

    with monkeypatch.context() as patch:
        patch.setattr(ext2_native._Ext2FS, "_write_block", fail_pointer_table_write)
        with pytest.raises(OSError, match="indirect-table"):
            writer.write_file("/grown/" + names[len(direct_names)], b"")
    assert image.read_bytes() == before_transition

    for name in names[len(direct_names) :]:
        writer.write_file("/grown/" + name, b"")

    directory_inode = next(
        item["inode"] for item in writer.list_dir("/") if item["name"] == "grown"
    )
    raw = writer._fs._read_inode(directory_inode)
    single_indirect = writer._fs._u32(raw, 40 + 12 * 4)
    assert single_indirect
    directory_blocks = writer._fs._inode_blocks(raw, allow_indirect=True)
    assert len(directory_blocks) == 14
    pointers = writer._fs._read_block(single_indirect)
    assert writer._fs._u32(pointers, 0) == directory_blocks[12]
    assert writer._fs._u32(pointers, 4) == directory_blocks[13]
    assert not any(pointers[8:])
    assert writer._fs._u32(raw, 28) == 15 * (writer.block_size // 512)
    assert writer.read_file("/grown/" + names[-1]) == b""

    writer.mkdir("/right")
    writer.rename("/grown", "/right/moved")
    assert "grown" not in {entry["name"] for entry in writer.list_dir("/")}
    moved_entries = writer.list_dir("/right/moved")
    assert {entry["name"] for entry in moved_entries} == set(names)
    right_inode = next(
        entry["inode"] for entry in writer.list_dir("/") if entry["name"] == "right"
    )
    moved_inode = next(
        entry["inode"]
        for entry in writer.list_dir("/right")
        if entry["name"] == "moved"
    )
    dotdot = writer._fs._lookup_child(moved_inode, b"..")
    assert dotdot is not None and dotdot["inode"] == right_inode
    assert writer.read_file("/right/moved/" + names[-1]) == b""
    with image.open("rb") as stream:
        stream.seek(len(prefix))
        moved_extent = stream.read(partition.stat().st_size)
    moved_check_image = tmp_path / "partition-after-directory-move.ext2"
    moved_check_image.write_bytes(moved_extent)
    _assert_e2fsck_clean(moved_check_image)

    for name in names:
        writer.delete("/right/moved/" + name)
    writer.delete("/right/moved")
    writer.delete("/right")
    assert writer.free_space() == original_free_space
    assert "right" not in {entry["name"] for entry in writer.list_dir("/")}
    with image.open("rb") as stream:
        assert stream.read(len(prefix)) == prefix
        stream.seek(len(prefix) + partition.stat().st_size)
        assert stream.read() == suffix
        stream.seek(len(prefix))
        checked_extent = stream.read(partition.stat().st_size)
    check_image = tmp_path / "partition-after-directory-growth.ext2"
    check_image.write_bytes(checked_extent)
    _assert_e2fsck_clean(check_image)


@pytest.mark.parametrize("corruption", ["double", "unused-slot"])
def test_native_ext2_rejects_corrupt_single_indirect_directory_map(
    tmp_path, corruption
):
    image = tmp_path / "corrupt-directory.ext2"
    _make_ext2(image, size=8 * 1024 * 1024, block_size=1024)
    writer = NativeExt2ImageWriter(image)
    writer.mkdir("/grown")
    entry_size = (8 + 255 + 3) & ~3
    entries_per_block = (writer.block_size - 24) // entry_size
    for index in range(12 * entries_per_block + 1):
        writer.write_file(
            "/grown/%04d-" % index + "x" * 250,
            b"",
        )

    directory_inode = next(
        item["inode"] for item in writer.list_dir("/") if item["name"] == "grown"
    )
    raw = writer._fs._read_inode(directory_inode)
    indirect_block = writer._fs._u32(raw, 40 + 12 * 4)
    with image.open("r+b") as stream:
        if corruption == "double":
            stream.seek(writer._fs._block_offset(indirect_block))
            stream.write((indirect_block).to_bytes(4, "little"))
        else:
            stream.seek(writer._fs._block_offset(indirect_block) + 8)
            stream.write((writer._fs._u32(raw, 40)).to_bytes(4, "little"))

    reopened = NativeExt2ImageWriter(image)
    with pytest.raises(NativeExt2Error, match="indirect (pointer )?block|duplicate"):
        reopened.list_dir("/grown")


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


def test_native_ext2_refuses_mutation_inside_flagged_directories(tmp_path):
    image = tmp_path / "flagged-directory.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    writer.mkdir("/locked")
    locked_inode = next(
        entry["inode"] for entry in writer.list_dir("/") if entry["name"] == "locked"
    )

    _set_inode_flags(image, 2, 0x10)  # EXT2_IMMUTABLE_FL on the root directory.
    before = image.read_bytes()
    with pytest.raises(NativeExt2Error, match="flagged ext2 directories"):
        writer.write_file("/blocked-at-root", b"must not be committed")
    assert image.read_bytes() == before

    _set_inode_flags(image, 2, 0)
    _set_inode_flags(image, locked_inode, 0x10)
    before = image.read_bytes()
    with pytest.raises(NativeExt2Error, match="flagged ext2 directories"):
        writer.write_file("/locked/blocked", b"must not be committed")
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


def test_native_ext2_rolls_back_and_verifies_restored_extent_hash(
    tmp_path, monkeypatch
):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    before = image.read_bytes()
    hash_source = writer._hash_source
    hash_calls = 0

    def reject_committed_hash():
        nonlocal hash_calls
        hash_calls += 1
        digest = hash_source()
        if hash_calls == 3:
            return b"\x00" * len(digest)
        return digest

    monkeypatch.setattr(writer, "_hash_source", reject_committed_hash)
    with pytest.raises(
        NativeExt2Error, match="committed ext2 extent failed SHA-256 verification"
    ):
        writer.write_file("/not-committed.txt", b"staged only")

    assert hash_calls == 4
    assert image.read_bytes() == before
    assert not image.with_name(image.name + ".mountfile-ext2-recovery.json").exists()
    assert not list(tmp_path.glob(".filesystem.ext2.mountfile-ext2-recovery-*.bin"))


def test_native_ext2_short_rollback_write_is_not_claimed_as_restored(
    tmp_path, monkeypatch
):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    before = image.read_bytes()
    hash_source = writer._hash_source
    hash_calls = 0
    image_write_opens = 0
    original_open = open

    def reject_committed_hash():
        nonlocal hash_calls
        hash_calls += 1
        digest = hash_source()
        if hash_calls == 3:
            return b"\x00" * len(digest)
        return digest

    class ShortWriteStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, _data):
            return 0

    def short_rollback_open(path, mode="r", *args, **kwargs):
        nonlocal image_write_opens
        stream = original_open(path, mode, *args, **kwargs)
        if str(path) == str(image) and mode == "r+b":
            image_write_opens += 1
            if image_write_opens == 3:
                return ShortWriteStream(stream)
        return stream

    monkeypatch.setattr(writer, "_hash_source", reject_committed_hash)
    monkeypatch.setattr(ext2_native, "open", short_rollback_open, raising=False)
    with pytest.raises(
        NativeExt2Error,
        match="commit and rollback both failed.*short write while restoring ext2 extent",
    ):
        writer.write_file("/not-restored.txt", b"staged only")

    assert image_write_opens == 3
    assert hash_calls == 4
    assert image.read_bytes() != before
    marker = image.with_name(image.name + ".mountfile-ext2-recovery.json")
    assert marker.is_file()
    recovery = json.loads(marker.read_text(encoding="utf-8"))
    snapshot = image.with_name(recovery["snapshot"])
    assert snapshot.is_file()
    assert snapshot.read_bytes() == before

    # External edits after the failure must not be overwritten by recovery.
    pending_image = image.read_bytes()
    pending_stat = image.stat()
    with image.open("r+b") as stream:
        stream.seek(0)
        stream.write(bytes([pending_image[0] ^ 1]))
    monkeypatch.undo()
    with pytest.raises(NativeExt2Error, match="image changed since ext2 failure"):
        NativeExt2ImageWriter(image)
    assert marker.is_file()

    # If the exact post-failure image is restored, a later writer open can
    # restore the snapshot and remove the marker.
    with image.open("r+b") as stream:
        stream.seek(0)
        stream.write(pending_image[:1])
    os.utime(image, ns=(pending_stat.st_atime_ns, recovery["mtime_ns"]))

    # Simulate a process failure after the restored bytes are fsynced but
    # before the marker is removed; the next open must finish cleanup safely.
    original_unlink = os.unlink

    def fail_marker_unlink(path):
        if str(path) == str(marker):
            raise OSError("injected crash before recovery marker cleanup")
        return original_unlink(path)

    monkeypatch.setattr(ext2_native.os, "unlink", fail_marker_unlink)
    with pytest.raises(NativeExt2Error, match="automatic recovery failed"):
        NativeExt2ImageWriter(image)
    assert image.read_bytes() == before
    assert marker.is_file()
    monkeypatch.undo()

    NativeExt2ImageWriter(image)
    assert image.read_bytes() == before
    assert not marker.exists()
    assert not snapshot.exists()


def test_native_ext2_recovers_after_process_dies_mid_commit(tmp_path):
    partition = tmp_path / "partition.ext2"
    _make_ext2(partition, size=8 * 1024 * 1024)
    filesystem = partition.read_bytes()
    prefix = b"before-partition" * 128
    suffix = b"after-partition" * 128
    offset = 4096
    image = tmp_path / "disk.img"
    image.write_bytes(b"P" * (offset - len(prefix)) + prefix + filesystem + suffix)
    volume_offset = offset
    before = image.read_bytes()
    crashed_temp = tmp_path / "crashed-temp"
    crashed_temp.mkdir()
    crash_script = f"""
import os
import tempfile
from mountfile import ext2_native
from mountfile.ext2_native import NativeExt2ImageWriter

tempfile.tempdir = {str(crashed_temp)!r}
image = {str(image)!r}
real_open = open
image_write_opens = 0

class CrashDuringWrite:
    def __init__(self, stream):
        self.stream = stream
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def write(self, data):
        count = max(1, len(data) // 2)
        written = self.stream.write(data[:count])
        self.stream.flush()
        os.fsync(self.stream.fileno())
        os._exit(73)

def crash_open(path, mode='r', *args, **kwargs):
    global image_write_opens
    stream = real_open(path, mode, *args, **kwargs)
    if os.fspath(path) == image and mode == 'r+b':
        image_write_opens += 1
        if image_write_opens == 2:
            return CrashDuringWrite(stream)
    return stream

ext2_native.open = crash_open
writer = NativeExt2ImageWriter(image, offset={volume_offset}, volume_size={len(filesystem)})
writer.write_file('/interrupted.txt', b'this transaction must roll back')
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_script],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    assert image.read_bytes() != before

    marker = image.with_name(image.name + ".mountfile-ext2-recovery.json")
    assert marker.is_file()
    recovery = json.loads(marker.read_text(encoding="utf-8"))
    assert recovery["format"] == 2
    assert recovery["phase"] == "commit_in_progress"
    snapshot = image.with_name(recovery["snapshot"])
    assert snapshot.read_bytes() == filesystem

    writer = NativeExt2ImageWriter(
        image, offset=volume_offset, volume_size=len(filesystem)
    )
    assert image.read_bytes() == before
    assert not marker.exists()
    assert not snapshot.exists()
    assert "interrupted.txt" not in {entry["name"] for entry in writer.list_dir("/")}
    recovered_partition = tmp_path / "recovered.ext2"
    recovered_partition.write_bytes(
        image.read_bytes()[offset : offset + len(filesystem)]
    )
    _assert_e2fsck_clean(recovered_partition)


def test_native_ext2_blocks_writes_for_dangling_recovery_marker_symlink(tmp_path):
    image = tmp_path / "filesystem.ext2"
    _make_ext2(image, size=8 * 1024 * 1024)
    before = image.read_bytes()
    marker = image.with_name(image.name + ".mountfile-ext2-recovery.json")
    try:
        marker.symlink_to(tmp_path / "missing-marker-target")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(NativeExt2Error, match="recovery marker is a symbolic link"):
        NativeExt2ImageWriter(image)

    assert marker.is_symlink()
    assert image.read_bytes() == before


def test_native_ext2_rejects_concurrent_change_before_commit(tmp_path, monkeypatch):
    image = tmp_path / "concurrent-change.ext2"
    _make_ext2(image)
    writer = NativeExt2ImageWriter(image)
    original_copy_extent = writer._copy_extent
    calls = 0

    def mutate_after_staging(destination):
        nonlocal calls
        digest = original_copy_extent(destination)
        calls += 1
        if calls == 1:
            with image.open("r+b") as stream:
                stream.seek(512)
                value = stream.read(1)
                stream.seek(512)
                stream.write(bytes([value[0] ^ 0x01]))
        return digest

    monkeypatch.setattr(writer, "_copy_extent", mutate_after_staging)
    with pytest.raises(NativeExt2Error, match="changed concurrently"):
        writer.write_file("/not-committed.txt", b"must not be committed")
    assert calls == 1
    assert not any(
        entry["name"] == "not-committed.txt" for entry in writer.list_dir("/")
    )


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
