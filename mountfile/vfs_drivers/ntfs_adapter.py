# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""NTFS-драйвер VFS с жёсткой блокировкой записи (issue #386).

Запись возвращает fail-closed отказ с кодом ``WRITE_DISABLED_LOGFILE_UNSAFE``,
пока не реализовано восстановление ``$LogFile`` (issue #386).
"""

from __future__ import annotations

from typing import BinaryIO

from mountfile.native_write import (
    NativeWriteDisabled,
    NativeWriteError,
    NativeWriteUnsafe,
    NtfsTransactionalWriter,
    native_write_enabled,
)
from mountfile.vfs_contract import VFSResult
from mountfile.vfs_drivers.readonly_base import ReadOnlyNativeVFS


class NtfsVFS(ReadOnlyNativeVFS):
    """Read NTFS by default; write only with explicit native opt-in."""

    provider_property = "ntfs"

    def supported_operations(self) -> list[Operation]:  # noqa: F821
        from mountfile.vfs_contract import Operation

        operations = [Operation.LIST, Operation.STAT, Operation.READ]
        if native_write_enabled():
            operations.append(Operation.WRITE)
        return operations

    def write(self, path: str, data: BinaryIO | bytes, offset: int = 0) -> VFSResult:
        try:
            if not isinstance(data, bytes):
                data = data.read()
            NtfsTransactionalWriter().write_file(self.image_path, path, data, offset)
            return VFSResult.ok()
        except NativeWriteDisabled as exc:
            return VFSResult.fail("WRITE_DISABLED_LOGFILE_UNSAFE", str(exc))
        except (NativeWriteUnsafe, NativeWriteError) as exc:
            return VFSResult.fail("WRITE_FAILED", str(exc))
