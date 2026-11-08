"""Tests for the generated README status block."""

from scripts.generate_readme import END, START, render, update


def _root(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "9.9.9"\n', encoding="utf-8"
    )
    (tmp_path / "mountfile").mkdir()
    (tmp_path / "mountfile" / "capabilities.py").write_text("", encoding="utf-8")
    return tmp_path


def test_update_replaces_only_generated_block(tmp_path):
    root = _root(tmp_path)
    readme = f"manual before\n{START}\nold\n{END}\nmanual after\n"
    result = update(readme, render(root))
    assert "manual before" in result
    assert "manual after" in result
    assert "9.9.9" in result
    assert result.count(START) == 1
    assert result.count(END) == 1


def test_block_does_not_depend_on_a_branch_name(tmp_path):
    """A branch-derived block made every pull request rewrite its own head."""
    block = render(_root(tmp_path))
    assert "Source revision" not in block
    assert "feat/" not in block
    assert "automation/" not in block


def test_render_mentions_available_status_automation(tmp_path):
    root = _root(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "checker_acceptance.py").write_text("", encoding="utf-8")
    text = render(root)
    assert "checker acceptance" in text
    assert "parity" in text
