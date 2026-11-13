from __future__ import annotations

from pathlib import Path

from mountfile.ext4_journal_gate import Ext4JournalGate, JournalState


def test_gate_is_fail_closed_for_missing_image(tmp_path: Path):
    result = Ext4JournalGate(tmp_path / "missing.img").check()
    assert result.state is JournalState.ERROR


def test_gate_is_fail_closed_for_truncated_image(tmp_path: Path):
    image = tmp_path / "tiny.img"
    image.write_bytes(b"\x00" * 100)
    result = Ext4JournalGate(image).check()
    assert result.state is JournalState.ERROR
    assert result.message
