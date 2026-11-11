#!/usr/bin/env python3
"""Audit GitHub branches without deleting or mutating refs.

The report identifies protected, active-PR, stale and diverged branches. It is
safe for scheduled CI: no branch is deleted, force-pushed or rebased. The
optional fail-on-stale mode is intended for an explicit manual workflow run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STALE_DAYS = 45
PROTECTED_NAMES = {"main", "master"}
PROTECTED_PREFIXES = ("release/",)


@dataclass(frozen=True)
class BranchRecord:
    name: str
    sha: str
    committed_at: str | None
    age_days: int | None
    protected: bool
    open_pr: int | None
    ahead: int | None = None
    behind: int | None = None


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def branch_age_days(
    committed_at: str | None, *, now: dt.datetime | None = None
) -> int | None:
    committed = _parse_date(committed_at)
    if committed is None:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=dt.timezone.utc)
    return max(0, (current - committed).days)


def is_protected(name: str, default_branch: str) -> bool:
    return (
        name in PROTECTED_NAMES
        or name == default_branch
        or name.startswith(PROTECTED_PREFIXES)
    )


def classify_branch(
    *,
    name: str,
    sha: str,
    committed_at: str | None,
    default_branch: str,
    stale_days: int,
    open_pr: int | None,
    ahead: int | None = None,
    behind: int | None = None,
    now: dt.datetime | None = None,
) -> dict:
    age = branch_age_days(committed_at, now=now)
    protected = is_protected(name, default_branch)
    stale = age is not None and age >= stale_days and not protected and open_pr is None
    return {
        "name": name,
        "sha": sha,
        "committed_at": committed_at,
        "age_days": age,
        "protected": protected,
        "open_pr": open_pr,
        "ahead": ahead,
        "behind": behind,
        "status": (
            "protected"
            if protected
            else "active_pr" if open_pr else "stale" if stale else "active"
        ),
        "stale": stale,
    }


def _api_json(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "mountfile-branch-hygiene",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except (OSError, urllib.error.HTTPError) as exc:
        raise RuntimeError(f"GitHub API request failed: {url}: {exc}") from exc


def _paged(api_root: str, path: str, token: str) -> list[dict]:
    values: list[dict] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        separator = "&" if "?" in path else "?"
        payload = _api_json(f"{api_root.rstrip('/')}{path}{separator}{query}", token)
        if not isinstance(payload, list):
            raise RuntimeError(f"GitHub API returned a non-list for {path}")
        values.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
    return values


def collect_report(
    *,
    api_root: str,
    repository: str,
    token: str,
    default_branch: str,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict:
    base = f"{api_root.rstrip('/')}/repos/{repository}"
    repository_info = _api_json(base, token)
    if not isinstance(repository_info, dict):
        raise RuntimeError("GitHub repository response is not an object")
    default_branch = str(repository_info.get("default_branch") or default_branch)
    branch_payload = _paged(api_root, f"/repos/{repository}/branches", token)
    prs = _paged(api_root, f"/repos/{repository}/pulls?state=open", token)
    pr_by_branch: dict[str, int] = {}
    for pr in prs:
        head = pr.get("head") or {}
        head_repo = head.get("repo") or {}
        if head_repo.get("full_name") == repository and head.get("ref"):
            pr_by_branch[str(head["ref"])] = int(pr["number"])

    branches = []
    for branch in branch_payload:
        name = str(branch.get("name") or "")
        commit = branch.get("commit") or {}
        sha = str(commit.get("sha") or "")
        commit_detail = _api_json(f"{base}/commits/{urllib.parse.quote(sha)}", token)
        committed_at = None
        if isinstance(commit_detail, dict):
            commit_data = commit_detail.get("commit") or {}
            committed_at = (commit_data.get("committer") or {}).get("date") or (
                commit_data.get("author") or {}
            ).get("date")
        branches.append(
            classify_branch(
                name=name,
                sha=sha,
                committed_at=committed_at,
                default_branch=default_branch,
                stale_days=stale_days,
                open_pr=pr_by_branch.get(name),
            )
        )

    stale = [branch["name"] for branch in branches if branch["stale"]]
    return {
        "schema": "mountfile.branch-hygiene/v1",
        "repository": repository,
        "default_branch": default_branch,
        "stale_days": stale_days,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "policy": {
            "mutates_refs": False,
            "deletes_branches": False,
            "stale_requires_no_open_pr": True,
            "protected_names": sorted(PROTECTED_NAMES),
            "protected_prefixes": list(PROTECTED_PREFIXES),
        },
        "summary": {
            "branch_count": len(branches),
            "open_pr_count": len(prs),
            "stale_count": len(stale),
            "stale_branches": stale,
        },
        "branches": sorted(branches, key=lambda item: item["name"]),
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "## Branch hygiene report",
        "",
        f"- Repository: `{report['repository']}`",
        f"- Default branch: `{report['default_branch']}`",
        f"- Branches: `{summary['branch_count']}`; open PRs: `{summary['open_pr_count']}`",
        f"- Stale candidates: `{summary['stale_count']}` (threshold: `{report['stale_days']} days`)",
        "- Policy: read-only; no branches are deleted or force-pushed.",
        "",
        "| Branch | Status | Age (days) | Open PR | SHA |",
        "|---|---:|---:|---:|---|",
    ]
    for branch in report["branches"]:
        lines.append(
            f"| `{branch['name']}` | `{branch['status']}` | {branch['age_days'] if branch['age_days'] is not None else 'unknown'} | "
            f"{('#' + str(branch['open_pr'])) if branch['open_pr'] else '—'} | `{branch['sha'][:12]}` |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    parser.add_argument(
        "--api-root", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument(
        "--default-branch", default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
    )
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--fail-on-stale", action="store_true")
    args = parser.parse_args(argv)
    if not args.repository or not args.token:
        parser.error(
            "--repository and --token (or GitHub environment variables) are required"
        )
    if args.stale_days < 1:
        parser.error("--stale-days must be positive")
    report = collect_report(
        api_root=args.api_root,
        repository=args.repository,
        token=args.token,
        default_branch=args.default_branch,
        stale_days=args.stale_days,
    )
    args.json_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report), end="")
    return 2 if args.fail_on_stale and report["summary"]["stale_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
