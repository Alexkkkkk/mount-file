# SPDX-License-Identifier: MIT
"""Shared fail-closed refusal taxonomy and checked byte-range arithmetic."""

from __future__ import annotations

from enum import Enum
from typing import TypeVar


class RefusalCategory(str, Enum):
    """Coarse, stable classes of deliberate fail-closed refusals."""

    UNSUPPORTED = "unsupported"
    DIRTY_RECOVERY = "dirty_recovery"
    CORRUPT = "corrupt"
    OUT_OF_BOUNDS = "out_of_bounds"
    CONCURRENT_CHANGE = "concurrent_change"
    UNSAFE = "unsafe"


class RefusalCode(str, Enum):
    """Stable machine-readable reasons for refusing a filesystem operation."""

    UNSUPPORTED_PROFILE = "unsupported_profile"
    DIRTY_OR_RECOVERY_REQUIRED = "dirty_or_recovery_required"
    MALFORMED_METADATA = "malformed_metadata"
    RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"
    INTEGRITY_CHECK_FAILED = "integrity_check_failed"
    OPERATION_NOT_SUPPORTED = "operation_not_supported"
    CONCURRENT_CHANGE = "concurrent_change"
    UNSAFE = "unsafe"


_CODE_CATEGORIES = {
    RefusalCode.UNSUPPORTED_PROFILE: RefusalCategory.UNSUPPORTED,
    RefusalCode.OPERATION_NOT_SUPPORTED: RefusalCategory.UNSUPPORTED,
    RefusalCode.DIRTY_OR_RECOVERY_REQUIRED: RefusalCategory.DIRTY_RECOVERY,
    RefusalCode.MALFORMED_METADATA: RefusalCategory.CORRUPT,
    RefusalCode.INTEGRITY_CHECK_FAILED: RefusalCategory.CORRUPT,
    RefusalCode.RANGE_OUT_OF_BOUNDS: RefusalCategory.OUT_OF_BOUNDS,
    RefusalCode.CONCURRENT_CHANGE: RefusalCategory.CONCURRENT_CHANGE,
    RefusalCode.UNSAFE: RefusalCategory.UNSAFE,
}


def code_for_message(message: str) -> RefusalCode:
    """Classify a legacy human refusal message without changing that message."""
    text = str(message).lower()
    if "concurr" in text or "changed during" in text or "changed while" in text:
        return RefusalCode.CONCURRENT_CHANGE
    if "dirty" in text or "journal recovery" in text or "recovery" in text:
        return RefusalCode.DIRTY_OR_RECOVERY_REQUIRED
    if any(
        word in text for word in ("outside", "out-of-bounds", "out of bounds", "extent")
    ):
        return RefusalCode.RANGE_OUT_OF_BOUNDS
    if any(
        word in text
        for word in ("corrupt", "checksum", "truncated", "integrity", "malformed")
    ):
        return RefusalCode.MALFORMED_METADATA
    if any(
        word in text for word in ("unsafe", "refusing", "cannot safely", "marked free")
    ):
        return RefusalCode.UNSAFE
    if any(word in text for word in ("unsupported", "requires", "not supported")):
        return RefusalCode.UNSUPPORTED_PROFILE
    return RefusalCode.UNSUPPORTED_PROFILE


class RefusalError(ValueError):
    """A deliberate, fail-closed refusal with a stable reason code."""

    def __init__(self, message: str, code: RefusalCode | None = None):
        super().__init__(message)
        self.code = RefusalCode(code) if code is not None else code_for_message(message)

    @property
    def category(self) -> RefusalCategory:
        """Return the stable coarse category for this refusal."""
        return _CODE_CATEGORIES[self.code]


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

    Returns the validated half-open interval. Booleans are rejected despite
    being ``int`` subclasses; subtraction-form checks avoid trusting wrapped
    fixed-width end arithmetic.
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
