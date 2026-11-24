# SPDX-License-Identifier: MIT
"""Property and safety tests for the bounded read-through image cache."""

import os
import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from mountfile.image_source import (
    CACHE_BYTES_ENV,
    DEFAULT_CACHE_BYTES,
    CachedImageSource,
    FileImageSource,
    ImageSourceError,
    SourceFingerprint,
    cache_budget_from_env,
    open_image_source,
)
from mountfile.safety import RefusalCode


def _make_image(tmp_path, size=64 * 1024 + 777):
    data = bytes(random.Random(size).getrandbits(8) for _ in range(size))
    image = tmp_path / "disk.img"
    image.write_bytes(data)
    return image, data


def test_cache_returns_exactly_the_parent_bytes_for_random_ranges(tmp_path):
    image, data = _make_image(tmp_path)
    rng = random.Random(1234)
    with FileImageSource(image) as parent, CachedImageSource(parent) as cached:
        for _ in range(500):
            offset = rng.randrange(0, len(data))
            length = rng.randrange(0, min(9000, len(data) - offset) + 1)
            assert cached.read_at(offset, length) == data[offset : offset + length]


def test_cache_never_reads_past_the_image_and_refuses_out_of_bounds(tmp_path):
    image, data = _make_image(tmp_path, size=5000)
    with FileImageSource(image) as parent, CachedImageSource(parent) as cached:
        assert cached.size == len(data)
        assert cached.read_at(len(data) - 1, 1) == data[-1:]
        with pytest.raises(ImageSourceError) as caught:
            cached.read_at(len(data) - 1, 2)
        assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS
        with pytest.raises(ImageSourceError):
            cached.read_at(-1, 1)


def test_cache_hit_ratio_and_byte_budget_stay_bounded(tmp_path):
    image, _data = _make_image(tmp_path)
    with FileImageSource(image) as parent:
        cached = CachedImageSource(parent, max_bytes=8192, block_size=4096)
        try:
            # One hot block touched twice per round plus a cold working set
            # larger than the budget: the second touch must be a hit while the
            # cold blocks keep evicting each other.
            for _ in range(40):
                cached.read_at(0, 512)
                cached.read_at(0, 512)
                for block in range(2, 6):
                    cached.read_at(block * 4096, 512)
            stats = cached.stats()
            assert stats["cached_bytes"] <= 8192
            assert stats["blocks"] <= 2
            assert stats["hits"] == 40
            assert stats["misses"] == 40 + 40 * 4
            assert 0.0 <= stats["hit_ratio"] <= 1.0
        finally:
            cached.close()


def test_cache_streams_large_reads_without_polluting_the_budget(tmp_path):
    image, data = _make_image(tmp_path)
    with FileImageSource(image) as parent:
        cached = CachedImageSource(parent, max_bytes=64 * 1024, block_size=4096)
        try:
            assert cached.read_at(0, 60 * 1024) == data[: 60 * 1024]
            assert cached.stats()["blocks"] == 0
        finally:
            cached.close()


def test_open_image_source_with_zero_budget_is_the_bare_source(tmp_path):
    image, data = _make_image(tmp_path, size=1024)
    source = open_image_source(image, cache_bytes=0)
    try:
        assert isinstance(source, FileImageSource)
        assert not isinstance(source, CachedImageSource)
        assert source.read_at(0, 4) == data[:4]
    finally:
        source.close()
    with open_image_source(image) as cached:
        assert isinstance(cached, CachedImageSource)
        assert cached.stats()["max_bytes"] == DEFAULT_CACHE_BYTES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_CACHE_BYTES),
        ("", DEFAULT_CACHE_BYTES),
        ("0", 0),
        ("16", 16 << 20),
        (" 4 ", 4 << 20),
        ("abc", DEFAULT_CACHE_BYTES),
        ("-5", DEFAULT_CACHE_BYTES),
        ("1.5", DEFAULT_CACHE_BYTES),
    ],
)
def test_cache_budget_from_env_is_fail_safe(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(CACHE_BYTES_ENV, raising=False)
    else:
        monkeypatch.setenv(CACHE_BYTES_ENV, raw)
    assert cache_budget_from_env() == expected


@pytest.mark.parametrize("block_size", [0, 256, 1000, 128 * 1024])
def test_cache_rejects_invalid_block_sizes(tmp_path, block_size):
    image, _data = _make_image(tmp_path, size=4096)
    with FileImageSource(image) as parent:
        with pytest.raises(ImageSourceError) as caught:
            CachedImageSource(parent, block_size=block_size)
        assert caught.value.code is RefusalCode.UNSAFE


def test_cache_refuses_reads_after_close_and_keeps_parent_alive(tmp_path):
    image, data = _make_image(tmp_path, size=4096)
    parent = FileImageSource(image)
    cached = CachedImageSource(parent, close_parent=False)
    cached.close()
    with pytest.raises(ImageSourceError, match="closed"):
        cached.read_at(0, 1)
    try:
        assert parent.read_at(0, 2) == data[:2]
    finally:
        parent.close()


def test_cache_closes_its_parent_by_default(tmp_path):
    image, _data = _make_image(tmp_path, size=4096)
    parent = FileImageSource(image)
    CachedImageSource(parent).close()
    assert parent.closed


def test_cache_concurrent_reads_are_consistent(tmp_path):
    image, data = _make_image(tmp_path)
    ranges = [(index * 97, 300) for index in range(400)]
    with FileImageSource(image) as parent, CachedImageSource(parent) as cached:
        with ThreadPoolExecutor(max_workers=8) as executor:
            actual = list(executor.map(lambda r: cached.read_at(*r), ranges))
    assert actual == [data[o : o + n] for o, n in ranges]


def test_fingerprint_reports_a_truncated_or_rewritten_image(tmp_path):
    image, _data = _make_image(tmp_path, size=8192)
    source = FileImageSource(image)
    try:
        fingerprint = source.fingerprint()
        assert isinstance(fingerprint, SourceFingerprint)
        assert fingerprint.size == 8192
        assert fingerprint.to_dict()["size"] == 8192
        assert source.unchanged()
        image.write_bytes(b"new payload of a different length")
        assert not source.unchanged()
    finally:
        source.close()


def test_cached_source_exposes_the_fingerprint_of_its_parent(tmp_path):
    image, _data = _make_image(tmp_path, size=4096)
    with open_image_source(image) as cached:
        fingerprint = cached.fingerprint()
        assert fingerprint.size == 4096
        assert cached.unchanged()
