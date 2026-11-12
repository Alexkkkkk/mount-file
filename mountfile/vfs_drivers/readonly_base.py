# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Общая read-only база для нативных драйверов ext/NTFS.

Открывает существующий нативный провайдер чтения (lazily, по имени профиля)
и наследует канонический контракт :class:`mountfile.vfs_contract.VFS`.
"""

from __future__ import annotations

from typing import BinaryIO, List, Optional, Union

from mountfile.vfs_contract import VFS, ErrorCode, Operation, VFSResult


class ReadOnlyNativeVFS(VFS):
    """Read-only операции поверх нативных провайдеров ext3/ext4 и NTFS.

    Конкретные адаптеры задают ``provider_property`` (``ext4`` или ``ntfs``);
    провайдер создаётся лениво при первом обращении, чтобы конструктор
    оставался дешёвым и пригодным для фабрики.
    """

    provider_property = None  # type: ignore[assignment]

    def __init__(
        self, image_path: str, offset: int = 0, volume_size: Optional[int] = None
    ):
        self.image_path = image_path
        self.offset = int(offset)
        self._volume_size = volume_size
        self._provider = None

    # -- provider lifecycle ---------------------------------------------
    @property
    def provider(self):
        if self._provider is None:
            self._provider = self._open_provider()
        return self._provider

    def _open_provider(self):
        from mountfile.native_vfs_adapters import Ext3VFS
        from mountfile.native_vfs_adapters import Ext4VFS as _Ext4Adapter
        from mountfile.native_vfs_adapters import NtfsVFS as _NtfsAdapter

        if self.provider_property == "ext4":
            # NativeExt4Image/NativeExt3Image distinguish themselves by probe;
            # the bounded read-only adapter covers both classic profiles.
            return _Ext4Adapter(
                self.image_path, self.offset, self._volume_size
            ).provider
        if self.provider_property == "ntfs":
            return _NtfsAdapter(
                self.image_path, self.offset, self._volume_size
            ).provider
        raise RuntimeError("unknown read-only provider property")

    def close(self) -> None:
        provider, self._provider = self._provider, None
        if provider is not None and hasattr(provider, "close"):
            provider.close()

    # -- contract --------------------------------------------------------
    def supported_operations(self) -> List[Operation]:
        return [Operation.LIST, Operation.STAT, Operation.READ]

    def _write_refusal(self, code: str, message: str) -> VFSResult:
        return VFSResult(success=False, error_code=code, message=message)

    def listdir(self, path: str) -> VFSResult:
        try:
            return VFSResult.ok(self.provider.list_dir(path or "/"))
        except (ValueError, RuntimeError) as exc:
            return VFSResult.fail(ErrorCode.INVALID_PATH, str(exc))

    def stat(self, path: str) -> VFSResult:
        try:
            return VFSResult.ok(self.provider.stat(path))
        except (ValueError, RuntimeError) as exc:
            return VFSResult.fail(ErrorCode.NOT_FOUND, str(exc))

    def read(self, path: str, offset: int = 0, size: int = -1) -> VFSResult:
        try:
            if size < 0:
                return VFSResult.ok(self.provider.read_file(path))
            return VFSResult.ok(
                self.provider.read_file_range(path, offset=offset, length=size)
            )
        except (ValueError, RuntimeError) as exc:
            return VFSResult.fail(ErrorCode.RANGE_OUT_OF_BOUNDS, str(exc))

    # -- unsupported writes (overridden by concrete adapters) ------------
    def write(
        self, path: str, data: Union[BinaryIO, bytes], offset: int = 0
    ) -> VFSResult:
        return self._write_refusal(
            "WRITE_NOT_SUPPORTED", "write is disabled for this profile"
        )

    def create(self, path: str, is_dir: bool = False) -> VFSResult:
        return self.write(path, b"")

    def delete(self, path: str, recursive: bool = False) -> VFSResult:
        return self.write(path, b"")

    def rename(self, old_path: str, new_path: str) -> VFSResult:
        return self.write(old_path, b"")
