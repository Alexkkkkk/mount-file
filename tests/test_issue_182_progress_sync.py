from scripts.sync_issue_182_progress import (
    STATUS_PATH,
    is_issue_182_related,
    missing_status_updates,
    render_comment,
)


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


def test_issue_branch_is_selected_without_requiring_commit_subject_reference():
    assert is_issue_182_related(
        "refs/heads/feature/issue-182-progress", [_commit(message="chore: sync")]
    )


def test_commit_reference_selects_issue_but_unrelated_push_does_not():
    assert is_issue_182_related("refs/heads/feature/docs", [_commit()])
    assert not is_issue_182_related(
        "refs/heads/feature/docs", [_commit(message="docs: unrelated update")]
    )


def test_every_commit_in_push_must_update_status_sheet():
    first = _commit("first", files=[STATUS_PATH])
    second = _commit(
        "second", message="feat: related task #182", files=["mountfile/vfs.py"]
    )
    assert missing_status_updates([first, second]) == ["second"]
    no_webhook_file_list = _commit("third", files=[])
    assert (
        missing_status_updates(
            [no_webhook_file_list], {"third": ["docs/issue-182-status.md"]}
        )
        == []
    )


def test_status_sync_renders_task_sheet_and_push_commit_without_audit_claim():
    status = "| W1 | Native writers | Частично | ext3/ext4/NTFS отсутствуют. |"
    body = render_comment(
        ref="refs/heads/feature/issue-182-progress",
        commits=[_commit(files=[STATUS_PATH])],
        status_markdown=status,
        repository="Alexkkkkk/mount-file",
    )
    assert "<!-- issue-182-progress-sync -->" in body
    assert "`feature/issue-182-progress`" in body
    assert "не сканирует код и не запускает полный аудит" in body
    assert status in body
    assert "abcdef012345" in body
    assert "https://github.com/Alexkkkkk/mount-file/commit/abcdef0123456789" in body


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
        repository="Alexkkkkk/mount-file",
    )
    assert r"\[spoofed\]" in body
    assert "@\u200bhere" in body
    assert "(https://example.com)" not in body
