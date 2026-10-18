from __future__ import annotations

from io import BytesIO

import pytest

from mountfile.dfir_slack import InvalidSlackBounds, SlackResult, extract_slack, read_file_slack


def test_extracts_only_final_cluster_slack_at_absolute_offset(tmp_path):
    image = tmp_path / "evidence.img"
    original = b"header" + b"DATA" + b"SLACK" + b"tail"
    image.write_bytes(original)

    # The final file cluster begins at absolute offset 6.  A six-byte file
    # occupies DATA plus the first two bytes of the cluster, leaving SLACK.
    result = read_file_slack(image, last_cluster_offset=6, file_size=6, cluster_size=9)

    assert result == SlackResult(b"ACK", 12)
    assert result.bytes == b"ACK"
    assert result.absolute_offset == 12
    assert image.read_bytes() == original


def test_stream_position_and_evidence_bytes_are_preserved():
    image = BytesIO(b"0123456789abcdef")
    image.seek(3)
    before = image.getvalue()

    result = extract_slack(image, last_cluster_offset=8, file_size=10, cluster_size=8)

    assert result == (b"abcdef", 10)
    assert image.tell() == 3
    assert image.getvalue() == before


def test_empty_and_cluster_aligned_files_explicitly_have_no_slack():
    image = b"abcdefgh"

    assert read_file_slack(image, last_cluster_offset=0, file_size=0, cluster_size=4) == (
        b"",
        None,
    )
    assert read_file_slack(image, last_cluster_offset=4, file_size=8, cluster_size=4) == (
        b"",
        None,
    )


def test_requirement_order_is_supported_without_extent_inference():
    image = b"ABCDwxyz"

    # This positional spelling is (last-cluster offset, file size, cluster
    # size, image).  The offset remains caller-supplied and absolute.
    assert extract_slack(4, 5, 4, image) == (b"xyz", 5)


def test_malformed_values_and_extent_bounds_are_rejected():
    image = b"0123456789"

    with pytest.raises(ValueError):
        read_file_slack(image, last_cluster_offset=-1, file_size=1, cluster_size=4)
    with pytest.raises(ValueError):
        read_file_slack(image, last_cluster_offset=0, file_size=-1, cluster_size=4)
    with pytest.raises(ValueError):
        read_file_slack(image, last_cluster_offset=0, file_size=1, cluster_size=0)
    with pytest.raises(InvalidSlackBounds):
        read_file_slack(image, last_cluster_offset=8, file_size=1, cluster_size=4)
    with pytest.raises(InvalidSlackBounds):
        read_file_slack(image, last_cluster_offset=10, file_size=0, cluster_size=1)


def test_non_integer_sizes_are_rejected():
    with pytest.raises(TypeError):
        read_file_slack(b"abcd", last_cluster_offset=0, file_size=1.5, cluster_size=4)
    with pytest.raises(TypeError):
        read_file_slack(b"abcd", last_cluster_offset=False, file_size=1, cluster_size=4)
