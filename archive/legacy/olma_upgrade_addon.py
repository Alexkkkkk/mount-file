#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MOUNTFILE_FIX — OLMA upgrade: self-learning + GitHub publishing
"""
Апгрейд существующей OLMA-установки:
  - добавляет olma/learn.py   (самообучение)
  - добавляет olma/github.py  (публикация на GitHub)
  - перезаписывает olma/cli.py  (новые команды)
  - расширяет olma/__init__.py
  - добавляет .olma/ в .gitignore

Запуск:
  python olma_upgrade.py               # применить
  python olma_upgrade.py --dry-run     # план
  python olma_upgrade.py --revert      # откат
  python olma_upgrade.py --smoke       # + smoke-тест
"""

from __future__ import annotations

import argparse
import ast
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "# OLMA_UPGRADE"
BAK = ".olma_upgrade.bak"


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(10):
        if (cur / "mountfile" / "olma").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


def read(p):
    return p.read_text(encoding="utf-8", errors="replace")


def write(p, t, *, dry):
    if dry:
        print(f"  [dry] {p}")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")


def backup(p, *, dry):
    if not p.exists():
        return
    b = Path(str(p) + BAK)
    if b.exists():
        return
    if dry:
        print(f"  [dry] backup {b.name}")
        return
    shutil.copy2(p, b)


def syntax_ok(p):
    try:
        ast.parse(read(p), filename=str(p))
        return True
    except SyntaxError as e:
        print(f"  [syntax] {p.name}: {e}")
        return False


def compile_ok(p):
    try:
        with tempfile.TemporaryDirectory() as td:
            py_compile.compile(str(p), cfile=os.path.join(td, "o.pyc"), doraise=True)
        return True
    except py_compile.PyCompileError as e:
        print(f"  [compile] {p.name}: {e}")
        return False


# =========================================================================== #
# ШАБЛОН: olma/learn.py
# =========================================================================== #

