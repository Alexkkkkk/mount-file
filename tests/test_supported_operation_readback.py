# SPDX-License-Identifier: MIT
"""Issue #510, D5: independent checker + read-back after every supported operation.

The contract has two halves:

* **static** -- the source of every supported operation must *call* its
  post-operation verification inside the runtime (``verify_image_after_operation``
  for ext4, the ``read_back`` cell backed by the independent ``ntfscat``
  helper for NTFS).  A storage step added without a verification call fails
  this test, so the contract cannot rot silently;
* **dynamic** -- a real successful NTFS ``write_file`` must order the
  independent read-back strictly between the storage step and the identity
  check, and before the atomic publish.
"""

from __future__ import annotations

import inspect

import pytest

from mountfile.journal_safety import JournalSafetyError
from mountfile.native_write import (
    ENABLE_ENV,
    ENABLE_VALUE,
    NtfsTransactionalWriter,
)
from tests.test_ntfs_write_phase_injection import _install_doubles

# Supported (profile, operation) pairs and the runtime symbol that must be
# reached by each operation's source.
CONTRACT = (
    ("ext3/ext4", "write_file", "verify_image_after_operation"),
    ("ext3/ext4", "create_file", "verify_image_after_operation"),
    ("NTFS", "write_file", "_read_ntfs_file_bytes"),
)


def test_every_supported_operation_calls_post_operation_verification() -> None:
    from mountfile.ext4_native_writer import Ext4NativeWriter
    from mountfile.native_write import NtfsTransactionalWriter as Writer

    sources = {
        "ext3/ext4": {
            "write_file": inspect.getsource(Ext4NativeWriter.write_file),
            "create_file": inspect.getsource(Ext4NativeWriter.create_file),
        },
        "NTFS": {"write_file": inspect.getsource(Writer.write_file)},
    }
    for profile, operation, symbol in CONTRACT:
        body = sources[profile][operation]
        assert symbol in body, (
            f"{profile}.{operation} must call {symbol} inside the runtime; "
            "a supported operation without post-operation verification is a "
            "silent D5 regression"
        )
    ext4_module = inspect.getsource(
        __import__("mountfile.ext4_native_writer", fromlist=["verify_image_after_operation"])
    )
    assert "e2fsck" in ext4_module, "ext4 verification must use the independent checker"


def test_ntfs_read_back_is_ordered_between_write_and_identity(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "d5.img"
    image.write_bytes(b"d5-source-image" + bytes(range(256)) * 4)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail="")
    NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    assert "read_back" in order
    assert order.index("read_back") > order.index("ntfs_write")
    assert order.index("read_back") < order.index("identity_verify")
    assert order.index("read_back") < order.index("publish")


def test_failed_read_back_blocks_publication(tmp_path, monkeypatch) -> None:
    image = tmp_path / "d5-fail.img"
    image.write_bytes(b"d5-source-image" + bytes(range(256)) * 4)
    monkeypatch.setenv(ENABLE_ENV, ENABLE_VALUE)
    order: list[str] = []
    _install_doubles(monkeypatch, order=order, fail="read_back")
    with pytest.raises((JournalSafetyError, Exception)) as excinfo:
        NtfsTransactionalWriter().write_file(str(image), "target", b"payload\n")
    assert "publish" not in order
    assert excinfo.value is not None
