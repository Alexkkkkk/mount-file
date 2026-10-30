# SPDX-License-Identifier: MIT
"""Shared fail-closed errors and checked byte-range arithmetic."""

from __future__ import annotations

from enum import Enum
from typing import TypeVar


class RefusalCode(str, Enum):
    """Stable machine-readable reasons for refusing a filesystem operation."""

    UNSUPPORTED_PROFILE = "unsupported_profile"
    DIRTY_OR_RECOVERY_REQUIRED = "dirty_or_recovery_required"
    MALFORMED_METADATA = "malformed_metadata"
    RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
    INTEGRITY_CHECK_FAILED = "integrity_check_failed"
    OPERATION_NOT_SUPPORTED = "operation_not_supported"


class RefusalError(ValueError):
    """A deliberate, fail-closed refusal with a stable reason code."""

    def __init__(
        self, message: str, code: RefusalCode = RefusalCode.UNSUPPORTED_PROFILE
    ):
        super().__init__(message)
        self.code = RefusalCode(code)


_E = TypeVar("_E", bound=RefusalError)


def checked_range(
    start: int,
    length: int,
    *,
    lower: int = 0,
    upper: int,
    context: str = "byte range",
    error_type: type[_E] = RefusalError,
) -> tuple[int, int]:
    """Validate ``[start, start + length)`` against ``[lower, upper)``.

    Booleans are rejected despite being ``int`` subclasses. Python integers do
    not overflow, but the explicit subtraction-form bound check also maps
    directly to fixed-width implementations and avoids trusting a wrapped end.
    Returns the validated half-open interval.
    """
    values = (start, length, lower, upper)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise error_type(
            f"{context}: bounds must be integers", RefusalCode.RANGE_OUT_OF_BOUNDS
        )
    if lower < 0 or upper < lower or start < lower or length < 0:
        raise error_type(
            f"{context}: invalid range bounds", RefusalCode.RANGE_OUT_OF_BOUNDS
        )
    if length > upper - start:
        raise error_type(
            f"{context}: range exceeds containing extent",
            RefusalCode.RANGE_OUT_OF_BOUNDS,
        )
    return start, start + length
