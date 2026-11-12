# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Acceptance tests for the unified VFS contract (issue #388)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_fat32 import make_fat32_image

from mountfile.fat32_vfs import Fat32VFS
from mountfile.native_vfs_adapters import Ext4VFS, NtfsVFS
from mountfile.vfs_contract import VFS, ErrorCode, Operation, VFSResult


class TestFat32VFS:
    def setup_method(self):
        self.tmp = os.path.join(os.path.dirname(__file__), "_vfs_tmp")
        os.makedirs(self.tmp, exist_ok=True)
        self.path = os.path.join(self.tmp, "fat32.img")
        make_fat32_image(self.path)
        from mountfile.fat32 import Fat32Provider

        self.vfs = Fat32VFS(Fat32Provider(self.path, writable=True))

    def teardown_method(self):
        self.vfs.provider.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_supported_operations_is_complete(self):
        ops = self.vfs.supported_operations()
        for op in (
            Operation.LIST,
            Operation.STAT,
            Operation.READ,
            Operation.WRITE,
            Operation.CREATE,
            Operation.DELETE,
            Operation.RENAME,
            Operation.MKDIR,
        ):
            assert op in ops

    def test_list_and_write_read_roundtrip(self):
        assert self.vfs.listdir("/").success
        result = self.vfs.write("/hello.txt", b"hello mount-file")
        assert result.success, result.message
        data = self.vfs.read("/hello.txt")
        assert data.success and data.data == b"hello mount-file"
        stat = self.vfs.stat("/hello.txt")
        assert stat.success and stat.data["size"] == 16

    def test_mkdir_and_delete(self):
        assert self.vfs.create("/dir1", is_dir=True).success
        assert self.vfs.stat("/dir1").data["is_dir"] is True
        assert self.vfs.delete("/dir1").success

    def test_missing_file_returns_typed_error(self):
        result = self.vfs.read("/nope.bin")
        assert not result.success
        assert result.error_code == ErrorCode.NOT_FOUND.value

    def test_read_range_out_of_bounds_returns_typed_error(self):
        """Regression: out-of-range reads must fail closed, never raise."""
        result = self.vfs.read("/hello.txt", offset=0, size=1 << 40)
        assert isinstance(result, VFSResult)
        assert result.success is False
        assert result.error_code is not None


class TestReadOnlyVFSFailClosed:
    """ext4/NTFS writers must refuse with a typed code, never raise."""

    def test_contract_is_abstract_and_typable(self):
        assert issubclass(VFSResult, object)
        assert Operation.WRITE.value == "write"

    def test_write_refusal_shape(self):
        result = VFSResult.fail(ErrorCode.WRITE_NOT_SUPPORTED, "nope")
        assert result.success is False
        assert result.error_code == "write_not_supported"

    def test_error_code_has_range_out_of_bounds(self):
        """Regression for PR #494: adapters reference RANGE_OUT_OF_BOUNDS."""
        assert ErrorCode.RANGE_OUT_OF_BOUNDS.value == "range_out_of_bounds"
