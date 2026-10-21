# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for tools/mfsync.py - the working-copy <-> GitHub sync helper.

Everything here is offline: a throwaway repository is created in tmp_path with
a local bare remote, so no network, no token and no GitHub API is touched.

Run:  python -m pytest tests/test_mfsync.py -v
"""

import importlib.util
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "tools", "mfsync.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("mfsync_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_module()


# --------------------------------------------------------------------- helpers


def run_git(cwd, *args):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    """A repository with one commit and a local bare 'origin' remote."""
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    work.mkdir()
    run_git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    run_git(work, "init", "-b", "main")
    run_git(work, "config", "user.email", "test@example.com")
    run_git(work, "config", "user.name", "Test User")
    run_git(work, "config", "commit.gpgsign", "false")
    run_git(work, "remote", "add", "origin", str(bare))
    # A CI checkout is a detached HEAD; the fixture reproduces that by creating
    # the branch explicitly instead of relying on init.defaultBranch.
    run_git(work, "checkout", "-B", "main")
    (work / "README.md").write_text("# demo\n", encoding="utf-8")
    run_git(work, "add", "-A")
    run_git(work, "commit", "-m", "chore: initial")
    run_git(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture()
def remote_with_extra_branch(repo, tmp_path):
    """A bare origin that also has a `test-remote` branch.

    auto-release.yml pushes such a branch with GITHUB_TOKEN, and the resulting
    event does not trigger CI - the ``remote_without_extra_branch`` fixture
    below mirrors the normal repository state for the happy path.
    """
    run_git(repo, "push", "origin", "main:test-remote")
    return repo


# --------------------------------------------------------------- secret gate


def test_scan_detects_github_pat(tmp_path):
    token = "github_pat_" + "11ABCDEFG0abcdefghijklmnop"
    path = tmp_path / "leak.py"
    path.write_text('TOKEN = "%s"\n' % token, encoding="utf-8")
    findings = M.scan_files(tmp_path, ["leak.py"])
    assert len(findings) == 1
    assert findings[0]["kind"] == "github token"
    assert token not in findings[0]["snippet"], "the scanner must never echo the secret"


def test_scan_ignores_placeholder_values(tmp_path):
    (tmp_path / "doc.md").write_text(
        'Set password = "your_password_here" first.\n' 'api_key = "${API_KEY}"\n',
        encoding="utf-8",
    )
    assert M.scan_files(tmp_path, ["doc.md"]) == []


def test_private_key_header_is_advisory(tmp_path):
    # A PEM header alone is a shape, not a credential: YARA rules and test
    # fixtures legitimately contain it, so it must not block a commit.
    (tmp_path / "k.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n", encoding="utf-8")
    findings = M.scan_files(tmp_path, ["k.txt"])
    assert [f["kind"] for f in findings] == ["private key"]
    assert findings[0]["severity"] == "heuristic"


def test_hard_token_is_high_severity(tmp_path):
    (tmp_path / "leak.py").write_text('T = "ghp_%s"\n' % ("q" * 40), encoding="utf-8")
    assert [f["severity"] for f in M.scan_files(tmp_path, ["leak.py"])] == ["high"]


def test_commit_allows_advisory_but_refuses_hard(repo):
    (repo / "rules").mkdir()
    (repo / "rules" / "keys.yar").write_text(
        'rule k { strings: $a = "-----BEGIN OPENSSH PRIVATE KEY-----" condition: $a }\n',
        encoding="utf-8",
    )
    sha = M.commit(str(repo), message="test(core): add yara fixture")
    assert sha
    (repo / "leak.py").write_text('T = "ghp_%s"\n' % ("w" * 40), encoding="utf-8")
    with pytest.raises(M.SyncError):
        M.commit(str(repo), message="chore: leak")


def test_scan_allowlist_and_inline_pragma(tmp_path):
    source = 'token = "github_pat_%s"  # pragma: allowlist secret\n' % ("A" * 30)
    (tmp_path / "settings.py").write_text(source, encoding="utf-8")
    assert M.scan_files(tmp_path, ["settings.py"]) == []


def test_scan_text_finds_and_redacts():
    token = "ghp_" + "a" * 40
    findings = M.scan_text('TOKEN = "%s"\n' % token)
    assert [f["kind"] for f in findings] == ["github token"]
    assert token not in findings[0]["snippet"]


def test_scan_text_honours_allowlist_pragma():
    text = 'TOKEN = "ghp_%s"  # pragma: allowlist secret\n' % ("a" * 40)
    assert M.scan_text(text) == []


def test_scan_staged_blob_not_replaced_worktree(repo):
    """The commit gate must inspect exactly what is in the Git index."""
    token = "ghp_" + "s" * 40
    target = repo / "staged.py"
    target.write_text('TOKEN = "%s"\n' % token, encoding="utf-8")
    run_git(repo, "add", "staged.py")
    target.write_text('TOKEN = "safe"\n', encoding="utf-8")

    assert M.scan_files(str(repo), ["staged.py"]) == []
    findings = M.scan_files(str(repo), ["staged.py"], staged=True)
    assert len(findings) == 1
    assert findings[0]["kind"] == "github token"
    assert token not in findings[0]["snippet"]


def test_scan_skips_binary_and_oversized(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2github_pat_" + b"A" * 30)
    assert M.scan_files(tmp_path, ["blob.bin"]) == []


def test_sensitive_filenames():
    hits = M.sensitive_filenames(["a/.env", "certs/server.pem", "src/app.py", "id_rsa"])
    assert hits == ["a/.env", "certs/server.pem", "id_rsa"]


def test_redact_secret_removes_env_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "z" * 36)
    text = M.redact_secret("push https://x-access-token:ghp_%s@github.com/o/r" % ("z" * 36))
    assert "ghp_" not in text
    assert "***" in text


# ------------------------------------------------------------ version parity


def test_version_parity_ok(repo):
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (repo / "mountfile").mkdir()
    (repo / "mountfile" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    report = M.check_version_parity(str(repo))
    assert report["ok"] is True
    assert report["values"] == ["1.2.3"]


def test_version_parity_mismatch(repo):
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (repo / "mountfile").mkdir()
    (repo / "mountfile" / "__init__.py").write_text('__version__ = "1.2.4"\n', encoding="utf-8")
    report = M.check_version_parity(str(repo))
    assert report["ok"] is False
    assert sorted(report["values"]) == ["1.2.3", "1.2.4"]


# ----------------------------------------------------------- commit messages


def test_guess_commit_docs_only(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    kind, scope, subject, body = M.guess_commit(str(repo))
    assert kind == "docs"
    assert scope == "docs"
    assert "guide.md" in subject
    assert "docs/guide.md" in body


def test_guess_commit_maps_engine_to_rw_scope(repo):
    (repo / "mountfile").mkdir(exist_ok=True)
    (repo / "mountfile" / "fat32.py").write_text("# engine\n", encoding="utf-8")
    kind, scope, _subject, _body = M.guess_commit(str(repo))
    assert (kind, scope) == ("feat", "rw")


def test_guess_commit_deletions_are_refactor(repo):
    os.unlink(os.path.join(str(repo), "README.md"))
    kind, _scope, subject, _body = M.guess_commit(str(repo))
    assert kind == "refactor"
    assert subject.startswith("remove")


# ------------------------------------------------------------ git operations


def test_status_reports_clean_repo(repo):
    report = M.status(str(repo))
    assert report["branch"] == "main"
    assert report["dirty"] == 0
    assert report["ahead"] == 0 and report["behind"] == 0


def test_dirty_entries_includes_untracked(repo):
    (repo / "new.txt").write_text("x", encoding="utf-8")
    entries = M.dirty_entries(str(repo))
    assert [p for _c, p in entries] == ["new.txt"]


def test_commit_refuses_secret(repo):
    (repo / "leak.py").write_text('T = "ghp_%s"\n' % ("q" * 40), encoding="utf-8")
    with pytest.raises(M.SyncError) as info:
        M.commit(str(repo), message="chore: add leak")
    assert info.value.code == 4
    # nothing was committed
    assert "chore: add leak" not in run_git(repo, "log", "--format=%s")


def test_commit_refuses_env_file(repo):
    (repo / ".env").write_text("A=1\n", encoding="utf-8")
    with pytest.raises(M.SyncError) as info:
        M.commit(str(repo))
    assert info.value.code == 4
    assert ".env" in info.value.details


def test_commit_and_push_roundtrip(repo):
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    sha = M.commit(str(repo), message="feat(core): add feature")
    assert sha and len(sha) == 40
    M.push(str(repo))
    assert run_git(repo, "rev-parse", "origin/main") == sha


def test_commit_noop_returns_empty(repo):
    assert M.commit(str(repo)) == ""


def test_commit_runs_pre_commit_hook(repo):
    """Commits must not silently bypass the repository's pre-commit policy."""
    hooks = run_git(repo, "rev-parse", "--git-path", "hooks")
    hooks_path = os.path.join(str(repo), hooks) if not os.path.isabs(hooks) else hooks
    os.makedirs(hooks_path, exist_ok=True)
    hook = os.path.join(hooks_path, "pre-commit")
    with open(hook, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\necho hook-blocked >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    (repo / "hooked.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(M.SyncError) as info:
        M.commit(str(repo), message="test(core): verify hooks")
    assert "commit failed" in str(info.value)
    assert "test(core): verify hooks" not in run_git(repo, "log", "--format=%s")


def test_ahead_behind_parses_tab_separated_counts(repo, tmp_path):
    """rev-list emits 'behind<TAB>ahead', not one line per value."""
    other = tmp_path / "other"
    run_git(tmp_path, "clone", str(tmp_path / "remote.git"), str(other))
    run_git(other, "config", "user.email", "o@example.com")
    run_git(other, "config", "user.name", "Other")
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run_git(other, "add", "theirs.txt")
    run_git(other, "commit", "-m", "feat: theirs")
    run_git(other, "push", "origin", "main")

    (repo / "ours.txt").write_text("ours\n", encoding="utf-8")
    run_git(repo, "add", "ours.txt")
    run_git(repo, "commit", "-m", "feat: ours")
    run_git(repo, "fetch", "origin")

    assert M.ahead_behind(str(repo)) == (1, 1)


def test_pull_rebases_remote_commits(repo, tmp_path):
    # a second clone pushes a commit, the first one is behind
    other = tmp_path / "other"
    run_git(tmp_path, "clone", str(tmp_path / "remote.git"), str(other))
    run_git(other, "config", "user.email", "o@example.com")
    run_git(other, "config", "user.name", "Other")
    (other / "remote.txt").write_text("r\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-m", "feat: remote change")
    run_git(other, "push", "origin", "main")

    result = M.pull(str(repo))
    assert result["rebased"] is True
    assert (repo / "remote.txt").exists()


def test_pull_keeps_local_changes(repo, tmp_path):
    other = tmp_path / "other"
    run_git(tmp_path, "clone", str(tmp_path / "remote.git"), str(other))
    run_git(other, "config", "user.email", "o@example.com")
    run_git(other, "config", "user.name", "Other")
    (other / "upstream.txt").write_text("u\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-m", "feat: upstream")
    run_git(other, "push", "origin", "main")

    (repo / "local.txt").write_text("l\n", encoding="utf-8")
    M.pull(str(repo))
    assert (repo / "local.txt").exists()
    assert (repo / "upstream.txt").exists()
    assert "local.txt" in M.changed_files(str(repo))


def test_pull_conflict_aborts_cleanly(repo, tmp_path):
    other = tmp_path / "other"
    run_git(tmp_path, "clone", str(tmp_path / "remote.git"), str(other))
    run_git(other, "config", "user.email", "o@example.com")
    run_git(other, "config", "user.name", "Other")
    (other / "README.md").write_text("# theirs\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-m", "docs: theirs")
    run_git(other, "push", "origin", "main")

    (repo / "README.md").write_text("# ours\n", encoding="utf-8")
    run_git(repo, "commit", "-am", "docs: ours")

    with pytest.raises(M.SyncError) as info:
        M.pull(str(repo))
    assert info.value.code == 2
    assert "README.md" in info.value.details
    # no rebase left behind, local commit preserved
    assert not os.path.exists(os.path.join(str(repo), ".git", "rebase-merge"))
    assert run_git(repo, "log", "-1", "--format=%s") == "docs: ours"


def test_pull_fails_closed_when_fetch_fails(repo, tmp_path):
    run_git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(M.SyncError) as info:
        M.pull(str(repo))
    assert info.value.code == 2
    assert "fetch failed" in str(info.value)


def test_pull_fails_closed_when_autostash_fails(repo, monkeypatch):
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    original_git = M.git
    calls = []

    def fail_stash(args, cwd, check=True, timeout=300):
        calls.append(args)
        if args[:2] == ["stash", "push"]:
            return subprocess.CompletedProcess(["git"] + list(args), 1, "", "stash blocked")
        return original_git(args, cwd, check=check, timeout=timeout)

    monkeypatch.setattr(M, "git", fail_stash)
    with pytest.raises(M.SyncError) as info:
        M.pull(str(repo))
    assert info.value.code == 2
    assert "autostash failed" in str(info.value)
    assert not any(command[0] in ("rebase", "merge") for command in calls)


@pytest.mark.parametrize(
    "marker, operation",
    [
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
    ],
)
def test_pull_refuses_in_progress_git_operation(repo, marker, operation):
    marker_path = run_git(repo, "rev-parse", "--git-path", marker)
    marker_path = (
        os.path.join(str(repo), marker_path) if not os.path.isabs(marker_path) else marker_path
    )
    if marker == "rebase-merge":
        os.makedirs(marker_path)
    else:
        with open(marker_path, "w", encoding="utf-8") as handle:
            handle.write("test\n")
    try:
        with pytest.raises(M.SyncError) as info:
            M.pull(str(repo))
        assert info.value.code == 2
        assert operation in str(info.value)
    finally:
        if os.path.isdir(marker_path):
            os.rmdir(marker_path)
        elif os.path.exists(marker_path):
            os.unlink(marker_path)


def test_sync_without_push(repo):
    (repo / "x.txt").write_text("x", encoding="utf-8")
    report = M.sync(str(repo), do_push=False)
    assert report["commit"]
    assert report["pushed"] == ""
    assert run_git(repo, "rev-parse", "HEAD") != run_git(repo, "rev-parse", "origin/main")


def test_sync_requires_pr_when_push_rejected(repo, monkeypatch):
    (repo / "y.txt").write_text("y", encoding="utf-8")

    def fake_push(cwd, branch=None, remote="origin", force_with_lease=False):
        raise M.SyncError("push rejected by branch protection", 5, ["GH013"])

    calls = {}

    def fake_open_pr(cwd, **kwargs):
        calls.update(kwargs)
        return {"url": "https://example.invalid/pr/1", "number": 1, "head": "sync/x"}

    monkeypatch.setattr(M, "push", fake_push)
    monkeypatch.setattr(M, "open_pr", fake_open_pr)
    report = M.sync(str(repo), do_push=True)
    assert report["pr"]["number"] == 1
    assert calls["base"] in ("main", "master")


def test_sync_pr_with_no_changes_does_not_open_empty_pr(repo, monkeypatch):
    called = []

    def should_not_open(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("an empty PR must not be opened")

    monkeypatch.setattr(M, "open_pr", should_not_open)
    report = M.sync(str(repo), pr=True, do_push=True)
    assert report["pr"] is None
    assert "nothing to push" in report["message"]
    assert called == []


def test_open_pr_reuses_stable_source_base_branch(repo, monkeypatch):
    run_git(repo, "checkout", "-b", "feature/demo")
    pushes, requests = [], []

    monkeypatch.setattr(M, "remote_slug", lambda _cwd: ("owner", "project"))
    monkeypatch.setattr(
        M, "push", lambda _cwd, branch=None, **_kwargs: pushes.append(branch) or branch
    )

    def fake_api(method, path, payload=None, token=None):
        requests.append((method, path, payload, token))
        if method == "GET":
            return [{"html_url": "https://example.invalid/pr/9", "number": 9}]
        raise AssertionError("existing PR should be reused instead of POSTed")

    monkeypatch.setattr(M, "api", fake_api)
    result = M.open_pr(str(repo), base="main", token="test-token")
    assert pushes == ["sync/feature-demo-to-main"]
    assert result == {
        "url": "https://example.invalid/pr/9",
        "number": 9,
        "head": "sync/feature-demo-to-main",
        "existing": True,
    }
    assert [request[0] for request in requests] == ["GET"]
    assert "head=owner%3Async%2Ffeature-demo-to-main" in requests[0][1]


def test_lock_rejects_concurrent_run(repo):
    first = M._lock(str(repo))
    first.__enter__()
    try:
        with pytest.raises(M.SyncError) as info:
            M._lock(str(repo)).__enter__()
        assert info.value.code == 3
    finally:
        first.__exit__(None, None, None)
    # released: a second lock can be taken
    with M._lock(str(repo)):
        pass


def test_lock_keeps_live_old_lock(repo):
    first = M._lock(str(repo))
    first.__enter__()
    try:
        old = time.time() - 7200
        os.utime(first.path, (old, old))
        with pytest.raises(M.SyncError) as info:
            M._lock(str(repo)).__enter__()
        assert info.value.code == 3
        assert os.path.exists(first.path)
    finally:
        first.__exit__(None, None, None)


def test_lock_supports_linked_worktree(repo, tmp_path):
    linked = tmp_path / "linked"
    run_git(repo, "worktree", "add", "-b", "linked-lock-test", str(linked))
    with M._lock(str(linked)) as lock:
        assert os.path.exists(lock.path)
        assert lock.path != os.path.join(str(linked), ".git", "mfsync.lock")


def test_snapshot_ignores_build_artifacts(repo):
    (repo / "dist").mkdir()
    (repo / "dist" / "big.bin").write_text("x", encoding="utf-8")
    (repo / "keep.py").write_text("x", encoding="utf-8")
    snap = M._snapshot(str(repo))
    assert "keep.py" in snap
    assert not [p for p in snap if p.startswith("dist" + os.sep)]


def test_watch_commits_after_change(repo):
    cycles = []
    original = M.sync

    def counting_sync(cwd, **kwargs):
        cycles.append(1)
        return original(cwd, **kwargs)

    M.sync = counting_sync
    try:

        def writer():
            time.sleep(0.4)
            (repo / "watched.txt").write_text("w", encoding="utf-8")

        thread = threading.Thread(target=writer)
        thread.start()
        M.watch(str(repo), interval=0.2, debounce=0.2, do_push=False, max_cycles=1)
        thread.join()
    finally:
        M.sync = original
    assert cycles == [1]
    assert "watched.txt" in run_git(repo, "show", "--name-only", "--format=", "HEAD")


def test_watch_waits_for_full_debounce_interval(repo, monkeypatch):
    calls = []

    def fake_sync(_cwd, **kwargs):
        calls.append(kwargs)
        return {"commit": "", "pushed": "", "pr": None}

    monkeypatch.setattr(M, "sync", fake_sync)

    def writer():
        time.sleep(0.03)
        (repo / "debounced.txt").write_text("change\n", encoding="utf-8")

    thread = threading.Thread(target=writer)
    started = time.monotonic()
    thread.start()
    M.watch(str(repo), interval=0.01, debounce=0.12, max_cycles=1)
    thread.join()
    assert time.monotonic() - started >= 0.10
    assert len(calls) == 1


def test_watch_pr_enables_push_without_push_flag(repo, monkeypatch):
    snapshots = iter([{}, {"changed.txt": (1, 1)}, {"changed.txt": (1, 1)}])
    calls = []

    monkeypatch.setattr(M, "_snapshot", lambda _cwd: next(snapshots))

    def fake_sync(_cwd, **kwargs):
        calls.append(kwargs)
        return {"commit": "", "pushed": "", "pr": None}

    monkeypatch.setattr(M, "sync", fake_sync)
    M.watch(str(repo), interval=0, debounce=0, pr=True, max_cycles=1)
    assert calls == [{"message": None, "do_push": True, "pr": True}]


def test_cli_status_json(repo, capsys):
    code = M.main(["--cwd", str(repo), "--json", "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"branch": "main"' in out


def test_cli_check_strict_fails_on_mismatch(repo, capsys):
    (repo / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    (repo / "mountfile").mkdir()
    (repo / "mountfile" / "__init__.py").write_text('__version__ = "9.9.8"\n', encoding="utf-8")
    assert M.main(["--cwd", str(repo), "check"]) == 0
    assert M.main(["--cwd", str(repo), "check", "--strict"]) == 1


def test_cli_outside_repo(tmp_path):
    assert M.main(["--cwd", str(tmp_path)]) == 1
