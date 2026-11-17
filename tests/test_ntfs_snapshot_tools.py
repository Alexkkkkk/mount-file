from pathlib import Path
from types import SimpleNamespace

from scripts import compare_ntfs_snapshots, windows_ntfs_raw_snapshot


class _Image:
    def __init__(self, value):
        self.value = value
        self.lengths = []

    def read_mft_record(self, _record_number):
        return object()

    def _attributes_for_record(self, _record_number, _record):
        return [
            SimpleNamespace(
                type_code=0x80,
                name="",
                nonresident=True,
                data_size=len(self.value),
            )
        ]

    def _read_nonresident_range(self, _attribute, offset, length):
        self.lengths.append(length)
        return self.value[offset : offset + length]


def test_snapshot_stream_is_written_in_bounded_chunks(tmp_path, monkeypatch):
    image = _Image(bytes(range(251)) * 5)
    monkeypatch.setattr(windows_ntfs_raw_snapshot, "_SNAPSHOT_CHUNK_BYTES", 127)
    output = tmp_path / "MFT.bin"

    written = windows_ntfs_raw_snapshot._write_stream(image, 0, "", output)

    assert written == len(image.value)
    assert output.read_bytes() == image.value
    assert max(image.lengths) <= 127
    assert not output.with_name(output.name + ".tmp").exists()


def test_compare_file_streams_and_reports_exact_mismatch(tmp_path, monkeypatch):
    before = tmp_path / "before.bin"
    after = tmp_path / "after.bin"
    left = b"abcdefghij"
    right = b"abXdefgYij!"
    before.write_bytes(left)
    after.write_bytes(right)
    monkeypatch.setattr(compare_ntfs_snapshots, "_CHUNK_BYTES", 3)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError()),
    )

    result = compare_ntfs_snapshots.compare_file(before, after)

    assert result["before_size"] == len(left)
    assert result["after_size"] == len(right)
    assert result["differing_bytes"] == 3
    assert result["first_difference"] == 2
    assert result["byte_identical"] is False


def test_compare_file_identical_streams(tmp_path):
    before = tmp_path / "before.bin"
    after = tmp_path / "after.bin"
    before.write_bytes(b"same bytes")
    after.write_bytes(b"same bytes")

    result = compare_ntfs_snapshots.compare_file(before, after)

    assert result["differing_bytes"] == 0
    assert result["first_difference"] is None
    assert result["byte_identical"] is True
