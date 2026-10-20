#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""mfsync - one command that keeps a working copy and GitHub in sync.

Usage
-----
    python tools/mfsync.py status          # branch, ahead/behind, dirty files, version parity
    python tools/mfsync.py check --strict  # release gates (version parity) for CI
    python tools/mfsync.py sync            # pull --rebase + commit + push (one command)
    python tools/mfsync.py sync --pr       # ... and open a pull request instead of pushing to main
    python tools/mfsync.py watch --push    # debounce watcher: auto-commit (and push) on save

Design rules
------------
* **stdlib only.** The project supports Python 3.8 and ships no new runtime
  dependencies, so this tool uses ``subprocess`` + ``urllib`` and nothing else.
* **No stored credentials.** Tokens are read from ``GH_TOKEN`` /
  ``GITHUB_TOKEN`` / ``MFSYNC_TOKEN`` at call time and never written to
  ``.git/config``, a file, a report or the terminal (all output is redacted).
* **Never leaves a half-finished rebase.** A conflicting pull is aborted, the
  autostash is restored and the conflicting paths are reported, so the working
  tree is always usable afterwards.
* **Secrets are refused before they are committed** - the scanner runs on the
  files that are about to enter the commit, not on the whole history.
* **Protected branches are handled.** If the push is rejected by a ruleset
  (``GH013``), the tool retries on a ``sync/*`` branch and opens a PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

GIT = os.environ.get("MFSYNC_GIT", "git")
API_ROOT = os.environ.get("MFSYNC_API_ROOT", "https://api.github.com")
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "MFSYNC_TOKEN")
PROTECTED_BRANCHES = ("main", "master")

# Directories and file types that never take part in the watch snapshot.
IGNORED_DIRS = (
    ".git", "__pycache__", ".venv", "venv", "env", "build", "dist", "out",
    "parts", "vfs_out", "node_modules", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".idea", ".vscode", ".mfsync", ".olma", "htmlcov",
)
IGNORED_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".img", ".bin", ".raw",
    ".dump", ".dd", ".zip", ".7z", ".log", ".coverage",
)

# (kind, pattern). Patterns are deliberately narrow: a false positive blocks a
# legitimate commit, so only unambiguous credential shapes are listed.
SECRET_PATTERNS = (
    ("github token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("credential assignment",
     re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']")),
)

# Kinds that describe a *shape* rather than a specific credential. A PEM header
# inside a YARA rule or a test fixture is not a leak, so these are reported as
# warnings and never block a commit unless the caller asks for strict mode.
HEURISTIC_KINDS = ("private key", "credential assignment")


def severity_of(kind):
    return "heuristic" if kind in HEURISTIC_KINDS else "high"


# Values that look like credentials but are documentation placeholders.
PLACEHOLDER_HINTS = (
    "your_", "your-", "xxx", "***", "...", "<", "${", "{{", "%s", "changeme",
    "example", "placeholder", "dummy", "redacted", "todo", "os.environ",
    "getenv", "secret_value", "token_here", "not_a_real", "fake", "sample",
)

# Files whose own content legitimately contains credential-shaped strings.
SCAN_ALLOWLIST = (
    "tools/mfsync.py",
    "tests/test_mfsync.py",
    "docs/sync.md",
    "SECURITY.md",
)

SENSITIVE_NAMES = (".env", "id_rsa", "id_ed25519", "id_dsa", ".netrc", ".pypirc", "credentials.json")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

MAX_SCAN_BYTES = 2 * 1024 * 1024

SCOPE_RULES = (
    (("mountfile/fat32.py", "mountfile/ext_debugfs_writer.py", "mountfile/write_engine.py",
      "mountfile/universal_fs_rw.py", "mountfile_rw_complete.py", "mountfile_rw_v2.py",
      "mountfile_universal_rw.py", "mountfile/dfir_universal"), "rw"),
    (("mountfile/dfir", "mountfile_dfir", "rules/", "mountfile/timeline.py",
      "mountfile/timestomp.py", "mountfile/unallocated.py", "mountfile/yara_scan.py"), "dfir"),
    (("mountfile/ai", "mountfile_ai.py", "mountfile/ai_format_advisor"), "ai"),
    (("mountfile/olma", "mountfile/olma_watch.py", "rules/olma"), "olma"),
    (("mountfile/tc_window.py", "mountfile/tc_style.py", "mountfile/mainwindow.py",
      "mountfile/hexview.py", "mountfile/text_editor.py", "mountfile/update_dialog.py"), "gui"),
    (("mountfile/turbo.py", "mountfile/vfs.py", "mountfile/partition.py",
      "mountfile/registry.py", "mountfile/autopick.py"), "core"),
    ((".github/workflows/", ".github/actions/"), "ci"),
    (("docs/", "mkdocs.yml", "README.md", "CHANGELOG.md"), "docs"),
    (("tests/",), "test"),
    (("tools/", "scripts/", "Makefile", "tidy_repo.py"), "tooling"),
    (("pyproject.toml", "requirements", "Dockerfile", "docker-compose", "build_exe"), "build"),
)


# --------------------------------------------------------------------------- io


class SyncError(RuntimeError):
    """A sync step failed; ``code`` is returned as the process exit status."""

    def __init__(self, message, code=1, details=None):
        RuntimeError.__init__(self, message)
        self.code = code
        self.details = details or []


def _redact(text):
    """Strip tokens from anything we are about to print or log."""
    text = str(text)
    for _kind, pattern in SECRET_PATTERNS:
        text = pattern.sub("***", text)
    # https://x-access-token:...@github.com/... -> https://***@github.com/...
    text = re.sub(r"https://[^/\s@]+@github\.com", "https://***@github.com", text)
    for name in TOKEN_ENV:
        value = os.environ.get(name)
        if value and len(value) > 8:
            text = text.replace(value, "***")
    return text


def say(text=""):
    print(_redact(text))


def ok(text):
    print("  \u2714 " + _redact(text))


def warn(text):
    print("  ! " + _redact(text))


def fail(text):
    print("  \u2716 " + _redact(text), file=sys.stderr)


# --------------------------------------------------------------------------- git


def git(args, cwd, check=True, timeout=300):
    """Run a git command and return the completed process (stdout/stderr as text)."""
    cmd = [GIT] + list(args)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
    except OSError as exc:
        raise SyncError("cannot run git: %s" % exc)
    if check and proc.returncode != 0:
        raise SyncError(
            "git %s failed: %s" % (" ".join(args), (proc.stderr or proc.stdout).strip()),
            proc.returncode,
        )
    return proc


def repo_root(path="."):
    proc = git(["rev-parse", "--show-toplevel"], path, check=False)
    if proc.returncode != 0:
        raise SyncError("%s is not inside a git repository" % os.path.abspath(path))
    return proc.stdout.strip()


def current_branch(cwd):
    proc = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, check=False)
    branch = (proc.stdout or "").strip()
    return branch or "HEAD"


def upstream_ref(cwd):
    proc = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def ahead_behind(cwd):
    if not upstream_ref(cwd):
        return 0, 0
    proc = git(["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd, check=False)
    if proc.returncode != 0:
        return 0, 0
    parts = proc.stdout.split()
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def dirty_entries(cwd):
    """Return [(status, path)] for every local modification, incl. untracked."""
    proc = git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd, check=False)
    if proc.returncode != 0:
        return []
    entries, fields, index = [], [f for f in proc.stdout.split("\0") if f], 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        code, path = field[:2], field[3:]
        if code[0] in "RC":
            index += 1  # the rename source follows
        entries.append((code, path))
    return entries


def changed_files(cwd):
    return sorted({path for _code, path in dirty_entries(cwd)})


def remote_slug(cwd):
    """Return (owner, name) of the origin remote, or ('', '')."""
    proc = git(["remote", "get-url", "origin"], cwd, check=False)
    url = (proc.stdout or "").strip()
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?$", url)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


# ------------------------------------------------------------------ version gate


VERSION_SOURCES = (
    ("pyproject.toml", re.compile(r'(?m)^\s*version\s*=\s*"([^"]+)"')),
    ("mountfile/__init__.py", re.compile(r'(?m)^\s*(?:__version__|VERSION)\s*=\s*["\']([^"\']+)["\']')),
)


def read_versions(cwd):
    versions = {}
    for relative, pattern in VERSION_SOURCES:
        path = os.path.join(cwd, relative)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                match = pattern.search(handle.read(4000))
        except OSError:
            continue
        if match:
            versions[relative] = match.group(1).strip()
    return versions


def check_version_parity(cwd):
    """Every declared version must agree, and the latest tag must match too."""
    versions = read_versions(cwd)
    values = sorted(set(versions.values()))
    latest_tag = ""
    proc = git(["describe", "--tags", "--abbrev=0"], cwd, check=False)
    if proc.returncode == 0:
        latest_tag = proc.stdout.strip()
    tag_value = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    report = {
        "versions": versions,
        "values": values,
        "ok": bool(values) and len(values) == 1,
        "tag": latest_tag,
        "tag_matches": (not tag_value) or (tag_value in values),
    }
    return report


# ---------------------------------------------------------------- secret gate


def _iter_candidate_lines(path):
    """Yield (lineno, line) with a NUL/PCRE sniff so binaries are skipped."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size == 0 or size > MAX_SCAN_BYTES:
        return
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return
    if b"\0" in blob[:4096]:
        return
    for lineno, line in enumerate(blob.decode("utf-8", "replace").splitlines(), 1):
        yield lineno, line


def _looks_placeholder(value):
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


def scan_text(text, path="<text>"):
    """Return findings for a blob of text: [{'path','line','kind','snippet'}]."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if "pragma: allowlist secret" in line or "noqa: secret" in line:
            continue
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            candidate = match.group(1) if match.groups() else match.group(0)
            if _looks_placeholder(candidate):
                continue
            snippet = line.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            findings.append({
                "path": path,
                "line": lineno,
                "kind": kind,
                "severity": severity_of(kind),
                "snippet": _redact(snippet),
            })
            break
    return findings


def scan_files(cwd, paths=None):
    """Scan the files about to be committed (all changed files by default)."""
    findings = []
    targets = paths if paths is not None else changed_files(cwd)
    for relative in targets:
        if relative.replace("\\", "/") in SCAN_ALLOWLIST:
            continue
        absolute = os.path.join(cwd, relative)
        if not os.path.isfile(absolute):
            continue
        for lineno, line in _iter_candidate_lines(absolute):
            if "pragma: allowlist secret" in line or "noqa: secret" in line:
                continue
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                candidate = match.group(1) if match.groups() else match.group(0)
                if _looks_placeholder(candidate):
                    continue
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                findings.append({
                    "path": relative, "line": lineno, "kind": kind,
                    "severity": severity_of(kind), "snippet": _redact(snippet),
                })
                break
    return findings


def sensitive_filenames(paths):
    """Files that should never be committed by name alone."""
    hits = []
    for relative in paths:
        base = os.path.basename(relative.replace("\\", "/"))
        if base in SENSITIVE_NAMES or base.lower().endswith(SENSITIVE_SUFFIXES):
            hits.append(relative)
    return hits


# ---------------------------------------------------------- commit message gen


def _scope_for(path):
    normalized = path.replace("\\", "/")
    for prefixes, scope in SCOPE_RULES:
        for prefix in prefixes:
            if normalized == prefix or normalized.startswith(prefix):
                return scope
    return "core"


def guess_commit(cwd):
    """Derive (type, scope, subject, body) from the working-tree diff."""
    entries = dirty_entries(cwd)
    paths = [path for _code, path in entries]
    deleted = [path for code, path in entries if "D" in code]
    scopes = []
    for path in paths:
        scope = _scope_for(path)
        if scope not in scopes:
            scopes.append(scope)
    scope = scopes[0] if scopes else "repo"
    if len(scopes) > 1:
        scope = "repo"

    if paths and len(deleted) == len(paths):
        kind = "refactor"
    elif paths and all(_scope_for(p) == "docs" for p in paths):
        kind = "docs"
    elif paths and all(_scope_for(p) == "test" for p in paths):
        kind = "test"
    elif paths and all(_scope_for(p) == "ci" for p in paths):
        kind = "ci"
    elif paths and all(_scope_for(p) in ("build", "tooling") for p in paths):
        kind = "build"
    else:
        kind = "feat"

    if paths and len(deleted) == len(paths):
        subject = "remove %s" % (paths[0] if len(paths) == 1 else "%d obsolete files" % len(paths))
    elif len(paths) == 1:
        name = paths[0].replace("\\", "/")
        if kind == "docs":
            subject = "update %s" % name
        elif kind == "ci":
            subject = "update %s" % os.path.basename(name)
        else:
            subject = "update %s" % os.path.basename(name)
    else:
        subject = "update %d files (%s)" % (len(paths), ", ".join(sorted({_scope_for(p) for p in paths})[:3]))

    lines = ["%s(%s): %s" % (kind, scope, subject), ""]
    for code, path in entries[:12]:
        lines.append("- %s %s" % (code.strip() or "M", path))
    if len(entries) > 12:
        lines.append("- ... and %d more" % (len(entries) - 12))
    body = "\n".join(lines)
    return kind, scope, subject, body


# --------------------------------------------------------------------- actions


class _lock(object):
    """Very small lock so two watchers cannot sync the same tree at once."""

    def __init__(self, cwd, timeout=0):
        self.path = os.path.join(cwd, ".git", "mfsync.lock")
        self.timeout = timeout
        self.held = False

    def __enter__(self):
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            self.held = True
            return self
        except FileExistsError:
            age = time.time() - os.path.getmtime(self.path)
            if age > 3600:
                os.unlink(self.path)
                return self.__enter__()
            raise SyncError("another mfsync run holds %s (%.0fs old)" % (self.path, age), 3)

    def __exit__(self, *_exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def pull(cwd, remote="origin", rebase=True):
    """fetch + rebase with autostash. Never leaves a rebase in progress."""
    result = {"fetched": False, "rebased": False, "stashed": False, "mode": "rebase" if rebase else "merge"}
    fetch = git(["fetch", "--prune", "--tags", remote], cwd, check=False)
    result["fetched"] = fetch.returncode == 0
    if fetch.returncode != 0:
        warn("fetch failed (offline?): %s" % (fetch.stderr or fetch.stdout).strip().splitlines()[:1])
        return result

    upstream = upstream_ref(cwd)
    if not upstream:
        result["skipped"] = "no upstream configured for %s" % current_branch(cwd)
        return result

    stashed = False
    if dirty_entries(cwd):
        stash = git(["stash", "push", "--include-untracked", "-m", "mfsync autostash"], cwd, check=False)
        stashed = stash.returncode == 0
        result["stashed"] = stashed

    step = ["rebase", upstream] if rebase else ["merge", "--no-edit", upstream]
    run = git(step, cwd, check=False)
    if run.returncode != 0:
        conflicts = git(["diff", "--name-only", "--diff-filter=U"], cwd, check=False).stdout.split()
        git(["rebase", "--abort"] if rebase else ["merge", "--abort"], cwd, check=False)
        if stashed:
            git(["stash", "pop"], cwd, check=False)
        raise SyncError(
            "pull stopped: %s conflicts with %s" % (current_branch(cwd), upstream),
            2,
            conflicts or [_redact((run.stderr or run.stdout).strip())],
        )
    result["rebased"] = True

    if stashed:
        pop = git(["stash", "pop"], cwd, check=False)
        if pop.returncode != 0:
            raise SyncError(
                "local changes could not be re-applied on top of %s" % upstream, 2,
                git(["diff", "--name-only", "--diff-filter=U"], cwd, check=False).stdout.split(),
            )
    return result


def stage_all(cwd):
    git(["add", "--all"], cwd)


def commit(cwd, message=None, kind=None, scope=None, scan=True, allow_sensitive=False):
    """Stage everything and commit. Returns the new sha, or '' if nothing to do."""
    stage_all(cwd)
    staged = git(["diff", "--cached", "--name-only"], cwd, check=False).stdout.split()
    if not staged:
        return ""

    if scan:
        bad_names = sensitive_filenames(staged)
        if bad_names and not allow_sensitive:
            raise SyncError(
                "refusing to commit credential-like files", 4,
                bad_names,
            )
        findings = scan_files(cwd, staged)
        blocking = [f for f in findings if f["severity"] == "high"]
        for finding in findings:
            if finding["severity"] != "high":
                warn("possible %s (not blocking): %s:%s" % (
                    finding["kind"], finding["path"], finding["line"]))
        if blocking:
            raise SyncError("refusing to commit %d secret(s)" % len(blocking), 4,
                            ["%s:%s [%s] %s" % (f["path"], f["line"], f["kind"], f["snippet"])
                             for f in blocking])

    if not message:
        guessed_kind, guessed_scope, subject, body = guess_commit(cwd)
        kind = kind or guessed_kind
        scope = scope or guessed_scope
        message = "%s(%s): %s\n\n%s" % (kind, scope, subject, body)

    result = git(["commit", "--no-verify", "-m", message], cwd, check=False)
    if result.returncode != 0:
        raise SyncError("commit failed: %s" % (result.stderr or result.stdout).strip())
    return git(["rev-parse", "HEAD"], cwd).stdout.strip()


def needs_pr_fallback(output):
    lowered = (output or "").lower()
    return any(token in lowered for token in ("gh013", "protected branch", "required status check",
                                              "changes must be made through a pull request",
                                              "ruleset", "pull request"))


def push(cwd, branch=None, remote="origin", force_with_lease=False):
    branch = branch or current_branch(cwd)
    args = ["push", remote, "HEAD:refs/heads/%s" % branch]
    if force_with_lease:
        args.insert(2, "--force-with-lease")
    proc = git(args, cwd, check=False)
    if proc.returncode != 0:
        output = (proc.stderr or "") + (proc.stdout or "")
        if needs_pr_fallback(output):
            raise SyncError("push rejected by branch protection", 5, [_redact(output.strip())])
        raise SyncError("push failed: %s" % _redact(output.strip()))
    return branch


def api(method, path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        API_ROOT + path, data=data, method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "mfsync",
            "Authorization": "Bearer %s" % token if token else "",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SyncError("GitHub API %s %s -> %s: %s" % (method, path, exc.code, _redact(body)[:400]), exc.code)
    except urllib.error.URLError as exc:
        raise SyncError("GitHub API unreachable: %s" % exc.reason, 6)


def token_from_env():
    for name in TOKEN_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def open_pr(cwd, base=None, title=None, body=None, branch=None, token=None):
    """Push HEAD to a sync/* branch and open a pull request against ``base``."""
    owner, name = remote_slug(cwd)
    if not owner or not name:
        raise SyncError("origin is not a github.com remote", 6)
    token = token or token_from_env()
    if not token:
        raise SyncError("no token in %s - cannot open a pull request" % "/".join(TOKEN_ENV), 6)

    source = current_branch(cwd)
    base = base or (PROTECTED_BRANCHES[0] if _remote_branch_exists(cwd, "main") else "master")
    head_branch = branch or ("sync/%s-%s" % (source, time.strftime("%Y%m%d-%H%M%S")))
    push(cwd, head_branch)

    if not title:
        _kind, _scope, subject, _body = guess_commit(cwd) if dirty_entries(cwd) else ("chore", source, "sync %s" % source, "")
        title = subject
    if not body:
        log = git(["log", "--oneline", "%s..HEAD" % ("origin/%s" % base)], cwd, check=False).stdout.strip()
        body = "Автоматическая синхронизация `%s` → `%s`.\n\n```\n%s\n```\n" % (source, base, log or "no new commits")

    try:
        created = api("POST", "/repos/%s/%s/pulls" % (owner, name), {
            "head": head_branch, "base": base, "title": title, "body": body,
        }, token=token)
        return {"url": created.get("html_url", ""), "number": created.get("number"), "head": head_branch}
    except SyncError as exc:
        if exc.code == 422:
            found = api("GET", "/repos/%s/%s/pulls?state=open&head=%s:%s" % (owner, name, owner, head_branch), token=token)
            if found:
                return {"url": found[0].get("html_url", ""), "number": found[0].get("number"), "head": head_branch, "existing": True}
        raise


def _remote_branch_exists(cwd, branch):
    proc = git(["ls-remote", "--heads", "origin", branch], cwd, check=False)
    return bool((proc.stdout or "").strip())


def status(cwd):
    branch = current_branch(cwd)
    ahead, behind = ahead_behind(cwd)
    entries = dirty_entries(cwd)
    report = {
        "root": cwd,
        "branch": branch,
        "upstream": upstream_ref(cwd),
        "ahead": ahead,
        "behind": behind,
        "dirty": len(entries),
        "files": entries[:20],
        "version": check_version_parity(cwd),
        "remote": remote_slug(cwd),
        "has_token": bool(token_from_env()),
    }
    return report


def print_status(report):
    say("mfsync \u2014 %s" % report["root"])
    say("  branch   : %s%s" % (report["branch"], "  \u2192 " + report["upstream"] if report["upstream"] else "  (no upstream)"))
    say("  sync     : %d ahead, %d behind" % (report["ahead"], report["behind"]))
    if report["dirty"]:
        say("  local    : %d changed file(s)" % report["dirty"])
        for code, path in report["files"]:
            say("             %s %s" % (code, path))
        if report["dirty"] > len(report["files"]):
            say("             ... and %d more" % (report["dirty"] - len(report["files"])))
    else:
        say("  local    : clean")
    version = report["version"]
    versions = version["versions"] or {}
    if version["ok"]:
        say("  version  : %s (parity OK)" % version["values"][0])
    else:
        say("  version  : MISMATCH %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(versions.items())))
    if version["tag"]:
        say("  tag      : %s%s" % (version["tag"], "" if version["tag_matches"] else "  (differs from version)"))
    say("  token    : %s" % ("available in env" if report["has_token"] else "not set (PR mode disabled)"))


def sync(cwd, message=None, kind=None, scope=None, do_push=True, pr=False, base=None,
         scan=True, remote="origin", allow_empty=False):
    """pull --rebase, commit, push (with automatic PR fallback). Returns a report."""
    report = {"pulled": None, "commit": "", "pushed": "", "pr": None}
    version = check_version_parity(cwd)
    report["version"] = version
    if not version["ok"]:
        warn("version parity broken: %s" % ", ".join("%s=%s" % (k, v) for k, v in version["versions"].items()))

    try:
        report["pulled"] = pull(cwd, remote=remote)
    except SyncError as exc:
        raise SyncError(str(exc), exc.code, exc.details)

    sha = commit(cwd, message=message, kind=kind, scope=scope, scan=scan)
    report["commit"] = sha
    if not sha and not allow_empty:
        report["message"] = "nothing to commit"

    if not do_push:
        return report

    branch = current_branch(cwd)
    if pr or not upstream_ref(cwd) or branch in PROTECTED_BRANCHES and needs_pr_by_policy(cwd):
        # Explicit PR mode, or a branch protection ruleset is active on the base.
        report["pr"] = open_pr(cwd, base=base or branch if branch in PROTECTED_BRANCHES else base)
        return report

    try:
        report["pushed"] = push(cwd, branch=branch, remote=remote)
    except SyncError as exc:
        if exc.code == 5:
            warn("branch protection rejected the push - opening a pull request instead")
            report["pr"] = open_pr(cwd, base=base or branch)
        else:
            raise
    return report


def needs_pr_by_policy(cwd):
    """True when main/master is known to require pull requests."""
    proc = git(["config", "--get", "mfsync.prOnly"], cwd, check=False)
    return (proc.stdout or "").strip() in ("1", "true", "yes")


def watch(cwd, interval=1.0, debounce=2.0, do_push=False, pr=False, max_cycles=None, message=None):
    """Debounce watcher: after the tree has been quiet for ``debounce`` seconds, sync."""
    say("watching %s (debounce %.1fs, push=%s, Ctrl+C to stop)" % (cwd, debounce, do_push))
    snapshot = _snapshot(cwd)
    cycles = 0
    while True:
        time.sleep(interval)
        current = _snapshot(cwd)
        if current == snapshot:
            continue
        # wait for quiet
        pending = current
        while True:
            time.sleep(interval)
            newer = _snapshot(cwd)
            if newer == pending:
                break
            pending = newer
        snapshot = pending
        changed = sorted(set(pending) ^ set(snapshot)) or ["(content)"]
        say("\n[%s] %d path(s) changed" % (time.strftime("%H:%M:%S"), len(changed)))
        try:
            result = sync(cwd, message=message, do_push=do_push, pr=pr)
            if result["commit"]:
                ok("commit %s" % result["commit"][:8])
            else:
                ok("nothing to commit")
            if result["pr"]:
                ok("pull request: %s" % result["pr"]["url"])
            elif result["pushed"]:
                ok("pushed %s" % result["pushed"])
        except SyncError as exc:
            fail(str(exc))
            for line in exc.details:
                say("    " + str(line))
        cycles += 1
        if max_cycles and cycles >= max_cycles:
            return cycles


def _snapshot(cwd):
    snapshot = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in files:
            if name.endswith(IGNORED_SUFFIXES):
                continue
            path = os.path.join(root, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            snapshot[os.path.relpath(path, cwd)] = (int(info.st_mtime_ns), info.st_size)
    return snapshot


# ------------------------------------------------------------------------ cli


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mfsync",
        description="Keep the mount-file working copy and GitHub in sync.",
    )
    parser.add_argument("--cwd", default=".", help="repository path (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="branch, ahead/behind, dirty files, version parity")

    check = sub.add_parser("check", help="release/sync gates (version parity)")
    check.add_argument("--strict", action="store_true", help="non-zero exit on version mismatch")

    for name, help_text in (("sync", "pull --rebase + commit + push"),):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("-m", "--message", help="explicit commit message")
        node.add_argument("--type", dest="kind", help="conventional type override (feat/fix/docs/...)")
        node.add_argument("--scope", help="conventional scope override")
        node.add_argument("--no-push", action="store_true", help="commit locally, do not push")
        node.add_argument("--pr", action="store_true", help="open a pull request instead of pushing")
        node.add_argument("--base", help="PR base branch (default: current/main)")
        node.add_argument("--no-scan", action="store_true", help="skip the secret scanner (not recommended)")
        node.add_argument("--remote", default="origin")

    commit_node = sub.add_parser("commit", help="stage everything and commit (no network)")
    commit_node.add_argument("-m", "--message")
    commit_node.add_argument("--type", dest="kind")
    commit_node.add_argument("--scope")
    commit_node.add_argument("--no-scan", action="store_true")

    pull_node = sub.add_parser("pull", help="fetch + rebase with autostash")
    pull_node.add_argument("--merge", action="store_true", help="merge instead of rebase")

    push_node = sub.add_parser("push", help="push the current branch")
    push_node.add_argument("--branch")
    push_node.add_argument("--remote", default="origin")
    push_node.add_argument("--force-with-lease", action="store_true")

    pr_node = sub.add_parser("pr", help="push to sync/* and open a pull request")
    pr_node.add_argument("--base")
    pr_node.add_argument("--title")
    pr_node.add_argument("--body")

    watch_node = sub.add_parser("watch", help="debounce watcher")
    watch_node.add_argument("--interval", type=float, default=1.0)
    watch_node.add_argument("--debounce", type=float, default=2.0)
    watch_node.add_argument("--push", action="store_true", help="push after each cycle")
    watch_node.add_argument("--pr", action="store_true", help="open a PR after each cycle")
    watch_node.add_argument("--max-cycles", type=int, default=None)
    watch_node.add_argument("-m", "--message")

    scan_node = sub.add_parser("scan", help="scan changed files for secrets")
    scan_node.add_argument("--all", action="store_true", help="scan every tracked file")
    scan_node.add_argument("--strict-heuristics", action="store_true",
                           help="also fail on PEM headers / credential assignments")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    command = args.command or "status"
    try:
        cwd = repo_root(args.cwd)
    except SyncError as exc:
        fail(str(exc))
        return 1

    try:
        if command == "status":
            report = status(cwd)
            if args.json:
                say(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print_status(report)
            return 0

        if command == "check":
            report = check_version_parity(cwd)
            if args.json:
                say(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                if report["ok"]:
                    ok("version parity: %s" % report["values"][0])
                else:
                    fail("version parity broken: %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(report["versions"].items())))
                if report["tag"]:
                    say("  latest tag: %s%s" % (report["tag"], "" if report["tag_matches"] else "  (differs from __version__)"))
            return 0 if report["ok"] or not args.strict else 1

        if command == "scan":
            paths = None
            if args.all:
                paths = git(["ls-files"], cwd, check=False).stdout.split()
            findings = scan_files(cwd, paths)
            blocking = [f for f in findings if f["severity"] == "high"]
            advisory = [f for f in findings if f["severity"] != "high"]
            if args.json:
                say(json.dumps({"blocking": blocking, "advisory": advisory}, indent=2, ensure_ascii=False))
            else:
                if not findings:
                    ok("no secrets in %s" % ("tracked files" if args.all else "changed files"))
                for finding in blocking:
                    fail("%s:%s [%s] %s" % (finding["path"], finding["line"], finding["kind"], finding["snippet"]))
                if advisory:
                    warn("%d PEM header / credential-shaped line(s) - review, not blocking" % len(advisory))
                    for finding in advisory[:20]:
                        say("      %s:%s [%s]" % (finding["path"], finding["line"], finding["kind"]))
                if blocking:
                    say("    rotate the credential, move it to a GitHub secret, or add")
                    say("    '# pragma: allowlist secret' to a documented placeholder line.")
            if blocking:
                return 4
            if advisory and args.strict_heuristics:
                return 4
            return 0

        if command == "commit":
            with _lock(cwd):
                sha = commit(cwd, message=args.message, kind=args.kind, scope=args.scope, scan=not args.no_scan)
            if sha:
                ok("commit %s" % sha[:8])
            else:
                ok("nothing to commit")
            return 0

        if command == "pull":
            with _lock(cwd):
                result = pull(cwd, rebase=not args.merge)
            if result.get("skipped"):
                warn(result["skipped"])
            else:
                ok("pulled %s%s" % ("rebase" if result["mode"] == "rebase" else "merge",
                                    " (autostash restored)" if result["stashed"] else ""))
            return 0

        if command == "push":
            with _lock(cwd):
                branch = push(cwd, branch=args.branch, remote=args.remote, force_with_lease=args.force_with_lease)
            ok("pushed %s" % branch)
            return 0

        if command == "pr":
            with _lock(cwd):
                created = open_pr(cwd, base=args.base, title=args.title, body=args.body)
            ok("pull request #%s %s" % (created.get("number"), created["url"]))
            return 0

        if command == "sync":
            with _lock(cwd):
                report = sync(cwd, message=args.message, kind=args.kind, scope=args.scope,
                              do_push=not args.no_push, pr=args.pr, base=args.base,
                              scan=not args.no_scan, remote=args.remote)
            if args.json:
                say(json.dumps(report, indent=2, ensure_ascii=False))
                return 0
            if report["pulled"] and report["pulled"].get("skipped"):
                warn(report["pulled"]["skipped"])
            if report["commit"]:
                ok("commit %s" % report["commit"][:8])
            else:
                ok("nothing to commit")
            if report["pr"]:
                ok("pull request #%s %s" % (report["pr"].get("number"), report["pr"]["url"]))
            elif report["pushed"]:
                ok("pushed %s" % report["pushed"])
            else:
                ok("not pushed (--no-push)")
            return 0

        if command == "watch":
            return 0 if watch(cwd, interval=args.interval, debounce=args.debounce,
                              do_push=args.push, pr=args.pr, max_cycles=args.max_cycles,
                              message=args.message) else 0

        fail("unknown command: %s" % command)
        return 1
    except SyncError as exc:
        fail(str(exc))
        for line in exc.details:
            say("    " + str(line))
        if exc.code == 2:
            say("    resolve the files above by hand, then run: python tools/mfsync.py sync")
        if exc.code == 4:
            say("    move the value to an environment variable / GitHub secret, or add")
            say("    '# pragma: allowlist secret' to a documented placeholder line.")
        if exc.code == 5:
            say("    branch protection is active: python tools/mfsync.py sync --pr")
        return exc.code
    except KeyboardInterrupt:
        say("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
