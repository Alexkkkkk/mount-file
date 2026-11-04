"""Resource-limit regressions for safe extraction."""

import pytest

from mountfile.extract import ExtractError, ExtractionLimits, extract_file, extract_path


class ImageProvider:
    def __init__(self, entries=None, files=None):
        self.entries = entries or {"/": []}
        self.files = files or {}
        self.range_calls = []
        self.whole_file_calls = []

    def list_dir(self, path="/"):
        if path not in self.entries:
            raise FileNotFoundError(path)
        return self.entries[path]

    def file_size(self, path):
        return len(self.files[path])

    def read_file_range(self, path, offset, length):
        self.range_calls.append((path, offset, length))
        return self.files[path][offset : offset + length]

    def read_file(self, path):
        self.whole_file_calls.append(path)
        return self.files[path]


def _file(name):
    return {"name": name, "is_dir": False}


def _dir(name):
    return {"name": name, "is_dir": True}


def _limits(**kwargs):
    values = {
        "max_file_size": 1024,
        "max_total_size": 4096,
        "max_files": 10,
        "max_depth": 8,
    }
    values.update(kwargs)
    return ExtractionLimits(**values)


def test_extract_file_rejects_known_oversize_before_range_read_and_preserves_target(
    tmp_path,
):
    provider = ImageProvider(files={"/large.bin": b"0123456789"})
    destination = tmp_path / "evidence.bin"
    destination.write_bytes(b"previous evidence")

    with pytest.raises(ExtractError, match="size limit"):
        extract_file(
            provider,
            "/large.bin",
            destination,
            limits=_limits(max_file_size=9),
        )

    assert provider.range_calls == []
    assert destination.read_bytes() == b"previous evidence"
    assert list(tmp_path.glob(".mountfile-*.tmp")) == []


def test_extract_file_caps_legacy_whole_file_provider_before_commit(tmp_path):
    class WholeFileProvider:
        def __init__(self):
            self.calls = 0

        def read_file(self, _path):
            self.calls += 1
            return b"too large"

    provider = WholeFileProvider()
    destination = tmp_path / "evidence.bin"
    destination.write_bytes(b"previous evidence")

    with pytest.raises(ExtractError, match="size limit"):
        extract_file(
            provider,
            "/evidence.bin",
            destination,
            limits=_limits(max_file_size=3),
        )

    assert provider.calls == 1
    assert destination.read_bytes() == b"previous evidence"
    assert list(tmp_path.glob(".mountfile-*.tmp")) == []


def test_extract_tree_enforces_total_bytes_and_keeps_prior_files_only(tmp_path):
    provider = ImageProvider(
        entries={"/": [_file("one.bin"), _file("two.bin")]},
        files={"/one.bin": b"123", "/two.bin": b"456"},
    )

    with pytest.raises(ExtractError, match="size limit"):
        extract_path(
            provider,
            "/",
            tmp_path,
            limits=_limits(max_file_size=8, max_total_size=4),
        )

    assert (tmp_path / "one.bin").read_bytes() == b"123"
    assert not (tmp_path / "two.bin").exists()
    assert [call[0] for call in provider.range_calls] == ["/one.bin"]


def test_extract_tree_enforces_file_count_before_writing_next_file(tmp_path):
    provider = ImageProvider(
        entries={"/": [_file("one.bin"), _file("two.bin")]},
        files={"/one.bin": b"1", "/two.bin": b"2"},
    )

    with pytest.raises(ExtractError, match="file-count limit"):
        extract_path(
            provider,
            "/",
            tmp_path,
            limits=_limits(max_files=1),
        )

    assert (tmp_path / "one.bin").read_bytes() == b"1"
    assert not (tmp_path / "two.bin").exists()
    assert [call[0] for call in provider.range_calls] == ["/one.bin"]


def test_extract_tree_bounds_directory_listing_size(tmp_path):
    provider = ImageProvider(
        entries={"/": [_file("one.bin"), _file("two.bin")]},
        files={"/one.bin": b"1", "/two.bin": b"2"},
    )

    with pytest.raises(ExtractError, match="entry-count limit"):
        extract_path(
            provider,
            "/",
            tmp_path,
            limits=_limits(max_entries=1),
        )

    assert provider.range_calls == []
    assert not (tmp_path / "one.bin").exists()
    assert not (tmp_path / "two.bin").exists()


def test_extract_tree_enforces_nested_directory_depth_before_creation(tmp_path):
    provider = ImageProvider(
        entries={"/": [_dir("nested")], "/nested": [_file("payload.bin")]},
        files={"/nested/payload.bin": b"payload"},
    )

    with pytest.raises(ExtractError, match="depth limit"):
        extract_path(
            provider,
            "/",
            tmp_path,
            limits=_limits(max_depth=0),
        )

    assert not (tmp_path / "nested").exists()
    assert provider.range_calls == []


def test_extract_single_file_respects_zero_file_count_limit(tmp_path):
    provider = ImageProvider(
        entries={"/": [_file("one.bin")]}, files={"/one.bin": b"1"}
    )

    with pytest.raises(ExtractError, match="file-count limit"):
        extract_path(
            provider,
            "/one.bin",
            tmp_path,
            limits=_limits(max_files=0),
        )

    assert provider.range_calls == []
    assert not (tmp_path / "one.bin").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_size": -1},
        {"max_total_size": True},
        {"max_files": 1.5},
        {"max_entries": -1},
        {"max_depth": -1},
    ],
)
def test_extraction_limits_reject_invalid_values(kwargs):
    with pytest.raises(ValueError, match="non-negative integer"):
        _limits(**kwargs)


def test_extract_rejects_non_limits_configuration_before_creating_destination(
    tmp_path,
):
    provider = ImageProvider()
    destination = tmp_path / "not-created"
    with pytest.raises(TypeError, match="ExtractionLimits"):
        extract_path(provider, "/", destination, limits={"max_files": 10})
    assert not destination.exists()
