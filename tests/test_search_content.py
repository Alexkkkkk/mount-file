import pytest

from mountfile.search_content import SearchError, search_image, search_regions


def test_search_image_finds_literal_longer_than_overlap_across_chunk_boundary(tmp_path):
    image = tmp_path / "image.bin"
    pattern = "a pattern considerably longer than the requested overlap"
    boundary = 17
    image.write_bytes(b"x" * boundary + pattern.encode() + b"y" * 19)

    hits = search_image(image, pattern, chunk=24, overlap=2)

    assert [(hit.offset, hit.length) for hit in hits] == [
        (boundary, len(pattern.encode()))
    ]


@pytest.mark.parametrize(
    "ranges, message",
    [
        ([(-1, 2)], "offset must be non-negative"),
        ([(0, 0)], "length must be positive"),
        ([(0, -1)], "length must be positive"),
        ([(3, 2)], "extends beyond image size"),
    ],
)
def test_search_regions_rejects_invalid_ranges(tmp_path, ranges, message):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcd")

    with pytest.raises(SearchError, match=message):
        search_regions(image, ranges, "a")


def test_search_regions_accepts_range_ending_at_eof(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"abcd")

    hits = search_regions(image, [(0, 4)], "abcd")

    assert [(hit.offset, hit.length) for hit in hits] == [(0, 4)]
