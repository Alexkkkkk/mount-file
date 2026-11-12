from __future__ import annotations

import datetime as dt

from scripts.branch_hygiene import (
    branch_age_days,
    classify_branch,
    is_protected,
    render_markdown,
)


def test_protected_names_and_release_prefix_are_never_stale():
    assert is_protected("main", "main")
    assert is_protected("release/1.2", "main")
    assert not is_protected("feat/example", "main")


def test_branch_with_open_pr_is_active_even_when_old():
    report = classify_branch(
        name="feat/old",
        sha="a" * 40,
        committed_at="2025-01-01T00:00:00Z",
        default_branch="main",
        stale_days=45,
        open_pr=123,
        now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert report["status"] == "active_pr"
    assert not report["stale"]


def test_old_branch_without_pr_is_stale_and_markdown_is_safe():
    report = classify_branch(
        name="feat/old",
        sha="b" * 40,
        committed_at="2025-01-01T00:00:00Z",
        default_branch="main",
        stale_days=45,
        open_pr=None,
        now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert report["status"] == "stale"
    markdown = render_markdown(
        {
            "repository": "owner/repo",
            "default_branch": "main",
            "stale_days": 45,
            "summary": {
                "branch_count": 1,
                "open_pr_count": 0,
                "stale_count": 1,
            },
            "branches": [report],
        }
    )
    assert "read-only" in markdown
    assert "`feat/old`" in markdown


def test_unknown_commit_date_is_not_marked_stale():
    assert branch_age_days(None) is None
    report = classify_branch(
        name="feat/unknown",
        sha="c" * 40,
        committed_at=None,
        default_branch="main",
        stale_days=1,
        open_pr=None,
    )
    assert report["status"] == "active"
    assert not report["stale"]