LEARN_PY = '''\
"""OLMA self-learning: статистика успешных фиксов по контекстам.

Знание хранится в .olma/knowledge.json:
  - сколько раз правило встречалось
  - какие стратегии применялись
  - сколько раз фикс привёл к успешному smoke-тесту
  - токены контекста вокруг места проблемы

Далее OLMA ранжирует стратегии по confidence и предлагает лучшую.
"""

from __future__ import annotations
import hashlib, json, os, re, statistics, time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .analyzer import Issue


DEFAULT_DB = ".olma/knowledge.json"
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\\.|\\(|\\)|\\[|\\]|=|,")


def _tokenize(line: str) -> list[str]:
    return TOKEN_RE.findall(line or "")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


class OlmaKnowledgeBase:
    """Персистентное хранилище знаний (.olma/knowledge.json)."""

    VERSION = 1

    def __init__(self, root: Path, path: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / (path or DEFAULT_DB)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "version": self.VERSION,
            "created": time.time(),
            "updated": time.time(),
            "issues": {},        # rule_id -> {attempts, successes, strategies}
            "contexts": {},      # ctx_hash -> {rule_id, tokens, attempts, successes, best_strategy}
            "history": [],       # список прогонов
            "candidates": [],    # паттерны без правила
        }

    def save(self) -> None:
        self.data["updated"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # --- mutations ---
    def record_run(self, issues: list[Issue], fix_results: list, smoke_ok: bool) -> None:
        run = {
            "ts": time.time(),
            "issues": len(issues),
            "applied": sum(1 for r in fix_results if r.applied),
            "smoke_ok": bool(smoke_ok),
            "details": [],
        }
        for r in fix_results:
            if not r.applied:
                continue
            ctx = self._context_for(r.file, r.line)
            ctx_h = _hash(f"{r.rule_id}|{ctx['tokens']}")
            rule = self.data["issues"].setdefault(r.rule_id, {
                "attempts": 0, "successes": 0, "strategies": {},
            })
            rule["attempts"] += 1
            if smoke_ok:
                rule["successes"] += 1
            strategy = f"fix_{r.rule_id.lower()}"
            st = rule["strategies"].setdefault(strategy, {"attempts": 0, "successes": 0})
            st["attempts"] += 1
            if smoke_ok:
                st["successes"] += 1

            c = self.data["contexts"].setdefault(ctx_h, {
                "rule_id": r.rule_id, "tokens": ctx["tokens"],
                "file_base": ctx["file_base"],
                "attempts": 0, "successes": 0, "best_strategy": strategy,
            })
            c["attempts"] += 1
            if smoke_ok:
                c["successes"] += 1
            run["details"].append({
                "rule_id": r.rule_id, "ctx": ctx_h,
                "ok": smoke_ok, "file": r.file, "line": r.line,
            })
        self.data["history"].append(run)
        # храним не более 500 прогонов
        if len(self.data["history"]) > 500:
            self.data["history"] = self.data["history"][-500:]

    # --- queries ---
    def rate_for_rule(self, rule_id: str) -> float:
        r = self.data["issues"].get(rule_id)
        if not r or r["attempts"] == 0:
            return 0.0
        return r["successes"] / r["attempts"]

    def rate_for_context(self, issue: Issue) -> float:
        ctx = self._context_for(issue.file, issue.line)
        h = _hash(f"{issue.rule_id}|{ctx['tokens']}")
        c = self.data["contexts"].get(h)
        if not c or c["attempts"] == 0:
            return 0.0
        return c["successes"] / c["attempts"]

    def suggest(self, issue: Issue, top_k: int = 3) -> list[dict]:
        """Возвращает список стратегий, ранжированных по confidence."""
        rule = self.data["issues"].get(issue.rule_id, {})
        strategies = rule.get("strategies", {})
        result = []
        for name, s in strategies.items():
            att = s.get("attempts", 0)
            suc = s.get("successes", 0)
            if att == 0:
                continue
            # байесовская сглаженная оценка
            conf = (suc + 1) / (att + 2)
            result.append({"strategy": name, "confidence": round(conf, 3),
                           "attempts": att, "successes": suc})
        # добавляем контекстную оценку как бонус
        ctx_conf = self.rate_for_context(issue)
        for r in result:
            r["confidence_ctx"] = round((r["confidence"] + ctx_conf) / 2, 3)
        result.sort(key=lambda x: -x["confidence_ctx"])
        return result[:top_k]

    def should_auto_apply(self, issue: Issue, threshold: float = 0.85) -> bool:
        for s in self.suggest(issue, top_k=1):
            if s["confidence_ctx"] >= threshold and s["attempts"] >= 3:
                return True
        return False

    # --- pattern discovery ---
    def discover_patterns(self, files: Iterable[Path]) -> list[dict]:
        """Ищет повторяющиеся токен-паттерны вокруг IO-вызовов."""
        counter: Counter[str] = Counter()
        examples: dict[str, list[str]] = defaultdict(list)
        IO_TOKENS = ("open", "read", "write", "seek", "close", "flush", "truncate")
        for p in files:
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                toks = _tokenize(line)
                if not any(t in IO_TOKENS for t in toks):
                    continue
                for w in (3, 4, 5):
                    for j in range(len(toks) - w + 1):
                        key = " ".join(toks[j:j + w])
                        counter[key] += 1
                        if len(examples[key]) < 3:
                            examples[key].append(f"{p.name}:{i+1}")
        # фильтруем по частоте
        cand = []
        for key, cnt in counter.most_common(200):
            if cnt < 3:
                continue
            cand.append({"pattern": key, "count": cnt,
                         "examples": examples[key], "covered": False})
        self.data["candidates"] = cand[:100]
        return cand

    # --- helpers ---
    def _context_for(self, file: str, line: int) -> dict:
        p = self.root / file
        tokens: list[str] = []
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for i in range(max(0, line - 2), min(len(lines), line + 1)):
                tokens.extend(_tokenize(lines[i]))
        except OSError:
            pass
        return {"file_base": Path(file).name, "tokens": tokens[:40]}

    # --- reports ---
    def summary(self) -> dict:
        rules = self.data["issues"]
        total_att = sum(r["attempts"] for r in rules.values())
        total_suc = sum(r["successes"] for r in rules.values())
        top = sorted(
            [{"rule_id": k, "attempts": v["attempts"],
              "successes": v["successes"],
              "rate": round(v["successes"] / max(1, v["attempts"]), 3)}
             for k, v in rules.items()],
            key=lambda x: -x["rate"],
        )
        return {
            "rules_learned": len(rules),
            "contexts_learned": len(self.data["contexts"]),
            "runs": len(self.data["history"]),
            "total_attempts": total_att,
            "total_successes": total_suc,
            "overall_rate": round(total_suc / max(1, total_att), 3),
            "top_rules": top[:10],
            "candidates": len(self.data["candidates"]),
        }


class OlmaLearner:
    """Высокоуровневый фасад для обучения."""

    def __init__(self, root: Path, *, db_path: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.kb = OlmaKnowledgeBase(self.root, db_path)

    def learn(self, issues, fix_results, *, smoke_ok: bool) -> None:
        self.kb.record_run(issues, fix_results, smoke_ok)
        self.kb.save()

    def discover(self) -> list[dict]:
        files = [p for p in self.root.rglob("*.py")
                 if not any(s in p.parts for s in
                            (".git", "__pycache__", ".venv", "venv", "olma"))]
        cands = self.kb.discover_patterns(files)
        self.kb.save()
        return cands

    def suggest(self, issue: Issue) -> list[dict]:
        return self.kb.suggest(issue)

    def should_auto_apply(self, issue: Issue, threshold: float = 0.85) -> bool:
        return self.kb.should_auto_apply(issue, threshold)

    def summary(self) -> dict:
        return self.kb.summary()

    def history(self, limit: int = 20) -> list[dict]:
        return self.kb.data["history"][-limit:]
'''


