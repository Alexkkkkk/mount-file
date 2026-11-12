# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Базовые контракты слоя VFS — переэкспорт канонических определений."""

from mountfile.vfs_contract import VFS, ErrorCode, Operation, VFSResult  # noqa: F401

__all__ = ["ErrorCode", "Operation", "VFS", "VFSResult"]
