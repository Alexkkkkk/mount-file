"""Tests for canonical repository status synchronization."""

import json

from scripts.sync_repository_status import sync


def test_sync_writes_all_canonical_status_artifacts(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "issue-182-status.md").write_text(
        "power_loss unsupported not_claimed\n| B1 |\n| D1 |\n| P1 |\n",
        encoding="utf-8",
    )
    output = tmp_path / "generated"
    written = sync(tmp_path, output)
    names = {path.name for path in written}
    assert names == {
        "capability-matrix.json",
        "checker-matrix.json",
        "recovery-matrix.json",
        "product-status.json",
    }
    for path in written:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    assert json.loads((output / "product-status.json").read_text())["verdict"] == "pass"


def test_sync_is_idempotent(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "issue-182-status.md").write_text(
        "power_loss unsupported not_claimed\n| B1 |\n| D1 |\n| P1 |\n",
        encoding="utf-8",
    )
    output = tmp_path / "generated"
    sync(tmp_path, output)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    sync(tmp_path, output)
    after = {path.name: path.read_bytes() for path in output.iterdir()}
    assert before == after
