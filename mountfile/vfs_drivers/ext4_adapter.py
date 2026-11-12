# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""ext4-драйвер VFS с жёсткой блокировкой записи (issue #385).

Запись нельзя включать до прохождения тестов на восстановление и сохранение
хеша образа: адаптер возвращает fail-closed отказ с кодом
``WRITE_DISABLED_JOURNAL_UNSAFE``, пока не реализован JBD2 replay.
"""

from __future__ import annotations

from typing import BinaryIO, List, Union

from mountfile.vfs_contract import VFSResult
from mountfile.vfs_drivers.readonly_base import ReadOnlyNativeVFS


class Ext4VFS(ReadOnlyNativeVFS):
    """Read-only драйвер ext3/ext4; запись явно заблокирована до JBD2 replay."""

    provider_property = "ext4"

    def supported_operations(self) -> List["Operation"]:  # noqa: F821
        from mountfile.vfs_contract import Operation

        return [Operation.LIST, Operation.STAT, Operation.READ]

    def write(
        self, path: str, data: Union[BinaryIO, bytes], offset: int = 0
    ) -> VFSResult:
        return VFSResult(
            success=False,
            error_code="WRITE_DISABLED_JOURNAL_UNSAFE",
            message=(
                "Native ext4 write is disabled until JBD2 replay is implemented "
                "(issue #385)"
            ),
        )
