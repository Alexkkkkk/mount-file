# SPDX-License-Identifier: MIT
"""Tests for shared filesystem safety primitives."""

import pytest

from mountfile.safety import RefusalCategory, RefusalCode, RefusalError, checked_range


@pytest.mark.parametrize(
    ("start", "length", "lower", "upper", "expected"),
    [
        (0, 0, 0, 0, (0, 0)),
        (2, 4, 0, 6, (2, 6)),
        (5, 1, 5, 6, (5, 6)),
    ],
)
def test_checked_range_accepts_valid_half_open_intervals(
    start, length, lower, upper, expected
):
    assert checked_range(start, length, lower=lower, upper=upper) == expected


@pytest.mark.parametrize(
    ("start", "length", "lower", "upper"),
    [
        (-1, 1, 0, 10),
        (0, -1, 0, 10),
        (9, 2, 0, 10),
        (4, 1, 5, 10),
        (True, 1, 0, 10),
        (0, False, 0, 10),
        (0, 1, 0, 9.5),
    ],
)
def test_checked_range_fails_closed_with_typed_code(start, length, lower, upper):
    with pytest.raises(RefusalError) as caught:
        checked_range(start, length, lower=lower, upper=upper)
    assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS


def test_checked_range_supports_parser_specific_error_subclasses():
    class ParserRefusal(RefusalError):
        pass

    with pytest.raises(ParserRefusal) as caught:
        checked_range(8, 4, upper=10, error_type=ParserRefusal)
    assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS


def test_checked_range_matches_exhaustive_small_integer_interval_contract():
    for lower in range(4):
        for upper in range(lower, 6):
            for start in range(-1, 7):
                for length in range(-1, 7):
                    valid = lower <= start and length >= 0 and start + length <= upper
                    if valid:
                        assert checked_range(
                            start, length, lower=lower, upper=upper
                        ) == (start, start + length)
                    else:
                        with pytest.raises(RefusalError) as caught:
                            checked_range(start, length, lower=lower, upper=upper)
                        assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS


@pytest.mark.parametrize(
    ("message", "code", "category"),
    [
        (
            "unsupported ext2 feature",
            RefusalCode.UNSUPPORTED_PROFILE,
            RefusalCategory.UNSUPPORTED,
        ),
        (
            "volume is dirty; recovery required",
            RefusalCode.DIRTY_OR_RECOVERY_REQUIRED,
            RefusalCategory.DIRTY_RECOVERY,
        ),
        (
            "corrupt metadata checksum",
            RefusalCode.MALFORMED_METADATA,
            RefusalCategory.CORRUPT,
        ),
        (
            "selected extent is outside image bounds",
            RefusalCode.RANGE_OUT_OF_BOUNDS,
            RefusalCategory.OUT_OF_BOUNDS,
        ),
        (
            "image changed concurrently",
            RefusalCode.CONCURRENT_CHANGE,
            RefusalCategory.CONCURRENT_CHANGE,
        ),
        ("refusing unsafe path", RefusalCode.UNSAFE, RefusalCategory.UNSAFE),
    ],
)
def test_legacy_messages_have_typed_taxonomy(message, code, category):
    refusal = RefusalError(message)
    assert refusal.code is code
    assert refusal.category is category
    assert str(refusal) == message
