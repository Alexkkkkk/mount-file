import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fat32 import make_fat32_image
from mountfile.universal_fs_rw import (
    Fat32RW,
    LoopMountRW,
    UniversalFSRW,
    _image_path_parent_name,
)


def test_image_paths_use_posix_semantics_independent_of_host_os():
    assert _image_path_parent_name("/ORIGINAL.BIN") == ("/", "ORIGINAL.BIN")
    assert _image_path_parent_name("/DIR/CHILD.BIN") == ("/DIR", "CHILD.BIN")
    assert _image_path_parent_name(r"\DIR\CHILD.BIN") == ("/DIR", "CHILD.BIN")


def test_fat_api_accepts_backslash_image_paths(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        payload = b"Windows path separator test"
        fs.mkdir("/SUBDIR")
        fs.write_file(r"\SUBDIR\ITEM.BIN", payload)
        assert fs.stat(r"\SUBDIR\ITEM.BIN") is not None
        assert fs.read(r"\SUBDIR\ITEM.BIN") == payload

        fs.rename(r"\SUBDIR\ITEM.BIN", r"\SUBDIR\RENAMED.BIN")
        assert fs.read(r"\SUBDIR\RENAMED.BIN") == payload
    finally:
        fs.close()


def _open_fat_rw(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    return image, Fat32RW(image)


def test_facade_uses_package_detector_instead_of_missing_module(tmp_path):
    image = tmp_path / "not-an-image.bin"
    image.write_bytes(b"not a filesystem")
    with pytest.raises(RuntimeError, match="no filesystem detected"):
        UniversalFSRW(image, auto_backup=False)


@pytest.mark.parametrize("fs_type", ["iso9660", "squashfs", "jffs2", "ubifs"])
def test_inherently_read_only_formats_fail_closed_before_mount(tmp_path, fs_type):
    image = tmp_path / "image.bin"
    image.write_bytes(b"read-only format placeholder")
    backend = LoopMountRW(image, 0, fs_type)
    info = backend.info()
    assert info.writable is False
    assert info.write_strategy == "read-only-format"
    with pytest.raises(RuntimeError, match="inherently read-only"):
        backend.write_file("/evidence.bin", b"must not mutate")


def test_loop_mount_normalizes_detector_display_names(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"placeholder")
    assert LoopMountRW(image, 0, "HFS+").fs_type == "hfsplus"
    assert LoopMountRW(image, 0, "FAT16").fs_type == "vfat"


def test_failed_overwrite_allocation_preserves_original_file(tmp_path, monkeypatch):
    _, fs = _open_fat_rw(tmp_path)
    try:
        original = b"original evidence" * 80
        fs.write_file("/ORIGINAL.BIN", original)
        before = fs.stat("/ORIGINAL.BIN")
        old_chain = fs._cluster_chain(before.inode)

        def fail_allocation(_count):
            raise RuntimeError("not enough free clusters")

        monkeypatch.setattr(fs, "_allocate_chain", fail_allocation)
        with pytest.raises(RuntimeError, match="not enough free clusters"):
            fs.write_file("/ORIGINAL.BIN", b"replacement" * 100)

        assert fs.read("/ORIGINAL.BIN") == original
        assert fs._cluster_chain(before.inode) == old_chain
    finally:
        fs.close()


def test_failed_overwrite_data_write_preserves_original_and_releases_new_chain(
    tmp_path, monkeypatch
):
    _, fs = _open_fat_rw(tmp_path)
    try:
        original = b"original evidence" * 80
        fs.write_file("/ORIGINAL.BIN", original)
        before = fs.stat("/ORIGINAL.BIN")
        old_chain = fs._cluster_chain(before.inode)
        next_free = fs._find_free_cluster(1)[0]

        def fail_write(_clusters, _data):
            raise OSError("simulated data write failure")

        monkeypatch.setattr(fs, "_write_chain", fail_write)
        with pytest.raises(OSError, match="simulated data write failure"):
            fs.write_file("/ORIGINAL.BIN", b"replacement" * 100)

        assert fs.read("/ORIGINAL.BIN") == original
        assert fs._cluster_chain(before.inode) == old_chain
        assert fs._fat_get(next_free) == 0
    finally:
        fs.close()


def test_new_file_with_no_directory_slot_releases_allocated_chain(tmp_path, monkeypatch):
    _, fs = _open_fat_rw(tmp_path)
    try:
        next_free = fs._find_free_cluster(1)[0]
        monkeypatch.setattr(fs, "_find_entry_slot", lambda _data, _count: None)

        with pytest.raises(RuntimeError, match="no free dir slot"):
            fs.write_file("/NEW.BIN", b"data that cannot be linked")

        assert fs._fat_get(next_free) == 0
        assert fs.stat("/NEW.BIN") is None
    finally:
        fs.close()


def test_rename_preserves_multicluster_file_chain(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        payload = bytes((i * 17) % 256 for i in range(4096))
        fs.write_file("/SOURCE.BIN", payload)
        before = fs.stat("/SOURCE.BIN")
        old_chain = fs._cluster_chain(before.inode)

        fs.rename("/SOURCE.BIN", "/RENAMED.BIN")

        after = fs.stat("/RENAMED.BIN")
        assert after is not None
        assert fs.stat("/SOURCE.BIN") is None
        assert fs._cluster_chain(after.inode) == old_chain
        assert fs.read("/RENAMED.BIN") == payload
    finally:
        fs.close()


def test_long_filename_creation_roundtrips_and_uses_unique_short_aliases(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        files = {
            "/A LONG SOURCE NAME.BIN": b"first evidence file",
            "/A LONG SOURCE NAME COPY.BIN": b"second evidence file",
            "/ABCDEFGHIJKLM": b"exactly thirteen characters",
            "/Evidence 📄.bin": b"unicode evidence file",
        }
        for path, payload in files.items():
            fs.write_file(path, payload)

        raw = fs._read_dir_raw(fs.root_cluster)
        short_aliases = []
        for path, payload in files.items():
            name = Path(path).name
            entry = next(item for item in fs.list("/") if item.name == name)
            index = fs._find_entry_index(raw, entry, "/")
            assert index is not None
            short_aliases.append(raw[index : index + 11])
            assert fs.read(path) == payload

        assert len(set(short_aliases)) == len(files)
    finally:
        fs.close()


def test_delete_long_filename_tombstones_all_slots_and_allows_reuse(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        removed = "/A LONG FILE TO REMOVE.BIN"
        retained = "/A DIFFERENT LONG FILE.BIN"
        replacement = "/A REPLACEMENT LONG FILE.BIN"
        fs.write_file(removed, b"removed")
        fs.write_file(retained, b"retained")

        raw_before = fs._read_dir_raw(fs.root_cluster)
        entry = next(item for item in fs.list("/") if item.name == Path(removed).name)
        short_index = fs._find_entry_index(raw_before, entry, "/")
        assert short_index is not None
        start = short_index
        while start >= 32 and raw_before[start - 32 + 11] == fs.ATTR_LFN:
            start -= 32
        deleted_slots = list(range(start, short_index + 32, 32))

        fs.delete(removed)
        raw_after = fs._read_dir_raw(fs.root_cluster)
        assert all(raw_after[offset] == 0xE5 for offset in deleted_slots)

        fs.write_file(replacement, b"replacement")
        assert fs.read(retained) == b"retained"
        assert fs.read(replacement) == b"replacement"
    finally:
        fs.close()


def test_mkdir_writes_valid_dot_entries(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        fs.mkdir("/SUBDIR")
        directory = fs.stat("/SUBDIR")
        assert directory is not None
        raw = fs._read_dir_raw(directory.inode)
        assert raw[:11] == b"." + b" " * 10
        assert raw[32:43] == b".." + b" " * 9
    finally:
        fs.close()


def test_long_filename_rename_fails_closed_without_mutation(tmp_path):
    _, fs = _open_fat_rw(tmp_path)
    try:
        payload = b"evidence that must remain reachable"
        source = "/A LONG SOURCE NAME.BIN"
        destination = "/A LONG DESTINATION NAME.BIN"
        fs.write_file(source, payload)

        with pytest.raises(NotImplementedError, match="only same-directory 8.3"):
            fs.rename(source, destination)

        assert fs.read(source) == payload
        assert fs.stat(destination) is None
    finally:
        fs.close()
