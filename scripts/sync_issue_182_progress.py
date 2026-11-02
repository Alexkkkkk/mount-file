#!/usr/bin/env python3
"""Sync the committed issue #182 progress sheet to one GitHub issue comment.

This script intentionally does not inspect source code or run an audit. The
human-maintained status file is the source of truth; the workflow only validates
that every issue-related commit updates it. Only the current main head can
publish the canonical comment, using the status sheet fetched at that exact SHA.
Feature branches perform read-only validation, never canonical publication.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ISSUE_NUMBER = 182
STATUS_PATH = "docs/issue-182-status.md"
COMMENT_MARKER = "<!-- issue-182-progress-sync -->"
API_ROOT = "https://api.github.com"
MAX_DISPLAYED_COMMITS = 15
ZERO_SHA = "0" * 40
MAX_PUSH_COMMITS = 100
MAIN_REF = "refs/heads/main"


def is_issue_182_branch(ref: str) -> bool:
    """Match a branch token, not a prefix such as issue-1820."""
    return ref.startswith("refs/heads/") and bool(
        re.search(r"(?<![a-z0-9])issue-182(?![a-z0-9])", ref, re.I)
    )


def is_issue_182_commit(commit: dict[str, Any]) -> bool:
    """Match an individual commit explicitly naming issue #182."""
    return bool(
        re.search(r"(?:#|issue\s*[-:]?\s*)182\b", str(commit.get("message", "")), re.I)
    )


