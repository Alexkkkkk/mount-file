# SPDX-License-Identifier: MIT
"""Windows replay comparator reports typed NTFS refusals without mutating input."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import scripts.compare_ntfs_replay as replay_comparator
from mountfile.ntfs_logfile import NtfsLogFileError


def test_logfile_refusal_writes_blocked_report_and_preserves_crash_image(
    tmp_path, monkeypatch
):
    crash_image = tmp_path / "crash.img"
    chkdsk_image = tmp_path / "chkdsk.img"
    native_image = tmp_path / "native.img"
    report_path = tmp_path / "comparison.json"
    crash_bytes = b"captured crash volume"
    crash_image.write_bytes(crash_bytes)
    chkdsk_image.write_bytes(b"checker-repaired volume")

    def refuse_after_copy(image_path: str) -> None:
        Path(image_path).write_bytes(b"changed disposable copy")
        raise NtfsLogFileError("malformed active $LogFile record")

    monkeypatch.setattr(replay_comparator, "native_ntfs_recover", refuse_after_copy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_ntfs_replay.py",
            "--crash-image",
            str(crash_image),
            "--chkdsk-image",
            str(chkdsk_image),
            "--native-image",
            str(native_image),
            "--out",
            str(report_path),
        ],
    )

    assert replay_comparator.main() == 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["error_type"] == "NtfsLogFileError"
    assert "malformed active $LogFile record" in report["error"]
    assert crash_image.read_bytes() == crash_bytes
    assert native_image.read_bytes() == b"changed disposable copy"