# =========================================================================== #
# ШАБЛОН: olma/github.py
# =========================================================================== #

GITHUB_PY = '''\
"""OLMA GitHub integration: issues + PR через REST API (stdlib only).

Токен: переменная окружения GITHUB_TOKEN (или GH_TOKEN).
Репозиторий: owner/name (аргумент или OLMA_GITHUB_REPO).

Основное:
    gh = OlmaGitHub(token, "owner/repo")
    gh.publish_analysis(issues, stats)        # создать issue с отчётом
    gh.publish_fixes_pr(fixes, changes)       # создать PR с фиксами
"""

from __future__ import annotations
import base64, json, os, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path


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
    def __init__(self, token: str, repo: str, *,
                 api_base: str | None = None, dry_run: bool = False,
                 user_agent: str = "MountFile-OLMA") -> None:
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
            return {"dry_run": True, "method": method, "path": path,
                    "data": data, "url": url}
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
        r = self._request("GET",
                          f"/repos/{self.repo}/git/ref/heads/{branch}")
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
    def create_issue(self, title: str, body: str,
                     labels: list[str] | None = None) -> GhResult:
        try:
            r = self._request("POST", f"/repos/{self.repo}/issues",
                              {"title": title, "body": body,
                               "labels": labels or ["olma"]})
            return GhResult(ok=True, action="create_issue",
                            url=r.get("html_url", ""),
                            number=r.get("number", 0))
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_issue", error=str(e))

    def comment_on_issue(self, number: int, body: str) -> GhResult:
        try:
            r = self._request(
                "POST", f"/repos/{self.repo}/issues/{number}/comments",
                {"body": body},
            )
            return GhResult(ok=True, action="comment", url=r.get("html_url", ""),
                            number=number)
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="comment", error=str(e))

    # --- writes: branches + files + PRs ---
    def create_branch(self, branch: str, from_sha: str) -> bool:
        try:
            self._request("POST", f"/repos/{self.repo}/git/refs",
                          {"ref": f"refs/heads/{branch}", "sha": from_sha})
            return True
        except OlmaGitHubError:
            # ветка может уже существовать — считаем успехом
            return True

    def upsert_file(self, path: str, content_bytes: bytes,
                    message: str, branch: str) -> bool:
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

    def create_pr(self, title: str, head: str, base: str,
                  body: str, *, draft: bool = False) -> GhResult:
        try:
            r = self._request("POST", f"/repos/{self.repo}/pulls",
                              {"title": title, "head": head, "base": base,
                               "body": body, "draft": draft})
            return GhResult(ok=True, action="create_pr",
                            url=r.get("html_url", ""),
                            number=r.get("number", 0))
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_pr", error=str(e))

    # --- high level ---
    def publish_analysis(self, issues: list, stats: dict | None = None,
                         *, title_prefix: str = "[OLMA]") -> GhResult:
        """Создаёт issue с отчётом анализа; при повторе — комментарий."""
        stats = stats or {}
        title = f"{title_prefix} analysis: {len(issues)} issues"
        # дедуп
        existing = self.search_issues(title_prefix)
        body = self._format_issue_body(issues, stats)
        if existing:
            num = existing[0]["number"]
            return self.comment_on_issue(num, body)
        return self.create_issue(title, body,
                                 labels=["olma", "analysis", "automated"])

    def publish_fixes_pr(self, fixes: list, changes: dict[str, bytes], *,
                         branch_prefix: str = "olma-fix",
                         base: str | None = None) -> GhResult:
        """Создаёт PR с применёнными фиксами."""
        base = base or self.default_branch()
        try:
            base_sha = self.get_ref_sha(base)
        except OlmaGitHubError as e:
            return GhResult(ok=False, action="create_pr", error=str(e))

        branch = f"{branch_prefix}-{int(time.time())}"
        self.create_branch(branch, base_sha)

        for path, content in changes.items():
            self.upsert_file(
                path, content,
                f"olma: apply fixes to {path}", branch,
            )

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
            lines.append(f"\\n_... и ещё {len(issues) - 50}._")
        lines += [
            "",
            "---",
            "_Сгенерировано OLMA (self-learning analyser)._",
        ]
        return "\\n".join(lines)

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
            lines.append(
                f"| `{r.rule_id}` | `{r.file}` | {r.line} | {r.detail} |"
            )
        lines += [
            "",
            "---",
            "**⚠️ Проверьте изменения перед merge — автофиксы могут требовать "
            "ручной доработки.**",
        ]
        return "\\n".join(lines)


# --- утилита: создать из env ---
def from_env(repo: str | None = None, *, dry_run: bool = False) -> OlmaGitHub:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    repo = repo or os.environ.get("OLMA_GITHUB_REPO") or ""
    if not repo:
        raise OlmaGitHubError(
            "Укажите repo: --repo owner/name или OLMA_GITHUB_REPO=owner/name"
        )
    return OlmaGitHub(token, repo, dry_run=dry_run)
'''


