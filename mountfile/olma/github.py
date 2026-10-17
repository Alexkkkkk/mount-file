# MOUNTFILE_FIX — OLMA GitHub integration
"""OLMA GitHub integration: issues + PR через REST API (stdlib only).

Токен: переменная окружения GITHUB_TOKEN (или GH_TOKEN).
Репозиторий: owner/name (аргумент или OLMA_GITHUB_REPO).

Основное:
    gh = OlmaGitHub(token, "owner/repo")
    gh.publish_analysis(issues, stats)        # создать issue с отчётом
    gh.publish_fixes_pr(fixes, changes)       # создать PR с фиксами
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "https://api.github.com"


@dataclass
class GhResult:
    ok: bool
    action: str
    url: str = ""
    number: int = 0
    error: str = ""


class OlmaGitHubError(Exception): ...


class OlmaGitHub:
    def __init__(
        self,
        token: str,
        repo: str,
        *,
        api_base: str | None = None,
        dry_run: bool = False,
        user_agent: str = "MountFile-OLMA",
    ) -> None:
        if not repo or "/" not in repo:
            raise OlmaGitHubError(f"неверный repo: {repo!r} (нужно owner/name)")
        if not token and not dry_run:
            raise OlmaGitHubError("нужен GITHUB_TOKEN")
        self.token = token
        self.repo = repo
        self.api = api_base or DEFAULT_API
        self.dry_run = dry_run
        self.ua = user_agent

    # --- core ---
    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"{self.api}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.ua,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if self.dry_run and method != "GET":
            return {
                "dry_run": True,
                "method": method,
                "path": path,
                "data": data,
                "url": url,
            }
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                txt = resp.read().decode("utf-8") or "{}"
                return json.loads(txt)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise OlmaGitHubError(f"HTTP {e.code} {method} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise OlmaGitHubError(f"network error: {e}") from e

    # --- reads ---
    def get_repo(self) -> dict:
        return self._request("GET", f"/repos/{self.repo}")

    def default_branch(self) -> str:
        try:
            return self.get_repo().get("default_branch", "main")
        except OlmaGitHubError:
            return "main"

    def get_ref_sha(self, branch: str) -> str:
        r = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        return r["object"]["sha"]

    def get_file(self, path: str, ref: str) -> dict:
        return self._request(
            "GET",
            f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}"
            f"?ref={urllib.parse.quote(ref)}",
        )

    def search_issues(self, query: str, *, state: str = "open") -> list[dict]:
        q = urllib.parse.quote(f"repo:{self.repo} {state} in:title {query}")
        r = self._request("GET", f"/search/issues?q={q}&per_page=20")
        return r.get("items", [])

    # --- writes: issues ---
    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> GhResult:
        try:
            r = self._request(
                "POST",
                f"/repos/{self.repo}/issues",
                {"title": title, "body": body, "labels": labels or ["olma"]},
            )
            return GhResult(
                ok=True,
                action="create_issue",
                url=r.get("html_url", ""),
                number=r.get("number", 0),
            )
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_issue", error=str(e))

    def comment_on_issue(self, number: int, body: str) -> GhResult:
        try:
            r = self._request(
                "POST",
                f"/repos/{self.repo}/issues/{number}/comments",
                {"body": body},
            )
            return GhResult(
                ok=True, action="comment", url=r.get("html_url", ""), number=number
            )
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="comment", error=str(e))

    # --- writes: branches + files + PRs ---
    def create_branch(self, branch: str, from_sha: str) -> bool:
        try:
            self._request(
                "POST",
                f"/repos/{self.repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": from_sha},
            )
            return True
        except OlmaGitHubError:
            # ветка может уже существовать — считаем успехом
            return True

    def upsert_file(
        self, path: str, content_bytes: bytes, message: str, branch: str
    ) -> bool:
        sha = None
        try:
            info = self.get_file(path, branch)
            sha = info.get("sha")
        except OlmaGitHubError:
            sha = None
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        try:
            self._request(
                "PUT",
                f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}",
                payload,
            )
            return True
        except OlmaGitHubError as e:
            print(f"  [gh] upsert {path}: {e}")
            return False

    def create_pr(
        self, title: str, head: str, base: str, body: str, *, draft: bool = False
    ) -> GhResult:
        try:
            r = self._request(
                "POST",
                f"/repos/{self.repo}/pulls",
                {
                    "title": title,
                    "head": head,
                    "base": base,
                    "body": body,
                    "draft": draft,
                },
            )
            return GhResult(
                ok=True,
                action="create_pr",
                url=r.get("html_url", ""),
                number=r.get("number", 0),
            )
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_pr", error=str(e))

    # --- high level ---
    def publish_analysis(
        self, issues: list, stats: dict | None = None, *, title_prefix: str = "[OLMA]"
    ) -> GhResult:
        """Создаёт issue с отчётом анализа; при повторе — комментарий."""
        stats = stats or {}
        title = f"{title_prefix} analysis: {len(issues)} issues"
        # дедуп
        existing = self.search_issues(title_prefix)
        body = self._format_issue_body(issues, stats)
        if existing:
            num = existing[0]["number"]
            return self.comment_on_issue(num, body)
        return self.create_issue(title, body, labels=["olma", "analysis", "automated"])

    def publish_fixes_pr(
        self,
        fixes: list,
        changes: dict[str, bytes],
        *,
        branch_prefix: str = "olma-fix",
        base: str | None = None,
    ) -> GhResult:
        """Создаёт PR с применёнными фиксами."""
        base = base or self.default_branch()
        try:
            base_sha = self.get_ref_sha(base)
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_pr", error=str(e))

        branch = f"{branch_prefix}-{int(time.time())}"
        self.create_branch(branch, base_sha)

        for path, content in changes.items():
            self.upsert_file(path, content, f"olma: apply fixes to {path}", branch)

        body = self._format_pr_body(fixes)
        title = f"[OLMA] auto-fixes: {len(fixes)} change(s)"
        return self.create_pr(title, branch, base, body)

    # --- formatters ---
    def _format_issue_body(self, issues: list, stats: dict) -> str:
        lines = [
            "## OLMA static analysis report",
            "",
            f"- **Issues:** {len(issues)}",
            f"- **Runtime events:** {stats.get('total', 0)}",
            f"- **Errors:** {stats.get('error', 0)}",
            "",
            "### Top findings",
            "",
            "| Sev | Rule | File | Line | Title |",
            "|-----|------|------|------|-------|",
        ]
        for i in issues[:50]:
            sev = getattr(i, "severity", 1)
            label = {1: "INFO", 2: "WARN", 3: "ERROR", 4: "CRITICAL"}.get(int(sev), "?")
            lines.append(
                f"| {label} | `{i.rule_id}` | `{i.file}` | {i.line} | {i.title} |"
            )
        if len(issues) > 50:
            lines.append(f"\n_... и ещё {len(issues) - 50}._")
        lines += [
            "",
            "---",
            "_Сгенерировано OLMA (self-learning analyser)._",
        ]
        return "\n".join(lines)

    def _format_pr_body(self, fixes: list) -> str:
        lines = [
            "## OLMA auto-fixes",
            "",
            "Этот PR сгенерирован автоматически на основе самообучения OLMA.",
            "",
            "### Применённые исправления",
            "",
            "| Rule | File | Line | Detail |",
            "|------|------|------|--------|",
        ]
        for r in fixes:
            lines.append(f"| `{r.rule_id}` | `{r.file}` | {r.line} | {r.detail} |")
        lines += [
            "",
            "---",
            "**⚠️ Проверьте изменения перед merge — автофиксы могут требовать "
            "ручной доработки.**",
        ]
        return "\n".join(lines)


# --- утилита: создать из env ---
def from_env(repo: str | None = None, *, dry_run: bool = False) -> OlmaGitHub:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    repo = repo or os.environ.get("OLMA_GITHUB_REPO") or ""
    if not repo:
        raise OlmaGitHubError(
            "Укажите repo: --repo owner/name или OLMA_GITHUB_REPO=owner/name"
        )
    return OlmaGitHub(token, repo, dry_run=dry_run)
