# SPDX-License-Identifier: MIT
"""Contract tests for the shared read-only VFS stat API."""

import pytest

from mountfile.vfs_api import ReadOnlyVFSStatMixin


class _Provider(ReadOnlyVFSStatMixin):
    def __init__(self, case_insensitive=False):
        self.case_insensitive_paths = case_insensitive

    def list_dir(self, path):
        if path == "/":
            return [{"name": "Folder", "is_dir": True, "size": 0, "date": ""}]
        if path == "/Folder":
            return [
                {
                    "name": "Report.TXT",
                    "is_dir": False,
                    "size": 17,
                    "date": "2026-10-27",
                    "inode": 42,
                }
            ]
        raise FileNotFoundError(path)


def test_stat_root_and_nested_path_preserve_provider_metadata():
    provider = _Provider()
    assert provider.stat("/") == {
        "path": "/",
        "name": "/",
        "is_dir": True,
        "size": 0,
        "date": "",
    }
    result = provider.stat("/Folder/Report.TXT")
    assert result == {
        "path": "/Folder/Report.TXT",
        "name": "Report.TXT",
        "is_dir": False,
        "size": 17,
        "date": "2026-10-27",
        "inode": 42,
    }


def test_stat_casefold_is_opt_in_and_returns_actual_path():
    provider = _Provider(case_insensitive=True)
    result = provider.stat("/folder/report.txt")
    assert result["path"] == "/Folder/Report.TXT"
    assert result["size"] == 17

    case_sensitive = _Provider()
    with pytest.raises(FileNotFoundError):
        case_sensitive.stat("/folder/report.txt")


def test_stat_rejects_traversal_and_invalid_types():
    provider = _Provider()
    with pytest.raises(ValueError, match="traversal"):
        provider.stat("/Folder/../Report.TXT")
    with pytest.raises(ValueError, match="invalid"):
        provider.stat("/Folder\\Report.TXT")
    with pytest.raises(ValueError, match="invalid"):
        provider.stat(None)


def test_backend_status_is_explicitly_read_only_and_serialisable():
    provider = _Provider()
    assert provider.write_capabilities == ()
    assert provider.backend_status == {
        "backend": "_Provider",
        "mode": "read-only",
        "read_capabilities": ["list", "stat", "read-range", "extract"],
        "write_capabilities": [],
        "write_enabled": False,
        "write_rejection_reason": (
            "native provider is read-only; use an explicitly selected "
            "bounded writer profile"
        ),
    }
