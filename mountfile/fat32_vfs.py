# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Адаптер :class:`VFS` поверх существующего write-движка FAT32 (issue #388)."""

from __future__ import annotations

from typing import BinaryIO, List, Union

from mountfile.fat32 import Fat32Error, Fat32Provider
from mountfile.vfs_contract import VFS, ErrorCode, Operation, VFSResult


class Fat32VFS(VFS):
    """Обёртка над :class:`mountfile.fat32.Fat32Provider` в контракте VFS."""

    def __init__(self, provider: Fat32Provider):
        self.provider = provider

    def supported_operations(self) -> List[Operation]:
        return [
            Operation.LIST,
            Operation.STAT,
            Operation.READ,
            Operation.WRITE,
            Operation.CREATE,
            Operation.DELETE,
            Operation.RENAME,
            Operation.MKDIR,
        ]

    def listdir(self, path: str) -> VFSResult:
        try:
            return VFSResult.ok(self.provider.list_dir(path or "/"))
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def stat(self, path: str) -> VFSResult:
        try:
            entry = self.provider._entry(path)
            return VFSResult.ok(
                {
                    "path": path,
                    "name": entry.name,
                    "is_dir": entry.is_dir,
                    "size": 0 if entry.is_dir else int(entry.size),
                }
            )
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def read(self, path: str, offset: int = 0, size: int = -1) -> VFSResult:
        try:
            if size < 0:
                return VFSResult.ok(self.provider.read_file(path))
            return VFSResult.ok(
                self.provider.read_file_range(path, offset=offset, length=size)
            )
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def write(
        self, path: str, data: Union[BinaryIO, bytes], offset: int = 0
    ) -> VFSResult:
        if offset:
            return VFSResult.fail(
                ErrorCode.OPERATION_NOT_SUPPORTED,
                "sparse/partial FAT32 writes are not supported yet",
            )
        payload = data.read() if hasattr(data, "read") else bytes(data)
        directory, _, filename = path.replace("\\", "/").rpartition("/")
        try:
            self.provider.fs.write_file(directory or "/", filename, payload)
            return VFSResult.ok()
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.IO_ERROR, str(exc))

    def create(self, path: str, is_dir: bool = False) -> VFSResult:
        directory, _, name = path.replace("\\", "/").rpartition("/")
        try:
            if is_dir:
                self.provider.fs.mkdir(directory or "/", name)
            else:
                self.provider.fs.write_file(directory or "/", name, b"")
            return VFSResult.ok()
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.ALREADY_EXISTS, str(exc))

    def delete(self, path: str, recursive: bool = False) -> VFSResult:
        try:
            self.provider.fs.delete(path, recursive=recursive)
            return VFSResult.ok()
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def rename(self, old_path: str, new_path: str) -> VFSResult:
        try:
            self.provider.fs.rename(old_path, new_path)
            return VFSResult.ok()
        except Fat32Error as exc:
            return VFSResult.fail(ErrorCode.IO_ERROR, str(exc))
