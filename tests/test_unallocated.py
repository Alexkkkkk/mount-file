# SPDX-License-Identifier: MIT
"""Focused tests for the raw-range unallocated index."""

import hashlib
import sqlite3

import pytest

from mountfile.unallocated import InvalidRange, UnallocatedIndexer


def test_duplicate_chunks_share_storage_but_keep_occurrences(tmp_path):
    database = tmp_path / "unallocated.sqlite"
    source = b"ABCDABCD!!"

    with UnallocatedIndexer(database) as indexer:
        unique = indexer.index(source, [(4, 4), (0, 4), (8, 2), (0, 4)])
        assert [(item.offset, item.length) for item in unique] == [(0, 4), (8, 2)]
        assert len(indexer.occurrences()) == 3
        assert indexer.occurrences()[0].sha256 == hashlib.sha256(b"ABCD").hexdigest()
        assert indexer.is_indexed(4, 4)
        assert indexer.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
        assert indexer.connection.execute("SELECT COUNT(*) FROM bitmap_pages").fetchone()[0] == 1


def test_index_survives_reopening_and_does_not_duplicate_rows(tmp_path):
    database = tmp_path / "unallocated.sqlite"
    source = b"0123456789abcdef"

    first = UnallocatedIndexer(database)
    first.index(source, [(0, 4), (8, 4)])
    first.close()

    second = UnallocatedIndexer(database)
    before = second.records()
    second.index(source, [(8, 4), (0, 4)])
    assert second.records() == before
    assert len(second.occurrences()) == 2
    assert second.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
    second.close()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key = 'source_size'").fetchone()[0] == "16"


@pytest.mark.parametrize(
    "ranges",
    [
        [(-1, 1)],
        [(0, 0)],
        [(9, 2)],
        [(17, 1)],
        [(True, 1)],
    ],
)
def test_invalid_ranges_are_rejected_without_partial_index(tmp_path, ranges):
    database = tmp_path / "unallocated.sqlite"
    indexer = UnallocatedIndexer(database)
    with pytest.raises((InvalidRange, TypeError, ValueError)):
        indexer.index(b"0123456789", ranges)
    assert indexer.records() == ()
    assert indexer.occurrences() == ()
    indexer.close()
