# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Единый контракт VFS для всех файловых систем (issue #388).

Модуль определяет транспорт-независимый набор операций и типизированный
результат. Каждый нативный провайдер (FAT32, ext2/3/4, NTFS, exFAT) обязан
реализовать :class:`VFS`, а GUI/CLI — спрашивать ``supported_operations()``
перед тем, как показывать пользователю кнопку «Записать».

Контракт дополняет, а не заменяет существующую матрицу возможностей
:mod:`mountfile.capabilities`: канонические имена операций совпадают со
строковыми значениями :class:`Operation`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO, List, Optional, Union


class Operation(str, Enum):
    """Канонические операции, публикуемые каждым профилем ФС."""

    LIST = "list"
    STAT = "stat"
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"
    MKDIR = "mkdir"


# Стабильные machine-readable коды ошибок. Значения пересекаются с
# :class:`mountfile.safety.RefusalCode`, где это возможно, чтобы refusal-слой
# оставался единым.
class ErrorCode(str, Enum):
    WRITE_NOT_SUPPORTED = "write_not_supported"
    OPERATION_NOT_SUPPORTED = "operation_not_supported"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    INVALID_PATH = "invalid_path"
    OUT_OF_SPACE = "out_of_space"
    IO_ERROR = "io_error"


@dataclass
class VFSResult:
    """Результат операции VFS. Ошибки не бросаются — они возвращаются здесь."""

    success: bool
    error_code: Optional[str] = None
    message: str = ""
    # Произвольные полезные данные: список записей, stat-словарь, байты и т.п.
    data: Optional[object] = field(default=None, repr=False)

    @classmethod
    def ok(cls, data: Optional[object] = None) -> "VFSResult":
        return cls(success=True, data=data)

    @classmethod
    def fail(
        cls,
        error_code: Union[str, ErrorCode],
        message: str = "",
    ) -> "VFSResult":
        code = error_code.value if isinstance(error_code, ErrorCode) else error_code
        return cls(success=False, error_code=code, message=message)


class VFS(ABC):
    """Единый контракт для всех файловых систем.

    Реализации обязаны быть fail-closed: любая неподдерживаемая операция
    возвращает ``VFSResult.fail(...)`` с кодом из :class:`ErrorCode`, а не
    бросает исключение сквозь публичный API.
    """

    @abstractmethod
    def supported_operations(self) -> List[Operation]:
        """Вернуть список реально поддерживаемых операций."""
        raise NotImplementedError

    def supports(self, operation: Operation) -> bool:
        return operation in self.supported_operations()

    @abstractmethod
    def listdir(self, path: str) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def stat(self, path: str) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def read(self, path: str, offset: int = 0, size: int = -1) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def write(self, path: str, data: Union[BinaryIO, bytes], offset: int = 0) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def create(self, path: str, is_dir: bool = False) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def delete(self, path: str, recursive: bool = False) -> VFSResult:
        raise NotImplementedError

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> VFSResult:
        raise NotImplementedError
