"""Tests for the generated README status block."""

from scripts.generate_readme import END, START, render, update


def test_update_replaces_only_generated_block(tmp_path):
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "9.9.9"\n', encoding="utf-8"
    )
    (root / "mountfile").mkdir()
    (root / "mountfile" / "capabilities.py").write_text("", encoding="utf-8")
    readme = f"manual before\n{START}\nold\n{END}\nmanual after\n"
    result = update(readme, render(root, "feature/readme"))
    assert "manual before" in result
    assert "manual after" in result
    assert "feature/readme" in result
    assert result.count(START) == 1
    assert result.count(END) == 1


def test_render_mentions_available_status_automation(tmp_path):
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "mountfile").mkdir()
    (tmp_path / "mountfile" / "capabilities.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "checker_acceptance.py").write_text("", encoding="utf-8")
    text = render(tmp_path, "main")
    assert "checker acceptance" in text
    assert "parity" in text
