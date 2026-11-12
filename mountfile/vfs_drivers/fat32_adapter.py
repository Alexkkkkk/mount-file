# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""FAT32-драйвер в контракте VFS (полный набор RW-операций).

Принимает путь к образу (как и остальные драйверы пакета) и сам открывает
:class:`mountfile.fat32.Fat32Provider`, переиспользуя всю логику операций из
:class:`mountfile.fat32_vfs.Fat32VFS`.
"""

from __future__ import annotations

from mountfile.fat32 import Fat32Provider
from mountfile.fat32_vfs import Fat32VFS as _ProviderBackedFat32VFS


class Fat32VFS(_ProviderBackedFat32VFS):
    def __init__(self, image_path: str, offset: int = 0, volume_size: int = None):
        super().__init__(Fat32Provider(image_path, offset=offset))