def commits_requiring_status(
    ref: str, commits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Issue branches require every commit; other branches only named commits."""
    if is_issue_182_branch(ref):
        return commits
    return [commit for commit in commits if is_issue_182_commit(commit)]


def is_issue_182_related(ref: str, commits: list[dict[str, Any]]) -> bool:
    """Whether the branch or any individual commit belongs to issue #182."""
    return is_issue_182_branch(ref) or any(map(is_issue_182_commit, commits))


def commit_updates_status_sheet(
    commit: dict[str, Any], verified_files: list[str] | None = None
) -> bool:
    """Check changed paths from the webhook or a commit-API file list."""
    changed = set(commit.get("added", []))
    changed.update(commit.get("modified", []))
    changed.update(commit.get("removed", []))
    changed.update(verified_files or [])
    return STATUS_PATH in changed


def missing_status_updates(
    commits: list[dict[str, Any]],
    verified_files_by_sha: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return commit SHAs that did not modify the canonical status sheet."""
    verified_files_by_sha = verified_files_by_sha or {}
    missing: list[str] = []
    for commit in commits:
        sha = str(commit.get("id", "unknown"))
        if not commit_updates_status_sheet(commit, verified_files_by_sha.get(sha, [])):
            missing.append(sha[:12])
    return missing


def _short_message(message: str) -> str:
    return " ".join(message.strip().splitlines()) or "(без сообщения)"


def _safe_subject(message: str) -> str:
    subject = _short_message(message).replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", subject)


def render_comment(
    *,
    ref: str,
    commits: list[dict[str, Any]],
    status_markdown: str,
    repository: str,
) -> str:
    shown = commits[-MAX_DISPLAYED_COMMITS:]
    rows = []
    for commit in shown:
        sha = str(commit.get("id", "unknown"))
        subject = _safe_subject(str(commit.get("message", "")))
        url = f"https://github.com/{repository}/commit/{sha}"
        linked_sha = f"[`{sha[:12]}`]({url})"
        rows.append(f"- {linked_sha} — {subject}")
    if len(commits) > len(shown):
        rows.insert(
            0,
            f"- Показаны последние {len(shown)} из {len(commits)} commits этого push.",
        )

    commit_list = "\n".join(rows) if rows else "- Нет commit-ов в push payload."
    branch = ref.removeprefix("refs/heads/")
    status = status_markdown.strip()
    return (
        f"{COMMENT_MARKER}\n"
        "## Issue #182 — текущий статус задач\n\n"
        f"**Ветка:** `{branch}`  \n"
        f"**Репозиторий:** `{repository}`  \n\n"
        "Автоматизация публикует committed status sheet; она не сканирует код и "
        "не запускает полный аудит. Канонический статус публикуется только из "
        "текущего `main`; другие ветки проходят read-only validation.\n\n"
        "### Commits последнего push\n\n"
        f"{commit_list}\n\n"
        "### Задачи: что готово и что осталось\n\n"
        f"{status}\n"
    )


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _api_request(token: str, method: str, path: str, payload: dict | None = None):
    # Even a caller bug must never close/edit an issue or mutate another API.
    if method != "GET":
        allowed = (
            method == "POST"
            and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/182/comments", path)
        ) or (
            method == "PATCH"
            and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/[0-9]+", path)
        )
        if (
            not allowed
            or not isinstance(payload, dict)
            or set(payload) != {"body"}
            or not isinstance(payload["body"], str)
        ):
            raise ValueError("Only issue #182 progress comment body writes are allowed")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_ROOT + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mount-file-issue-182-progress-sync",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise GitHubAPIError(
            f"GitHub API {method} {path} failed ({exc.code}): {detail}", exc.code
        ) from exc


def collect_first_parent_commits(
    token: str, repository: str, before: str, after: str
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Collect only commits introduced on the pushed branch's first-parent line."""
    if not before or not after or before in (ZERO_SHA, after) or after == ZERO_SHA:
        return [], {}

    reverse_commits: list[dict[str, Any]] = []
    files_by_sha: dict[str, list[str]] = {}
    cursor = after
    while cursor != before and len(reverse_commits) < MAX_PUSH_COMMITS:
        details = _api_request(token, "GET", f"/repos/{repository}/commits/{cursor}")
        commit_data = details.get("commit") or {}
        reverse_commits.append(
            {
                "id": cursor,
                "message": str(commit_data.get("message", "")),
                "url": str(details.get("html_url", "")),
                "added": [],
                "modified": [],
                "removed": [],
            }
        )
        files_by_sha[cursor] = [
            str(item.get("filename", "")) for item in details.get("files", [])
        ]
        parents = details.get("parents") or []
        if not parents:
            break
        # A merge commit's second-parent history was already authored on the
        # other branch; only the first-parent line belongs to this pushed branch.
        cursor = str(parents[0].get("sha", ""))

    if cursor != before:
        raise RuntimeError(
            "Could not safely identify the first-parent commits for this push "
            "(push is too large or history was rewritten)."
        )
    return list(reversed(reverse_commits)), files_by_sha


def _find_progress_comment(token: str, repository: str) -> dict | None:
    page = 1
    while page <= 10:
        path = (
            f"/repos/{repository}/issues/{ISSUE_NUMBER}/comments"
            f"?per_page=100&page={page}"
        )
        comments = _api_request(token, "GET", path)
        if not isinstance(comments, list):
            raise RuntimeError("Unexpected response while listing issue comments")
        for comment in comments:
            body = str(comment.get("body", ""))
            user = comment.get("user") or {}
            if COMMENT_MARKER in body and user.get("login") == "github-actions[bot]":
                return comment
        if len(comments) < 100:
            return None
        page += 1
    raise RuntimeError(
        "Issue #182 has too many comments to find the progress marker safely"
    )


def sync_comment(token: str, repository: str, body: str, expected_head: str) -> str:
    comment = _find_progress_comment(token, repository)
    # Check after the lookup, immediately before the one possible mutation.
    if not is_current_main_head(token, repository, expected_head):
        return "stale"
    if comment:
        comment_id = int(comment["id"])
        _api_request(
            token,
            "PATCH",
            f"/repos/{repository}/issues/comments/{comment_id}",
            {"body": body},
        )
        return "updated"
    _api_request(
        token,
        "POST",
        f"/repos/{repository}/issues/{ISSUE_NUMBER}/comments",
        {"body": body},
    )
    return "created"


def is_current_main_head(token: str, repository: str, expected_head: str) -> bool:
    """Return whether the API still reports this push as the main branch head."""
    try:
        branch = _api_request(token, "GET", f"/repos/{repository}/branches/main")
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return False
        raise
    latest = str(((branch or {}).get("commit") or {}).get("sha", ""))
    return bool(expected_head) and latest == expected_head


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not event_path or not repository or not token:
        print(
            "GITHUB_EVENT_PATH, GITHUB_REPOSITORY, and GITHUB_TOKEN are required",
            file=sys.stderr,
        )
        return 2

    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    ref = str(event.get("ref", ""))
    before = str(event.get("before", ""))
    after = str(event.get("after", ""))
    if event.get("deleted") or after == ZERO_SHA:
        print("Deleted branch/zero-SHA push; progress sync skipped.")
        return 0

    payload_commits = event.get("commits") or []
    if not payload_commits and event.get("head_commit"):
        payload_commits = [event["head_commit"]]
    if before and after and before != ZERO_SHA and before != after:
        push_commits, verified_files_by_sha = collect_first_parent_commits(
            token, repository, before, after
        )
    else:
        # First push to a new branch has no previous SHA. The event's commits
        # array is the bounded list of commits introduced with that branch.
        push_commits = payload_commits
        verified_files_by_sha = {}
        for commit in push_commits:
            if commit_updates_status_sheet(commit):
                continue
            sha = str(commit.get("id", "unknown"))
            if sha == "unknown":
                continue
            # Some push payloads omit changed-file arrays. Query only commit
            # metadata (filenames); never download source file contents.
            details = _api_request(token, "GET", f"/repos/{repository}/commits/{sha}")
            verified_files_by_sha[sha] = [
                str(item.get("filename", "")) for item in details.get("files", [])
            ]

    commits = commits_requiring_status(ref, push_commits)
    if not commits:
        print("No #182-related commits; progress validation skipped.")
        return 0

    missing = missing_status_updates(commits, verified_files_by_sha)
    if missing:
        print(
            "Every commit on an issue-182 branch, or each explicitly #182-related "
            "commit on another branch, must update docs/issue-182-status.md; "
            "missing from: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    if ref != MAIN_REF or os.environ.get("PUBLISH_PROGRESS") != "true":
        print(
            "#182 commits passed read-only validation; no canonical comment was "
            "published (only current main may publish)."
        )
        return 0

    if not after or after == ZERO_SHA or os.environ.get("CHECKED_OUT_SHA") != after:
        print("Push is not the checked-out main head; canonical publish skipped.")
        return 0
    if not is_current_main_head(token, repository, after):
        print("Main has a newer/different head; stale progress publish skipped.")
        return 0

    status_path = Path(STATUS_PATH)
    if not status_path.is_file():
        print(f"Required status sheet is missing: {STATUS_PATH}", file=sys.stderr)
        return 1
    body = render_comment(
        ref=ref,
        commits=commits,
        status_markdown=status_path.read_text(encoding="utf-8"),
        repository=repository,
    )
    action = sync_comment(token, repository, body, after)
    if action == "stale":
        print("Main advanced before comment mutation; stale publish skipped.")
        return 0
    print(f"GitHub issue #{ISSUE_NUMBER} progress comment {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