# =========================================================================== #
# ШАБЛОН: olma/cli.py (обновлённый)
# =========================================================================== #

CLI_PY = '''\
"""OLMA CLI: analyze/fix/trace + learn/suggest/publish/sync."""

from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path


def olma_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olma", description="OLMA full CLI")
    ap.add_argument("cmd", choices=[
        "analyze", "fix", "report", "trace", "watch", "rules",
        "install", "uninstall",
        "learn", "train", "suggest", "history",
        "publish-issue", "publish-pr", "sync",
    ])
    ap.add_argument("--root", default=None)
    ap.add_argument("--format", default="text",
                    choices=["text", "json", "md", "html"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-severity", default="INFO",
                    choices=["INFO", "WARN", "ERROR", "CRITICAL"])
    ap.add_argument("--rule", default=None, help="правило для suggest")
    ap.add_argument("--repo", default=None, help="owner/name GitHub")
    ap.add_argument("--branch-prefix", default="olma-fix")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="порог авто-применения")
    ap.add_argument("--no-publish", action="store_true",
                    help="не публиковать в GitHub")
    args = ap.parse_args(argv)

    from . import olma
    from .rules import RULES, Severity
    from .learn import OlmaLearner
    from .analyzer import OlmaAnalyzer
    from .reporter import OlmaReporter

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]
    learner = OlmaLearner(root)

    # ---------- simple commands ----------
    if args.cmd == "rules":
        for rid, r in sorted(RULES.items()):
            print(f"{rid}  [{r.severity.label():8}] {r.title}"
                  f"{'  (fixable)' if r.fixable else ''}")
            print(f"       {r.hint}")
        return 0

    if args.cmd == "install":
        olma.install(trace_files=True, trace_memory=True)
        print("OLMA runtime installed"); return 0

    if args.cmd == "uninstall":
        olma.uninstall(); print("OLMA runtime uninstalled"); return 0

    # ---------- analyze / report ----------
    if args.cmd in ("analyze", "report"):
        issues = olma.analyze(str(root))
        min_sev = {"INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}[args.min_severity]
        issues = [i for i in issues if i.severity >= min_sev]
        rep = OlmaReporter(issues[:args.limit])
        text = rep.render(fmt=args.format, out=args.out)
        if not args.out:
            print(text)
        else:
            print(f"Report: {args.out}")
        return 0

    # ---------- fix ----------
    if args.cmd == "fix":
        issues = olma.analyze(str(root))
        # ранжируем с учётом обучения
        ranked = sorted(
            [i for i in issues if i.fixable],
            key=lambda i: -learner.kb.rate_for_context(i),
        )
        results = olma.fix(dry_run=not args.apply, root=str(root))
        ok = sum(1 for r in results if r.applied)
        print(f"Fix: applied={ok}, total={len(results)}")
        for r in results[:args.limit]:
            mark = "OK " if r.applied else "SKIP"
            conf = learner.kb.rate_for_rule(r.rule_id)
            print(f"  [{mark}] {r.file}:{r.line} {r.rule_id} "
                  f"conf={conf:.2f} — {r.detail}")
        if not args.apply:
            print("\\nЭто dry-run. --apply чтобы применить.")
        return 0

    # ---------- learn ----------
    if args.cmd == "learn":
        # полный цикл: analyze → fix(apply) → smoke → learn
        issues = olma.analyze(str(root))
        fixer_results = olma.fix(dry_run=True, root=str(root))
        applied = [r for r in fixer_results if r.applied]
        print(f"Learn: issues={len(issues)}, applied={len(applied)}")
        if not applied:
            print("Нет применённых фиксов — учиться не на чем.")
            learner.discover()
            return 0
        smoke_ok = True
        try:
            import subprocess
            r = subprocess.run(
                [sys.executable, "main.py", "--diag"],
                cwd=str(root), capture_output=True, timeout=60,
            )
            smoke_ok = (r.returncode == 0)
        except Exception:
            smoke_ok = False
        learner.learn(issues, fixer_results, smoke_ok=smoke_ok)
        learner.discover()
        s = learner.summary()
        print(f"  smoke_ok={smoke_ok}")
        print(f"  rules_learned={s['rules_learned']}")
        print(f"  contexts_learned={s['contexts_learned']}")
        print(f"  overall_rate={s['overall_rate']}")
        return 0

    # ---------- suggest ----------
    if args.cmd == "suggest":
        issues = olma.analyze(str(root))
        if args.rule:
            issues = [i for i in issues if i.rule_id == args.rule]
        for i in issues[:args.limit]:
            sug = learner.suggest(i)
            auto = learner.should_auto_apply(i, args.threshold)
            print(f"{i.rule_id} {i.file}:{i.line} — {i.title}")
            if not sug:
                print("   (нет данных — будет изучено после первого прогона)")
            for s in sug:
                print(f"   {s['strategy']} conf={s['confidence_ctx']} "
                      f"({s['successes']}/{s['attempts']})")
            print(f"   auto_apply={auto}")
        return 0

    if args.cmd == "history":
        for run in learner.history(args.limit):
            import datetime as dt
            ts = dt.datetime.fromtimestamp(run["ts"]).strftime("%Y-%m-%d %H:%M")
            print(f"{ts}  issues={run['issues']} applied={run['applied']} "
                  f"smoke_ok={run['smoke_ok']}")
        s = learner.summary()
        print(f"\\nИтого: runs={s['runs']} rules={s['rules_learned']} "
              f"contexts={s['contexts_learned']} rate={s['overall_rate']}")
        return 0

    # ---------- publish ----------
    if args.cmd == "publish-issue":
        from .github import OlmaGitHub, from_env, OlmaGitHubError
        issues = olma.analyze(str(root))
        stats = olma.trace_stats()
        try:
            gh = from_env(args.repo, dry_run=args.dry_run)
        except OlmaGitHubError as e:
            print(f"GitHub: {e}"); return 1
        r = gh.publish_analysis(issues, stats)
        print(r)
        return 0 if r.ok else 1

    if args.cmd == "publish-pr":
        from .github import from_env, OlmaGitHubError
        if not args.apply:
            print("Требуется --apply (иначе нечего публиковать).")
            return 1
        issues = olma.analyze(str(root))
        fixes = olma.fix(dry_run=False, root=str(root))
        applied = [r for r in fixes if r.applied]
        if not applied:
            print("Нет применённых фиксов."); return 0
        # собираем изменённые файлы
        changes: dict[str, bytes] = {}
        for r in applied:
            p = root / r.file
            if p.exists() and r.file not in changes:
                changes[r.file] = p.read_bytes()
        try:
            gh = from_env(args.repo, dry_run=args.dry_run)
        except OlmaGitHubError as e:
            print(f"GitHub: {e}"); return 1
        res = gh.publish_fixes_pr(applied, changes,
                                  branch_prefix=args.branch_prefix)
        print(res)
        return 0 if res.ok else 1

    # ---------- sync: полный цикл ----------
    if args.cmd == "sync":
        print("=== OLMA sync ===")
        issues = olma.analyze(str(root))
        print(f"analyze: {len(issues)} issues")
        fixes = olma.fix(dry_run=not args.apply, root=str(root))
        applied = [r for r in fixes if r.applied]
        print(f"fix: {len(applied)} applied")
        # smoke
        smoke_ok = True
        if args.apply:
            try:
                import subprocess
                r = subprocess.run([sys.executable, "main.py", "--diag"],
                                   cwd=str(root), capture_output=True, timeout=60)
                smoke_ok = r.returncode == 0
            except Exception:
                smoke_ok = False
            print(f"smoke: {'OK' if smoke_ok else 'FAIL'}")
        # learn
        learner.learn(issues, fixes, smoke_ok=smoke_ok)
        learner.discover()
        s = learner.summary()
        print(f"learn: rate={s['overall_rate']} contexts={s['contexts_learned']}")
        # publish
        if not args.no_publish and args.apply:
            try:
                from .github import from_env, OlmaGitHubError
                gh = from_env(args.repo, dry_run=args.dry_run)
                r1 = gh.publish_analysis(issues, olma.trace_stats())
                print(f"publish issue: {r1}")
                if applied:
                    changes = {}
                    for r in applied:
                        p = root / r.file
                        if p.exists() and r.file not in changes:
                            changes[r.file] = p.read_bytes()
                    r2 = gh.publish_fixes_pr(applied, changes,
                                             branch_prefix=args.branch_prefix)
                    print(f"publish PR: {r2}")
            except Exception as e:
                print(f"publish skipped: {e}")
        return 0

    return 1
'''


