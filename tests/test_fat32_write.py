# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the FAT32 write extras: delete, rename, read-only provider.

Run:  python -m pytest tests/test_fat32_write.py -v
  or:  python tests/test_fat32_write.py
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_fat32 import make_fat32_image

from mountfile.extract import extract_path
from mountfile.fat32 import Fat32, Fat32Error, Fat32Provider


class TestDelete:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(self.path)
        self.fh = open(self.path, "r+b")
        self.fs = Fat32(self.fh)

    def teardown_method(self):
        self.fh.close()

    def _free(self):
        return sum(
            1 for n in range(2, self.fs.fat_size // 4) if self.fs._fat_get(n) == 0
        )

    def test_delete_file_frees_clusters(self):
        payload = os.urandom(5000)  # spans several clusters
        self.fs.write_file("/", "big.bin", payload)
        used = self._free()
        entry = self.fs.delete("/big.bin")
        assert entry.is_dir is False
        assert entry.size == len(payload)
        # Every data cluster of the file went back to the free pool.
        assert self._free() - used == math.ceil(len(payload) / self.fs.cluster_size)
        assert [e.name for e in self.fs.list_dir()] == []

    def test_delete_is_case_insensitive(self):
        self.fs.write_file("/", "Hello.TXT", b"data")
        self.fs.delete("/hello.txt")
        assert self.fs.list_dir() == []

    def test_delete_missing_raises(self):
        try:
            self.fs.delete("/nope.txt")
        except Fat32Error:
            return
        raise AssertionError("expected Fat32Error")

    def test_recursive_delete_removes_whole_tree(self):
        self.fs.mkdir("/", "A")
        self.fs.mkdir("/A", "B")
        self.fs.write_file("/A", "f1.bin", os.urandom(3000))
        self.fs.write_file("/A/B", "f2.bin", os.urandom(2000))
        free = self._free()
        try:
            self.fs.delete("/A")  # non-empty, not recursive
        except Fat32Error as e:
            assert "not empty" in str(e)
        else:
            raise AssertionError("expected Fat32Error for a non-empty dir")

        entry = self.fs.delete("/A", recursive=True)
        assert entry.is_dir is True
        assert self.fs.list_dir() == []
        # A + B + ceil(3000/512) + ceil(2000/512) clusters all came back.
        expected = 2 + math.ceil(3000 / 512) + math.ceil(2000 / 512)
        assert self._free() - free == expected

    def test_recursive_delete_keeps_name_reusable(self):
        self.fs.mkdir("/", "DIR")
        self.fs.write_file("/DIR", "inner.bin", os.urandom(1500))
        self.fs.delete("/DIR", recursive=True)
        self.fs.mkdir("/", "DIR")  # the name is free again
        self.fs.write_file("/DIR", "other.bin", b"ok")
        assert self.fs.read_file("/DIR/other.bin") == b"ok"

    def test_delete_empty_dir_but_not_full_dir(self):
        self.fs.mkdir("/", "SUB")
        self.fs.write_file("/SUB", "inner.txt", b"x" * 10)
        try:
            self.fs.delete("/SUB")
        except Fat32Error as e:
            assert "not empty" in str(e)
        else:
            raise AssertionError("expected Fat32Error for a non-empty dir")
        # Nothing may have been lost by the refused delete.
        assert self.fs.read_file("/SUB/inner.txt") == b"x" * 10
        self.fs.delete("/SUB/inner.txt")
        entry = self.fs.delete("/SUB")
        assert entry.is_dir is True
        assert self.fs.list_dir() == []

    def test_freed_space_is_reusable(self):
        payload = os.urandom(4000)
        self.fs.write_file("/", "a.bin", payload)
        after_write = self._free()
        self.fs.delete("/a.bin")
        assert self._free() > after_write  # space came back
        self.fs.write_file("/", "b.bin", payload)
        assert self.fs.read_file("/b.bin") == payload
        # b.bin fits in the space a.bin released: no growth of the used area.
        assert self._free() == after_write


class TestRename:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(self.path)
        self.fh = open(self.path, "r+b")
        self.fs = Fat32(self.fh)

    def teardown_method(self):
        self.fh.close()

    def test_rename_keeps_content(self):
        payload = os.urandom(3000)
        self.fs.write_file("/", "old.bin", payload)
        self.fs.rename("/old.bin", "new name.bin")  # LFN path too
        assert self.fs.read_file("/new name.bin") == payload
        assert [e.name for e in self.fs.list_dir()] == ["new name.bin"]

    def test_rename_short_name_drops_lfn(self):
        self.fs.write_file("/", "My Long Name.txt", b"x")
        self.fs.rename("/My Long Name.txt", "SHORT.TXT")
        assert self.fs.read_file("/SHORT.TXT") == b"x"
        entries = self.fs.list_dir()
        assert len(entries) == 1
        assert entries[0].name == "SHORT.TXT"

    def test_rename_dir_and_child_stays_reachable(self):
        self.fs.mkdir("/", "OLD")
        self.fs.write_file("/OLD", "f.txt", b"hi")
        self.fs.rename("/OLD", "NEW")
        assert self.fs.read_file("/NEW/f.txt") == b"hi"

    def test_rename_collision_rejected(self):
        self.fs.write_file("/", "a.txt", b"a")
        self.fs.write_file("/", "b.txt", b"b")
        try:
            self.fs.rename("/a.txt", "B.TXT")
        except Fat32Error:
            pass
        else:
            raise AssertionError("expected Fat32Error on a name collision")
        assert self.fs.read_file("/a.txt") == b"a"
        assert self.fs.read_file("/b.txt") == b"b"

    def test_no_duplicate_entries_after_many_renames(self):
        self.fs.write_file("/", "x.bin", b"payload")
        for i in range(15):
            self.fs.rename("/x.bin", "name_%d.bin" % i)
            self.fs.rename("/name_%d.bin" % i, "x.bin")
        assert self.fs.read_file("/x.bin") == b"payload"
        assert len(self.fs.list_dir()) == 1


class TestFat32Provider:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(self.path)
        with open(self.path, "r+b") as fh:
            fs = Fat32(fh)
            fs.mkdir("/", "DIR")
            fs.write_file("/DIR", "sub.bin", bytes(range(256)) * 200)
            fs.write_file("/", "tiny.txt", b"hello")

    def test_list_and_read_range(self):
        with Fat32Provider(self.path) as p:
            names = [i["name"] for i in p.list_dir("/")]
            assert names == ["DIR", "tiny.txt"]  # dirs first
            assert p.list_dir("/DIR")[0]["size"] == 51200
            assert p.read_file("/tiny.txt") == b"hello"
            # Unaligned windows across cluster boundaries.
            assert p.read_file_range("/DIR/sub.bin", 0, 16) == bytes(range(16))
            assert p.read_file_range("/DIR/sub.bin", 1023, 2) == bytes([255, 0])
            assert p.read_file_range("/DIR/sub.bin", 51200, 10) == b""
            assert len(p.read_file_range("/DIR/sub.bin", 0, 1 << 20)) == 51200
            assert p.file_size("/DIR/sub.bin") == 51200

    def test_provider_feeds_extract(self):
        out = os.path.join(self.tmp, "out")
        with Fat32Provider(self.path) as p:
            count = extract_path(p, "/DIR", out)
            assert count == 1
            with open(os.path.join(out, "DIR", "sub.bin"), "rb") as f:
                assert f.read() == bytes(range(256)) * 200
        # Whole volume: both entries, subdirectory recreated.
        out2 = os.path.join(self.tmp, "out2")
        with Fat32Provider(self.path) as p:
            count = extract_path(p, "/", out2)
        assert count == 2
        assert os.path.isfile(os.path.join(out2, "tiny.txt"))
        assert os.path.isfile(os.path.join(out2, "DIR", "sub.bin"))

    def test_provider_is_read_only_by_default(self):
        with Fat32Provider(self.path) as p:
            try:
                p.fs.write_file("/", "nope.bin", b"x")
            except Exception:
                pass
        with open(self.path, "rb") as f:  # still intact
            assert f.read(3) == b"\xeb\x58\x90"


if __name__ == "__main__":
    for cls in (TestDelete, TestRename, TestFat32Provider):
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                inst.setup_method()
                getattr(inst, name)()
                if hasattr(inst, "teardown_method"):
                    inst.teardown_method()
                print("PASS", name)
    print("ALL TESTS PASSED")
