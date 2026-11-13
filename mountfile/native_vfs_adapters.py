# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only VFS-адаптеры ext3/ext4 и NTFS (issue #388).

Запись сознательно возвращает fail-closed результат с типизированным кодом,
пока не реализованы журнальные writer'ы (issues #385 и #386).
"""

from __future__ import annotations

from typing import BinaryIO, List, Optional, Union

from mountfile.ext3_reader import NativeExt3Image, NativeExt4Image
from mountfile.ntfs_reader import NativeNtfsMetadataImage
from mountfile.safety import RefusalError
from mountfile.vfs_contract import VFS, ErrorCode, Operation, VFSResult


class _ReadOnlyNativeVFS(VFS):
    """Общая реализация read-only операций поверх нативных провайдеров."""

    provider_class = None  # type: ignore[assignment]

    def __init__(
        self, image_path: str, offset: int = 0, volume_size: Optional[int] = None
    ):
        self.provider = self.provider_class(
            image_path, offset=offset, volume_size=volume_size
        )

    def supported_operations(self) -> List[Operation]:
        return [Operation.LIST, Operation.STAT, Operation.READ]

    def _write_refusal(self) -> VFSResult:
        return VFSResult.fail(
            ErrorCode.WRITE_NOT_SUPPORTED,
            "native write is disabled until journal-aware writer lands",
        )

    # -- read-only ------------------------------------------------------
    def listdir(self, path: str) -> VFSResult:
        try:
            return VFSResult.ok(self.provider.list_dir(path or "/"))
        except (RefusalError, ValueError) as exc:
            return VFSResult.fail(ErrorCode.INVALID_PATH, str(exc))

    def stat(self, path: str) -> VFSResult:
        try:
            return VFSResult.ok(self.provider.stat(path))
        except (RefusalError, ValueError) as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def read(self, path: str, offset: int = 0, size: int = -1) -> VFSResult:
        try:
            if size < 0:
                return VFSResult.ok(self.provider.read_file(path))
            return VFSResult.ok(
                self.provider.read_file_range(path, offset=offset, length=size)
            )
        except (RefusalError, ValueError) as exc:
            return VFSResult.fail(ErrorCode.RANGE_OUT_OF_BOUNDS, str(exc))

    # -- unsupported ----------------------------------------------------
    def write(
        self, path: str, data: Union[BinaryIO, bytes], offset: int = 0
    ) -> VFSResult:
        return self._write_refusal()

    def create(self, path: str, is_dir: bool = False) -> VFSResult:
        return self._write_refusal()

    def delete(self, path: str, recursive: bool = False) -> VFSResult:
        return self._write_refusal()

    def rename(self, old_path: str, new_path: str) -> VFSResult:
        return self._write_refusal()


class Ext3VFS(_ReadOnlyNativeVFS):
    provider_class = NativeExt3Image


class Ext4VFS(_ReadOnlyNativeVFS):
    provider_class = NativeExt4Image


class NtfsVFS(_ReadOnlyNativeVFS):
    provider_class = NativeNtfsMetadataImage