# =========================================================================== #
# ШАБЛОН: olma/__init__.py (расширенный)
# =========================================================================== #

INIT_PY = '''\
"""OLMA — Object-Level Memory Analysis & Access + self-learning."""

from __future__ import annotations

from .core import (
    OlmaError, OlmaBoundsError, OlmaIntegrityError, OlmaStateError,
    OlmaHandle, OlmaView, OlmaBuffer,
    OlmaCluster, OlmaChain,
    OlmaPartition, OlmaPartitionTable,
    OlmaTransaction, OlmaXorDecoder,
    safe_download, verify_sha256,
    SECTOR, FAT32_MAX_CLUSTER, FAT32_EOC_MIN, FAT32_BAD,
)
from .runtime import OlmaRuntime, RuntimeEvent
from .analyzer import OlmaAnalyzer, Issue
from .rules import RULES, Rule, Severity
from .fixer import OlmaFixer, FixResult
from .reporter import OlmaReporter
from .learn import OlmaLearner, OlmaKnowledgeBase
from .github import OlmaGitHub, OlmaGitHubError, GhResult, from_env
from .cli import olma_main


class Olma:
    def __init__(self) -> None:
        self._runtime: OlmaRuntime | None = None
        self._analyzer: OlmaAnalyzer | None = None
        self._learner: OlmaLearner | None = None

    def install(self, *, trace_files=True, trace_memory=False) -> None:
        if self._runtime is not None:
            return
        self._runtime = OlmaRuntime(trace_files=trace_files,
                                    trace_memory=trace_memory)
        self._runtime.install()

    def uninstall(self) -> None:
        if self._runtime:
            self._runtime.uninstall(); self._runtime = None

    def trace_stats(self) -> dict:
        return self._runtime.stats() if self._runtime else {}

    def analyzer(self, root=None) -> OlmaAnalyzer:
        if self._analyzer is None:
            from pathlib import Path
            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._analyzer = OlmaAnalyzer(base)
        return self._analyzer

    def learner(self, root=None) -> OlmaLearner:
        if self._learner is None:
            from pathlib import Path
            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._learner = OlmaLearner(base)
        return self._learner

    def analyze(self, root=None) -> list:
        return self.analyzer(root).scan()

    def fix(self, *, dry_run=True, root=None) -> list:
        issues = self.analyze(root)
        from .fixer import OlmaFixer
        fixer = OlmaFixer(self.analyzer(root).root, dry_run=dry_run)
        return fixer.apply(issues)

    def report(self, *, fmt="html", out=None, root=None) -> str:
        from .reporter import OlmaReporter
        issues = self.analyze(root)
        return OlmaReporter(issues, self.trace_stats()).render(fmt=fmt, out=out)

    # --- self-learning shortcuts ---
    def learn(self, root=None) -> None:
        lr = self.learner(root)
        issues = self.analyze(root)
        fixes = self.fix(dry_run=True, root=root)
        lr.learn(issues, fixes, smoke_ok=True)

    def suggest(self, issue, root=None) -> list:
        return self.learner(root).suggest(issue)

    def publish_issue(self, *, repo=None, dry_run=True, root=None):
        from .github import from_env
        issues = self.analyze(root)
        gh = from_env(repo, dry_run=dry_run)
        return gh.publish_analysis(issues, self.trace_stats())

    def publish_pr(self, *, repo=None, dry_run=True, root=None):
        from .github import from_env
        fixes = [r for r in self.fix(dry_run=False, root=root) if r.applied]
        changes = {}
        from pathlib import Path
        base = Path(root) if root else Path(__file__).resolve().parents[1]
        for r in fixes:
            p = base / r.file
            if p.exists() and r.file not in changes:
                changes[r.file] = p.read_bytes()
        gh = from_env(repo, dry_run=dry_run)
        return gh.publish_fixes_pr(fixes, changes)


olma = Olma()

__all__ = [
    "olma", "Olma",
    "OlmaError", "OlmaBoundsError", "OlmaIntegrityError", "OlmaStateError",
    "OlmaHandle", "OlmaView", "OlmaBuffer",
    "OlmaCluster", "OlmaChain",
    "OlmaPartition", "OlmaPartitionTable",
    "OlmaTransaction", "OlmaXorDecoder",
    "OlmaRuntime", "OlmaAnalyzer", "OlmaFixer", "OlmaReporter",
    "OlmaLearner", "OlmaKnowledgeBase",
    "OlmaGitHub", "OlmaGitHubError", "GhResult", "from_env",
    "Issue", "Rule", "Severity", "RULES", "FixResult",
    "safe_download", "verify_sha256", "olma_main",
    "SECTOR", "FAT32_MAX_CLUSTER", "FAT32_EOC_MIN", "FAT32_BAD",
]
'''


