"""Regression tests for host-name collisions during tree extraction."""

import pytest

from mountfile.extract import ExtractError, extract_path


class TreeProvider:
    def __init__(self, trees):
        self.trees = trees
        self.reads = []

    def list_dir(self, path):
        return self.trees[path]

    def read_file(self, path):
        self.reads.append(path)
        return path.encode("utf-8")


def _entry(name, is_dir=False):
    return {"name": name, "is_dir": is_dir}


@pytest.mark.parametrize(
    "left,right",
    [
        ("report.txt", "REPORT.TXT"),
        ("café.txt", "cafe\u0301.txt"),
        ("a:b", "a_b"),
        ("file", "file. "),
        ("duplicate", "duplicate"),
    ],
)
def test_ambiguous_siblings_refused_before_writing(tmp_path, left, right):
    provider = TreeProvider({"/": [_entry(left), _entry(right)]})
    with pytest.raises(ExtractError, match="collision"):
        extract_path(provider, "/", tmp_path)
    assert provider.reads == []
    assert list(tmp_path.iterdir()) == []


def test_file_and_directory_with_same_host_name_are_refused(tmp_path):
    provider = TreeProvider({"/": [_entry("Docs", True), _entry("docs")]})
    with pytest.raises(ExtractError, match="collision"):
        extract_path(provider, "/", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_collision_inside_nested_directory_is_refused(tmp_path):
    provider = TreeProvider(
        {
            "/": [_entry("nested", True)],
            "/nested": [_entry("A"), _entry("a")],
        }
    )
    with pytest.raises(ExtractError, match="collision.*nested"):
        extract_path(provider, "/", tmp_path)
    assert list((tmp_path / "nested").iterdir()) == []
    assert provider.reads == []


def test_same_filename_in_distinct_directories_is_allowed(tmp_path):
    provider = TreeProvider(
        {
            "/": [_entry("one", True), _entry("two", True)],
            "/one": [_entry("readme")],
            "/two": [_entry("README")],
        }
    )
    assert extract_path(provider, "/", tmp_path) == 2
    assert (tmp_path / "one" / "readme").read_bytes() == b"/one/readme"
    assert (tmp_path / "two" / "README").read_bytes() == b"/two/README"


def test_name_without_portable_characters_is_refused(tmp_path):
    provider = TreeProvider({"/": [_entry(" . ")]})
    with pytest.raises(ExtractError, match="ambiguous"):
        extract_path(provider, "/", tmp_path)
    assert provider.reads == []
