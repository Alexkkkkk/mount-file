import json
import re

import pytest

import scripts.sync_issue_182_progress as progress_sync
from scripts.sync_issue_182_progress import (
    STATUS_PATH,
    ZERO_SHA,
    collect_first_parent_commits,
    commits_requiring_status,
    is_issue_182_branch,
    is_issue_182_related,
    missing_status_updates,
    render_comment,
)

REPOSITORY = "Alexkkkkk/mount-file"
MAIN_SHA = "a" * 40
STATUS = "| W1 | Native writers | Частично | Остались проверки. |"


def _commit(
    sha="abcdef0123456789", message="docs: update task status #182", files=None
):
    return {
        "id": sha,
        "message": message,
        "url": f"https://api.github.com/repos/example/project/commits/{sha}",
        "added": [],
        "modified": files or [],
        "removed": [],
    }


def _event(tmp_path, monkeypatch, event):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def _main_event(commit=None, after=MAIN_SHA):
    return {
        "ref": "refs/heads/main",
        "before": ZERO_SHA,
        "after": after,
        "commits": [commit or _commit(after, files=[STATUS_PATH])],
    }


def _write_status_sheet(tmp_path, monkeypatch):
    status_file = tmp_path / STATUS_PATH
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(STATUS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_issue_branch_is_selected_without_requiring_commit_subject_reference():
    assert is_issue_182_related(
        "refs/heads/feature/issue-182-progress", [_commit(message="chore: sync")]
    )


def test_issue_branch_token_does_not_match_issue_1820():
    assert is_issue_182_branch("refs/heads/feature/issue-182-progress")
    assert not is_issue_182_branch("refs/heads/feature/issue-1820-progress")
    assert not is_issue_182_related(
        "refs/heads/feature/issue-1820-progress", [_commit(message="chore: sync")]
    )


def test_commit_reference_selects_issue_but_unrelated_push_does_not():
    assert is_issue_182_related("refs/heads/feature/docs", [_commit()])
    assert not is_issue_182_related(
        "refs/heads/feature/docs", [_commit(message="docs: unrelated update")]
    )


def test_other_branches_validate_only_related_commits_in_mixed_push():
    related = _commit("related", message="feat: implement #182", files=[STATUS_PATH])
    unrelated = _commit("unrelated", message="docs: typo fix", files=["README.md"])
    assert commits_requiring_status("refs/heads/main", [related, unrelated]) == [
        related
    ]
    assert commits_requiring_status(
        "refs/heads/feature/docs", [related, unrelated]
    ) == [related]
    assert commits_requiring_status(
        "refs/heads/feature/issue-182-fix", [related, unrelated]
    ) == [related, unrelated]
    assert missing_status_updates([related, unrelated]) == ["unrelated"]


def test_every_commit_on_issue_branch_must_update_status_sheet():
    first = _commit("first", files=[STATUS_PATH])
    second = _commit("second", message="chore: unrelated", files=["mountfile/vfs.py"])
    assert missing_status_updates([first, second]) == ["second"]
    no_webhook_file_list = _commit("third", files=[])
    assert (
        missing_status_updates(
            [no_webhook_file_list], {"third": ["docs/issue-182-status.md"]}
        )
        == []
    )


def test_merge_push_checks_first_parent_commits_not_imported_second_parent(monkeypatch):
    details = {
        "merge": {
            "commit": {"message": "merge issue update"},
            "html_url": "https://github.com/example/repo/commit/merge",
            "parents": [{"sha": "branch-commit"}, {"sha": "imported-main"}],
            "files": [{"filename": STATUS_PATH}],
        },
        "branch-commit": {
            "commit": {"message": "feat: #182 update status"},
            "html_url": "https://github.com/example/repo/commit/branch-commit",
            "parents": [{"sha": "before"}],
            "files": [{"filename": STATUS_PATH}],
        },
    }
    requested = []

    def fake_api_request(_token, method, path):
        assert method == "GET"
        sha = path.rsplit("/", 1)[-1]
        requested.append(sha)
        return details[sha]

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    commits, files_by_sha = collect_first_parent_commits(
        "token", "owner/repo", "before", "merge"
    )

    assert [commit["id"] for commit in commits] == ["branch-commit", "merge"]
    assert "imported-main" not in requested
    assert progress_sync.missing_status_updates(commits, files_by_sha) == []


def test_first_parent_walk_skips_zero_sha():
    assert collect_first_parent_commits("token", "owner/repo", "before", ZERO_SHA) == (
        [],
        {},
    )


def test_status_sync_renders_task_sheet_and_read_only_branch_behavior():
    body = render_comment(
        ref="refs/heads/feature/issue-182-progress",
        commits=[_commit(files=[STATUS_PATH])],
        status_markdown=STATUS,
        repository=REPOSITORY,
    )
    assert "<!-- issue-182-progress-sync -->" in body
    assert "`feature/issue-182-progress`" in body
    assert "не сканирует код и не запускает полный аудит" in body
    assert "только из текущего `main`" in body
    assert STATUS in body
    assert "abcdef012345" in body
    assert f"https://github.com/{REPOSITORY}/commit/abcdef0123456789" in body


def test_commit_subject_cannot_create_markdown_link_or_mention():
    body = render_comment(
        ref="refs/heads/feature/issue-182-progress",
        commits=[
            _commit(
                message="feat: [spoofed](https://example.com) @here",
                files=[STATUS_PATH],
            )
        ],
        status_markdown="| R1 | Read | Частично | Ограниченный профиль. |",
        repository=REPOSITORY,
    )
    assert r"\[spoofed\]" in body
    assert "@\u200bhere" in body
    assert "(https://example.com)" not in body


def test_main_happy_path_creates_one_canonical_comment(tmp_path, monkeypatch):
    _event(tmp_path, monkeypatch, _main_event())
    _write_status_sheet(tmp_path, monkeypatch)
    monkeypatch.setenv("CHECKED_OUT_SHA", MAIN_SHA)
    monkeypatch.setenv("PUBLISH_PROGRESS", "true")
    calls = []

    def fake_api_request(_token, method, path, payload=None):
        calls.append((method, path, payload))
        if path.endswith("/branches/main"):
            return {"commit": {"sha": MAIN_SHA}}
        if path.endswith("/issues/182/comments?per_page=100&page=1"):
            return []
        if method == "POST":
            return {"id": 1}
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    assert progress_sync.main() == 0
    writes = [
        (method, path, payload) for method, path, payload in calls if method != "GET"
    ]
    assert len(writes) == 1
    method, path, payload = writes[0]
    assert (method, path) == ("POST", f"/repos/{REPOSITORY}/issues/182/comments")
    assert "<!-- issue-182-progress-sync -->" in payload["body"]
    assert STATUS in payload["body"]
    assert any(
        path == f"/repos/{REPOSITORY}/issues/182/comments?per_page=100&page=1"
        for _, path, _ in calls
    )
    assert calls[-2][1].endswith("/branches/main")


def test_main_happy_path_updates_paginated_existing_comment(tmp_path, monkeypatch):
    _event(tmp_path, monkeypatch, _main_event())
    _write_status_sheet(tmp_path, monkeypatch)
    monkeypatch.setenv("CHECKED_OUT_SHA", MAIN_SHA)
    monkeypatch.setenv("PUBLISH_PROGRESS", "true")
    calls = []
    page_one = [
        {"id": i, "body": "ordinary comment", "user": {"login": "user"}}
        for i in range(100)
    ]

    def fake_api_request(_token, method, path, payload=None):
        calls.append((method, path, payload))
        if path.endswith("/branches/main"):
            return {"commit": {"sha": MAIN_SHA}}
        if path.endswith("/comments?per_page=100&page=1"):
            return page_one
        if path.endswith("/comments?per_page=100&page=2"):
            return [
                {
                    "id": 987,
                    "body": f"{progress_sync.COMMENT_MARKER} old status",
                    "user": {"login": "github-actions[bot]"},
                }
            ]
        if method == "PATCH":
            return {"id": 987}
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    assert progress_sync.main() == 0
    writes = [
        (method, path, payload) for method, path, payload in calls if method != "GET"
    ]
    assert len(writes) == 1
    assert writes[0][0:2] == (
        "PATCH",
        f"/repos/{REPOSITORY}/issues/comments/987",
    )
    assert STATUS in writes[0][2]["body"]
    assert any("page=2" in path for _, path, _ in calls)


def test_newer_main_head_skips_publication_before_comment_lookup(tmp_path, monkeypatch):
    _event(tmp_path, monkeypatch, _main_event())
    _write_status_sheet(tmp_path, monkeypatch)
    monkeypatch.setenv("CHECKED_OUT_SHA", MAIN_SHA)
    monkeypatch.setenv("PUBLISH_PROGRESS", "true")
    calls = []

    def fake_api_request(_token, method, path, payload=None):
        calls.append((method, path, payload))
        return {"commit": {"sha": "b" * 40}}

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    assert progress_sync.main() == 0
    assert len(calls) == 1
    assert calls[0][1].endswith("/branches/main")


def test_newer_main_head_immediately_before_write_skips_mutation(tmp_path, monkeypatch):
    _event(tmp_path, monkeypatch, _main_event())
    _write_status_sheet(tmp_path, monkeypatch)
    monkeypatch.setenv("CHECKED_OUT_SHA", MAIN_SHA)
    monkeypatch.setenv("PUBLISH_PROGRESS", "true")
    branch_checks = 0
    calls = []

    def fake_api_request(_token, method, path, payload=None):
        nonlocal branch_checks
        calls.append((method, path, payload))
        if path.endswith("/branches/main"):
            branch_checks += 1
            sha = MAIN_SHA if branch_checks == 1 else "b" * 40
            return {"commit": {"sha": sha}}
        if path.endswith("/issues/182/comments?per_page=100&page=1"):
            return []
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    assert progress_sync.main() == 0
    assert branch_checks == 2
    assert not [call for call in calls if call[0] != "GET"]


def test_deleted_branch_and_zero_sha_are_skipped_without_api_calls(
    tmp_path, monkeypatch
):
    event = {
        "ref": "refs/heads/feature/issue-182-work",
        "before": MAIN_SHA,
        "after": ZERO_SHA,
        "deleted": True,
        "commits": [],
    }
    _event(tmp_path, monkeypatch, event)
    calls = []
    monkeypatch.setattr(
        progress_sync,
        "_api_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert progress_sync.main() == 0
    assert calls == []


def test_mixed_main_push_validates_only_related_commit_and_never_publishes_in_validator(
    tmp_path, monkeypatch
):
    related = "c" * 40
    unrelated = "d" * 40
    event = {
        "ref": "refs/heads/main",
        "before": "e" * 40,
        "after": unrelated,
        "commits": [],
    }
    _event(tmp_path, monkeypatch, event)
    monkeypatch.setenv("PUBLISH_PROGRESS", "false")
    calls = []
    details = {
        unrelated: {
            "commit": {"message": "docs: unrelated typo"},
            "parents": [{"sha": related}],
            "files": [{"filename": "README.md"}],
        },
        related: {
            "commit": {"message": "feat: fix task #182"},
            "parents": [{"sha": event["before"]}],
            "files": [{"filename": STATUS_PATH}],
        },
    }

    def fake_api_request(_token, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET" and "/commits/" in path:
            return details[path.rsplit("/", 1)[-1]]
        raise AssertionError(f"Validator must not publish: {method} {path}")

    monkeypatch.setattr(progress_sync, "_api_request", fake_api_request)
    assert progress_sync.main() == 0
    assert [path.rsplit("/", 1)[-1] for _, path, _ in calls] == [unrelated, related]


def test_feature_branch_validates_read_only_and_never_publishes(tmp_path, monkeypatch):
    commit = _commit("f" * 40, message="feat: task #182", files=[STATUS_PATH])
    event = {
        "ref": "refs/heads/feature/issue-182-status",
        "before": ZERO_SHA,
        "after": commit["id"],
        "commits": [commit],
    }
    _event(tmp_path, monkeypatch, event)
    monkeypatch.setenv("PUBLISH_PROGRESS", "true")
    calls = []
    monkeypatch.setattr(
        progress_sync,
        "_api_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert progress_sync.main() == 0
    assert calls == []


def test_http_write_allowlist_rejects_issue_mutation_and_non_comment_payload(
    monkeypatch,
):
    def forbidden_urlopen(_request, timeout):
        raise AssertionError("Forbidden endpoint reached the network")

    monkeypatch.setattr(progress_sync.urllib.request, "urlopen", forbidden_urlopen)
    with pytest.raises(
        ValueError, match="Only issue #182 progress comment body writes"
    ):
        progress_sync._api_request(
            "token", "PATCH", f"/repos/{REPOSITORY}/issues/182", {"state": "closed"}
        )
    with pytest.raises(
        ValueError, match="Only issue #182 progress comment body writes"
    ):
        progress_sync._api_request(
            "token",
            "POST",
            f"/repos/{REPOSITORY}/issues/182/comments",
            {"body": "progress", "state": "closed"},
        )
    with pytest.raises(
        ValueError, match="Only issue #182 progress comment body writes"
    ):
        progress_sync._api_request(
            "token",
            "PATCH",
            f"/repos/{REPOSITORY}/issues/comments/123",
            {"body": "progress", "state": "closed"},
        )


def test_api_allowlist_accepts_only_progress_comment_endpoints(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": 123}'

    captured = []

    def fake_urlopen(request, timeout):
        captured.append((request.get_method(), request.full_url, request.data, timeout))
        return FakeResponse()

    monkeypatch.setattr(progress_sync.urllib.request, "urlopen", fake_urlopen)
    progress_sync._api_request(
        "token",
        "POST",
        f"/repos/{REPOSITORY}/issues/182/comments",
        {"body": "progress"},
    )
    progress_sync._api_request(
        "token",
        "PATCH",
        f"/repos/{REPOSITORY}/issues/comments/123",
        {"body": "updated progress"},
    )
    assert [item[0] for item in captured] == ["POST", "PATCH"]
    assert all(
        re.search(r"/issues/(?:182/comments|comments/123)$", item[1])
        for item in captured
    )
