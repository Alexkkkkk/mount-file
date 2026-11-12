# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Рабочий каркас драйверов VFS: реестр профилей и фабрика (issue #388).

Пакет не затрагивает существующий модуль :mod:`mountfile.vfs` (провайдеры
чтения и маршрутизация остаются там) — он лишь добавляет единую точку входа
для GUI и CLI: ``get_vfs(fs_type, image_path)`` и реестр ``VFS_REGISTRY``.
"""

from __future__ import annotations

from typing import Dict, Type

from mountfile.vfs_contract import ErrorCode, Operation, VFS, VFSResult
from mountfile.vfs_drivers.ext4_adapter import Ext4VFS
from mountfile.vfs_drivers.fat32_adapter import Fat32VFS
from mountfile.vfs_drivers.ntfs_adapter import NtfsVFS

# Реестр профилей ФС: каноническое имя -> класс драйвера.
VFS_REGISTRY: Dict[str, Type[VFS]] = {
    "fat32": Fat32VFS,
    "ext3": Ext4VFS,  # ext3/ext4 разделяют read-only профиль классического слоя
    "ext4": Ext4VFS,
    "ntfs": NtfsVFS,
}


def get_vfs(fs_type: str, image_path: str, offset: int = 0) -> VFS:
    """Фабричный метод для получения VFS-драйвера по типу ФС.

    Размер тома определяется автоматически через
    :func:`mountfile.vfs.volume_size_for_offset`, как того требуют bounded
    native-провайдеры ext/NTFS.
    """
    from mountfile.vfs import volume_size_for_offset

    key = str(fs_type).lower()
    if key not in VFS_REGISTRY:
        raise ValueError(f"Unsupported filesystem type: {fs_type}")
    try:
        volume_size = volume_size_for_offset(image_path, offset)
    except (OSError, ValueError):
        # Тестовые/фиктивные образы могут не определять размер тома;
        # драйверы ext/NTFS сами откажут с typed refusal при открытии.
        volume_size = None
    return VFS_REGISTRY[key](image_path, offset=offset, volume_size=volume_size)


__all__ = [
    "ErrorCode",
    "Ext4VFS",
    "Fat32VFS",
    "NtfsVFS",
    "Operation",
    "VFS",
    "VFS_REGISTRY",
    "VFSResult",
    "get_vfs",
]
