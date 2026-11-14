"""Tests for canonical repository status synchronization."""

import json

from scripts.sync_repository_status import sync

_README = """# Fixture

| Profile | Mode | Write | Notes |
| --- | --- | --- | --- |
| ext3/ext4 | read-only | no | journaled profiles stay unsupported |
| NTFS | read-only | no | log recovery is not implemented |

Native ext3/ext4 and NTFS writes are unsupported. Power-loss safety is unsupported.
"""

_CHANGELOG = """# Changelog

## Unreleased

Native ext3/ext4/NTFS writes remain unsupported. Power-loss safety is unsupported.

## 1.0.0

Initial fixture.
"""

_CLI = '''"""Minimal CLI surface fixture."""

import json

CAPABILITIES = "capabilities"


def get_native_capability_matrix():
    return {"profiles": []}


def cmd_capabilities(args):
    matrix = get_native_capability_matrix()
    return json.dumps(matrix)
'''

_GUI = '''"""Minimal GUI surface fixture."""


class FixtureWindow:
    def _set_write_enabled(self):
        status = self.writer.capability_status()
        operations = status.get("operations", {})
        canonical = "native-write"
        state = operations.get(canonical, {}).get("state")
        return state == "available"
'''


def _fixture_root(tmp_path):
    """Build the smallest tree the product-status contract inspects.

    ``build_report`` fails closed when the documentation or UI surfaces stop
    matching the conservative capability matrix, so the fixture has to contain
    those surfaces instead of only ``tests/`` and ``docs/``.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "issue-182-status.md").write_text(
        "power_loss unsupported not_claimed\n| B1 |\n| D1 |\n| P1 |\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(_README, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    (tmp_path / "mountfile").mkdir()
    (tmp_path / "mountfile" / "cli.py").write_text(_CLI, encoding="utf-8")
    (tmp_path / "mountfile" / "mainwindow.py").write_text(_GUI, encoding="utf-8")
    return tmp_path


def test_sync_writes_all_canonical_status_artifacts(tmp_path):
    root = _fixture_root(tmp_path)
    output = tmp_path / "generated"
    written = sync(root, output)
    names = {path.name for path in written}
    assert names == {
        "capability-matrix.json",
        "checker-matrix.json",
        "recovery-acceptance.json",
        "recovery-matrix.json",
        "product-status.json",
    }
    for path in written:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    assert json.loads((output / "product-status.json").read_text())["verdict"] == "pass"


def test_sync_is_idempotent(tmp_path):
    root = _fixture_root(tmp_path)
    output = tmp_path / "generated"
    sync(root, output)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    sync(root, output)
    after = {path.name: path.read_bytes() for path in output.iterdir()}
    assert before == after
