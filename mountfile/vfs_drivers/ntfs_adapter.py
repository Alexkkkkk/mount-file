# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""NTFS-драйвер VFS с жёсткой блокировкой записи (issue #386).

Запись возвращает fail-closed отказ с кодом ``WRITE_DISABLED_LOGFILE_UNSAFE``,
пока не реализовано восстановление ``$LogFile`` (issue #386).
"""

from __future__ import annotations

from typing import BinaryIO, List, Union

from mountfile.vfs_contract import VFSResult
from mountfile.vfs_drivers.readonly_base import ReadOnlyNativeVFS


class NtfsVFS(ReadOnlyNativeVFS):
    """Read-only драйвер NTFS; запись явно заблокирована до $LogFile recovery."""

    provider_property = "ntfs"

    def supported_operations(self) -> List["Operation"]:  # noqa: F821
        from mountfile.vfs_contract import Operation

        return [Operation.LIST, Operation.STAT, Operation.READ]

    def write(
        self, path: str, data: Union[BinaryIO, bytes], offset: int = 0
    ) -> VFSResult:
        return VFSResult(
            success=False,
            error_code="WRITE_DISABLED_LOGFILE_UNSAFE",
            message=(
                "Native NTFS write is disabled until $LogFile recovery is "
                "implemented (issue #386)"
            ),
        )