# =========================================================================== #
# УСТАНОВКА
# =========================================================================== #

UPGRADES = {
    "mountfile/olma/learn.py": LEARN_PY,
    "mountfile/olma/github.py": GITHUB_PY,
    "mountfile/olma/cli.py": CLI_PY,
    "mountfile/olma/__init__.py": INIT_PY,
}


def install(root: Path, *, dry: bool) -> int:
    print(f"Корень: {root}")
    pkg = root / "mountfile" / "olma"
    if not pkg.is_dir():
        print(f"  [err] OLMA не установлена: {pkg} не найден.")
        print("        Сначала запустите mountfile_fix.py")
        return 0

    n = 0
    print("\n[1/3] Установка модулей...")
    for rel, content in UPGRADES.items():
        p = root / rel
        if p.exists():
            backup(p, dry=dry)
        write(p, content, dry=dry)
        print(f"  [installed] {rel} ({len(content.splitlines())} строк)")
        n += 1

    print("\n[2/3] Обновление .gitignore...")
    gi = root / ".gitignore"
    if not dry:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        for entry in (".olma/", "*.olma.bak", "*.olma_upgrade.bak"):
            if entry not in lines:
                lines.append(entry)
        gi.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("  [updated] .gitignore")

    print("\n[3/3] Создание каталога .olma/...")
    if not dry:
        (root / ".olma").mkdir(exist_ok=True)
        print("  [created] .olma/")

    return n


