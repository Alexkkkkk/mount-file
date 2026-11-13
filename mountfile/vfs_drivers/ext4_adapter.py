# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""ext4-драйвер VFS с жёсткой блокировкой записи (issue #385).

Запись нельзя включать до прохождения тестов на восстановление и сохранение
хеша образа: адаптер возвращает fail-closed отказ с кодом
``WRITE_DISABLED_JOURNAL_UNSAFE``, пока не реализован JBD2 replay.
"""

from __future__ import annotations

from typing import BinaryIO

from mountfile.native_write import (
    Ext4TransactionalWriter,
    NativeWriteDisabled,
    NativeWriteError,
    NativeWriteUnsafe,
    native_write_enabled,
)
from mountfile.vfs_contract import VFSResult
from mountfile.vfs_drivers.readonly_base import ReadOnlyNativeVFS


class Ext4VFS(ReadOnlyNativeVFS):
    """Read ext3/ext4 by default; write only with explicit native opt-in."""

    provider_property = "ext4"

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
            Ext4TransactionalWriter().write_file(self.image_path, path, data, offset)
            return VFSResult.ok()
        except NativeWriteDisabled as exc:
            return VFSResult.fail("WRITE_DISABLED_JOURNAL_UNSAFE", str(exc))
        except (NativeWriteUnsafe, NativeWriteError) as exc:
            return VFSResult.fail("WRITE_FAILED", str(exc))
