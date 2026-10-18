import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fat32 import make_fat32_image
from mountfile.fat32 import Fat32
from mountfile.universal_fs_rw import Fat32RW, UniversalFSRW


def _open_fat_rw(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    return image, Fat32RW(image)


def test_facade_uses_package_detector_instead_of_missing_module(tmp_path):
    image = tmp_path / "not-an-image.bin"
    image.write_bytes(b"not a filesystem")
    with pytest.raises(RuntimeError, match="no filesystem detected"):
        UniversalFSRW(image, auto_backup=False)


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


def test_long_filename_rename_fails_closed_without_mutation(tmp_path):
    image, fs = _open_fat_rw(tmp_path)
    fs.close()
    with image.open("r+b") as image_file:
        Fat32(image_file).write_file(
            "/", "A LONG SOURCE NAME.BIN", b"evidence that must remain reachable"
        )
    fs = Fat32RW(image)
    try:
        payload = b"evidence that must remain reachable"
        source = "/A LONG SOURCE NAME.BIN"
        destination = "/A LONG DESTINATION NAME.BIN"

        with pytest.raises(NotImplementedError, match="only same-directory 8.3"):
            fs.rename(source, destination)

        assert fs.read(source) == payload
        assert fs.stat(destination) is None
    finally:
        fs.close()