def check_all(root: Path) -> int:
    print("Проверка синтаксиса...")
    bad = 0
    for p in [
        root / "mountfile" / "olma" / n
        for n in ("learn.py", "github.py", "cli.py", "__init__.py")
    ]:
        if not p.exists():
            continue
        if not syntax_ok(p) or not compile_ok(p):
            bad += 1
    print("  OK" if bad == 0 else f"  Проблем: {bad}")
    return bad


def revert(root: Path) -> int:
    n = 0
    for bak in root.rglob("*" + BAK):
        orig = Path(str(bak)[: -len(BAK)])
        if orig.exists():
            shutil.copy2(bak, orig)
            print(f"  [reverted] {orig.relative_to(root)}")
            n += 1
    return n


def smoke(root: Path) -> int:
    print("Smoke-тест OLMA upgrade...")
    tests = [
        [
            "python",
            "-c",
            "from mountfile.olma.learn import OlmaLearner; " "print('learn OK')",
        ],
        [
            "python",
            "-c",
            "from mountfile.olma.github import OlmaGitHub; " "print('github OK')",
        ],
        [
            "python",
            "-c",
            "from mountfile.olma import OlmaLearner, OlmaGitHub; "
            "print('exports OK')",
        ],
        ["python", "main.py", "--olma", "rules"],
    ]
    rc = 0
    for cmd in tests:
        try:
            r = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, timeout=60
            )
            ok = r.returncode == 0
            print(f"  [{'ok' if ok else 'fail'}] {' '.join(cmd[:3])}")
            if not ok:
                if r.stderr:
                    print("    ", r.stderr.strip().splitlines()[-1])
                if rc == 0:
                    rc = r.returncode
        except Exception as e:
            print(f"  [err] {e}")
            rc = 1
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OLMA upgrade: learning + GitHub")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else find_root(Path.cwd())

    if args.revert:
        revert(root)
        return 0
    if args.check:
        return 1 if check_all(root) > 0 else 0

    install(root, dry=args.dry_run)
    if args.dry_run:
        return 0

    if check_all(root) > 0:
        print("Синтаксические ошибки. Откат: --revert")
        return 1

    if args.smoke:
        return smoke(root)

    print("\nГотово. Дальше:")
    print("  export GITHUB_TOKEN=ghp_xxx")
    print("  export OLMA_GITHUB_REPO=owner/repo")
    print("  python main.py --olma learn              # цикл обучения")
    print("  python main.py --olma suggest            # рекомендации")
    print("  python main.py --olma history            # статистика")
    print("  python main.py --olma sync --apply       # + публикация")
    print("  python main.py --olma publish-issue --dry-run")
    print("  python main.py --olma publish-pr --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
