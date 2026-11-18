# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Regression guard for the py/path-injection fix in mountfile_dfir.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from mountfile_dfir import safe_path, sha256_file


@pytest.fixture(autouse=True)
def dfir_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MF_DFIR_ROOT", str(tmp_path))
    return tmp_path


def test_relative_path_resolves_inside_root(tmp_path):
    assert safe_path("a.txt") == (tmp_path / "a.txt").resolve()


def test_absolute_path_inside_root_is_allowed(tmp_path):
    target = tmp_path / "b.bin"
    target.write_bytes(b"x")
    assert safe_path(target) == target.resolve()


@pytest.mark.parametrize("evil", ["../outside.txt", "../../etc/passwd", "/etc/passwd", "/tmp/zzz"])
def test_traversal_and_foreign_absolute_paths_are_rejected(evil):
    with pytest.raises(ValueError, match="outside the DFIR root"):
        safe_path(evil)


def test_symlink_escaping_the_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="outside the DFIR root"):
        safe_path(link)


def test_sha256_file_hashes_inside_root_only(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"hello")
    assert sha256_file("f.bin") == sha256_file(tmp_path / "f.bin")
    with pytest.raises(ValueError, match="outside the DFIR root"):
        sha256_file("/etc/passwd")
