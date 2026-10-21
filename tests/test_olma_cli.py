# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the OLMA command line and its reporting/learning façade.

`mountfile/olma/cli.py` sat at 2% coverage — the CLI had never been invoked
by a test at all. These tests drive every sub-command through
``olma_main()`` against small temporary project trees, so nothing touches
the real repository, the network or GitHub.

Run:  python -m pytest tests/test_olma_cli.py -v
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from mountfile.olma import cli as olma_cli
from mountfile.olma import olma as facade
from mountfile.olma.analyzer import Issue, OlmaAnalyzer
from mountfile.olma.fixer import FixResult
from mountfile.olma.learn import OlmaKnowledgeBase, OlmaLearner
from mountfile.olma.reporter import OlmaReporter
from mountfile.olma.rules import RULES, Rule, Severity


@pytest.fixture(autouse=True)
def fresh_facade():
    """The module-level ``olma`` singleton caches its analyzer per root."""
    facade._runtime = None
    facade._analyzer = None
    facade._learner = None
    yield
    facade._runtime = None
    facade._analyzer = None
    facade._learner = None


@pytest.fixture()
def project(tmp_path):
    """A tiny project that triggers at least one rule."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(
        "def load(p):\n"
        "    f = open(p)\n"  # OL001: open() without with
        "    data = f.read()\n"  # OL002/OL009: read() with no size
        "    return data\n",
        encoding="utf-8",
    )
    (root / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _issue(rule_id="OL001", severity=2, fixable=True):
    return Issue(
        rule_id=rule_id,
        title="open() без with",
        severity=severity,
        file="mod.py",
        line=2,
        col=4,
        snippet="f = open(p)",
        hint="Оберните open() в with",
        fixable=fixable,
    )


# ============================================================ 1. rules dump


def test_rules_command_lists_every_rule(capsys):
    assert olma_cli.olma_main(["rules"]) == 0
    out = capsys.readouterr().out
    for rid in ("OL001", "OL011", "OL020"):
        assert rid in out
    assert "(fixable)" in out
    assert out.count("OL0") >= len(RULES)


def test_severity_labels_and_ordering():
    assert Severity.INFO.label() == "INFO"
    assert Severity.CRITICAL.label() == "CRITICAL"
    assert Severity.CRITICAL > Severity.ERROR > Severity.WARN > Severity.INFO
    assert isinstance(RULES["OL001"], Rule)
    assert RULES["OL011"].severity is Severity.CRITICAL
    assert RULES["OL011"].fixable is True


# ==================================================== 2. analyze / report


def test_analyze_on_project_emits_issue_rows(capsys, project):
    assert olma_cli.olma_main(["analyze", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "OL00" in out or "issues" in out.lower()


def test_analyze_respects_min_severity(capsys, project, monkeypatch):
    calls = {}

    def fake_analyze(root=None):
        calls["root"] = root
        return [_issue(severity=1), _issue(rule_id="OL011", severity=4)]

    monkeypatch.setattr(facade, "analyze", fake_analyze)
    assert (
        olma_cli.olma_main(
            ["analyze", "--root", str(project), "--min-severity", "CRITICAL"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "OL001" not in out, "INFO issue must be filtered out"
    assert "OL011" in out


def test_analyze_respects_limit(capsys, monkeypatch):
    monkeypatch.setattr(
        facade, "analyze", lambda root=None: [_issue(), _issue(rule_id="OL002")]
    )
    assert olma_cli.olma_main(["analyze", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "OL002" not in out


@pytest.mark.parametrize("fmt", ["text", "json", "md", "html"])
def test_report_renders_every_format_to_stdout(fmt, capsys, project):
    assert olma_cli.olma_main(["report", "--root", str(project), "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert out.strip()
    if fmt == "json":
        json.loads(out)


def test_report_writes_file_when_out_given(tmp_path, capsys, project):
    out = tmp_path / "report.md"
    assert (
        olma_cli.olma_main(
            ["report", "--root", str(project), "--format", "md", "--out", str(out)]
        )
        == 0
    )
    assert "Report:" in capsys.readouterr().out
    assert out.exists() and "OL" in out.read_text(encoding="utf-8")


# ============================================================== 3. reporter


@pytest.mark.parametrize("fmt", ["text", "json", "markdown", "md", "html"])
def test_reporter_render_all_formats_including_aliases(fmt):
    rep = OlmaReporter([_issue(), _issue(rule_id="OL011", severity=4)], {"total": 3})
    text = rep.render(fmt=fmt)
    assert text.strip()


def test_reporter_alias_htm_and_unknown_format():
    rep = OlmaReporter([])
    assert "html" in rep.render(fmt="htm").lower()
    with pytest.raises(ValueError, match="unsupported format"):
        rep.render(fmt="pdf")


def test_reporter_summary_counts_by_severity_and_rule():
    rep = OlmaReporter([_issue(), _issue(rule_id="OL011", severity=4)])
    summary = rep._summary()
    assert summary["total"] == 2
    assert summary["by_severity"]["WARN"] == 1
    assert summary["by_severity"]["CRITICAL"] == 1
    assert summary["by_rule"] == {"OL001": 1, "OL011": 1}


def test_reporter_writes_out_file(tmp_path):
    target = tmp_path / "r.html"
    text = OlmaReporter([_issue()]).render(fmt="html", out=str(target))
    assert target.read_text(encoding="utf-8") == text


def test_reporter_escapes_html():
    evil = Issue(
        rule_id="OL001",
        title="<script>alert(1)</script>",
        severity=2,
        file="<b>.py",
        line=1,
        col=0,
        snippet="<img src=x>",
        hint="&",
        fixable=True,
    )
    html = OlmaReporter([evil]).render(fmt="html")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;" in html or "&amp;" in html


# =========================================================== 4. fix (dry-run)


def test_fix_dry_run_reports_and_changes_nothing(capsys, project):
    before = (project / "mod.py").read_text(encoding="utf-8")
    assert olma_cli.olma_main(["fix", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Fix results" in out
    assert "dry-run" in out
    assert (project / "mod.py").read_text(encoding="utf-8") == before


def test_fix_apply_reports_counts(capsys, monkeypatch):
    """--apply passes dry_run=False into the façade (patched: no real edits)."""
    seen = {}

    def fake_fix(*, dry_run=True, root=None):
        seen["dry_run"] = dry_run
        return [
            FixResult(file="mod.py", line=2, rule_id="OL001", applied=True, detail="d")
        ]

    monkeypatch.setattr(facade, "fix", fake_fix)
    assert olma_cli.olma_main(["fix", "--apply"]) == 0
    out = capsys.readouterr().out
    assert seen["dry_run"] is False
    assert "applied=1" in out
    assert "[OK ]" in out


def test_fix_lists_skipped_results(capsys, monkeypatch):
    monkeypatch.setattr(
        facade,
        "fix",
        lambda **kw: [
            FixResult(
                file="a.py", line=1, rule_id="OL002", applied=False, detail="skip"
            )
        ],
    )
    assert olma_cli.olma_main(["fix"]) == 0
    assert "[SKIP]" in capsys.readouterr().out


# ============================================== 5. learn / train / suggest


def test_learn_without_applied_fixes_explains_and_discovers(capsys, project):
    assert olma_cli.olma_main(["learn", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Learn:" in out
    assert "учиться не на чем" in out


def test_train_is_an_alias_of_learn(capsys, project):
    assert olma_cli.olma_main(["train", "--root", str(project)]) == 0
    assert "Learn:" in capsys.readouterr().out


def test_learn_with_applied_fixes_prints_summary(capsys, monkeypatch):
    monkeypatch.setattr(facade, "analyze", lambda root=None: [_issue()])
    monkeypatch.setattr(
        facade,
        "fix",
        lambda **kw: [FixResult(file="mod.py", line=2, rule_id="OL001", applied=True)],
    )

    class Learner:
        def __init__(self, root):
            self.root = root

        def learn(self, issues, fixes, *, smoke_ok):
            Learner.learned = (len(issues), len(fixes), smoke_ok)

        def discover(self):
            return []

        def summary(self):
            return {"rules_learned": 1, "contexts_learned": 2, "overall_rate": 0.5}

    monkeypatch.setattr("mountfile.olma.learn.OlmaLearner", Learner)
    assert olma_cli.olma_main(["learn"]) == 0
    out = capsys.readouterr().out
    assert "rules_learned=1" in out
    assert "contexts_learned=2" in out
    assert "overall_rate=0.5" in out
    assert Learner.learned[0] == 1 and Learner.learned[1] == 1


def test_suggest_prints_rows_and_auto_apply_flag(capsys, project):
    assert olma_cli.olma_main(["suggest", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "auto_apply=" in out


def test_suggest_filters_by_rule(capsys, monkeypatch):
    monkeypatch.setattr(
        facade, "analyze", lambda root=None: [_issue(), _issue(rule_id="OL011")]
    )
    assert olma_cli.olma_main(["suggest", "--rule", "OL011"]) == 0
    out = capsys.readouterr().out
    assert "OL011" in out
    assert "OL001" not in out


def test_suggest_reports_empty_knowledge(capsys, monkeypatch, project):
    monkeypatch.setattr(facade, "analyze", lambda root=None: [_issue()])
    # a fresh project has no .olma/knowledge.json, so nothing is known yet
    assert olma_cli.olma_main(["suggest", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "нет данных" in out


def test_history_prints_runs_and_totals(capsys, project):
    assert olma_cli.olma_main(["history", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Итого:" in out


def test_history_lists_recorded_runs(capsys, project):
    kb = OlmaKnowledgeBase(project)
    kb.record_run([_issue()], [FixResult("mod.py", 2, "OL001", True)], smoke_ok=True)
    kb.save()
    assert olma_cli.olma_main(["history", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "issues=1" in out and "applied=1" in out


# ======================================================= 6. GitHub publish


def test_publish_issue_without_repo_returns_error(capsys, project, monkeypatch):
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "OLMA_GITHUB_REPO"):
        monkeypatch.delenv(var, raising=False)
    assert olma_cli.olma_main(["publish-issue", "--root", str(project)]) == 1
    assert "GitHub:" in capsys.readouterr().out


def test_publish_issue_success_path(capsys, project, monkeypatch):
    class Result:
        ok = True
        action = "created"
        url = "https://example.invalid/1"
        number = 1
        error = ""

        def __str__(self):
            return "created #1"

    class Gh:
        def publish_analysis(self, issues, stats):
            return Result()

    monkeypatch.setattr("mountfile.olma.github.from_env", lambda repo, dry_run: Gh())
    assert olma_cli.olma_main(["publish-issue", "--repo", "o/n"]) == 0
    assert "created #1" in capsys.readouterr().out


def test_publish_pr_requires_apply(capsys, project):
    assert olma_cli.olma_main(["publish-pr", "--root", str(project)]) == 1
    assert "Требуется --apply" in capsys.readouterr().out


def test_publish_pr_without_fixes_is_noop(capsys, project, monkeypatch):
    monkeypatch.setattr(facade, "fix", lambda **kw: [])
    assert olma_cli.olma_main(["publish-pr", "--apply", "--root", str(project)]) == 0
    assert "Нет применённых фиксов" in capsys.readouterr().out


def test_publish_pr_publishes_collected_changes(capsys, project, monkeypatch):
    seen = {}

    class Result:
        ok = True
        action = "pr"
        url = "u"
        number = 7
        error = ""

        def __str__(self):
            return "pr #7"

    class Gh:
        def publish_fixes_pr(self, fixes, changes, branch_prefix="x"):
            seen["changes"] = changes
            seen["prefix"] = branch_prefix
            return Result()

    monkeypatch.setattr(
        facade,
        "fix",
        lambda **kw: [FixResult(file="mod.py", line=2, rule_id="OL001", applied=True)],
    )
    monkeypatch.setattr("mountfile.olma.github.from_env", lambda repo, dry_run: Gh())
    assert (
        olma_cli.olma_main(
            [
                "publish-pr",
                "--apply",
                "--root",
                str(project),
                "--branch-prefix",
                "olma-x",
            ]
        )
        == 0
    )
    assert "pr #7" in capsys.readouterr().out
    assert seen["changes"]["mod.py"].startswith(b"def load")
    assert seen["prefix"] == "olma-x"


def test_publish_pr_without_repo_errors(capsys, project, monkeypatch):
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "OLMA_GITHUB_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        facade,
        "fix",
        lambda **kw: [FixResult(file="mod.py", line=2, rule_id="OL001", applied=True)],
    )
    assert olma_cli.olma_main(["publish-pr", "--apply", "--root", str(project)]) == 1
    assert "GitHub:" in capsys.readouterr().out


# ================================================= 7. runtime trace / watch


def test_install_and_uninstall_commands(capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(facade, "install", lambda **kw: calls.append(("i", kw)))
    monkeypatch.setattr(facade, "uninstall", lambda: calls.append(("u", {})))
    assert olma_cli.olma_main(["install"]) == 0
    assert "installed" in capsys.readouterr().out
    assert olma_cli.olma_main(["uninstall"]) == 0
    assert "uninstalled" in capsys.readouterr().out
    assert calls[0][0] == "i" and calls[1][0] == "u"


def test_trace_command_prints_stats(capsys, monkeypatch):
    monkeypatch.setattr(facade, "install", lambda **kw: None)
    monkeypatch.setattr(facade, "trace_stats", lambda: {"total": 3})
    assert olma_cli.olma_main(["trace"]) == 0
    assert "Stats" in capsys.readouterr().out


def test_watch_command_stops_on_keyboard_interrupt(capsys, monkeypatch):
    monkeypatch.setattr(facade, "install", lambda **kw: None)
    monkeypatch.setattr(facade, "trace_stats", lambda: {"total": 1, "read": 1})

    def boom(_secs):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", boom)
    assert olma_cli.olma_main(["watch"]) == 0
    out = capsys.readouterr().out
    assert "ИТОГО" in out and "events=1" in out


# ============================================================== 8. sync


def test_sync_dry_run_prints_pipeline(capsys, project):
    assert olma_cli.olma_main(["sync", "--root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "=== OLMA sync ===" in out
    assert "analyze:" in out and "fix:" in out and "learn:" in out


def test_sync_with_apply_runs_smoke_and_skips_publish(capsys, project, monkeypatch):
    monkeypatch.setattr(facade, "analyze", lambda root=None: [_issue()])
    monkeypatch.setattr(facade, "fix", lambda **kw: [])
    monkeypatch.setattr(facade, "trace_stats", lambda: {})
    assert (
        olma_cli.olma_main(["sync", "--apply", "--no-publish", "--root", str(project)])
        == 0
    )
    out = capsys.readouterr().out
    assert "smoke:" in out


def test_sync_publishes_when_apply_and_not_no_publish(capsys, project, monkeypatch):
    class Result:
        ok = True
        action = "x"
        url = "u"
        number = 0
        error = ""

        def __str__(self):
            return "published"

    class Gh:
        def publish_analysis(self, issues, stats):
            return Result()

        def publish_fixes_pr(self, fixes, changes, branch_prefix="o"):
            return Result()

    monkeypatch.setattr(facade, "analyze", lambda root=None: [_issue()])
    monkeypatch.setattr(
        facade,
        "fix",
        lambda **kw: [FixResult(file="mod.py", line=2, rule_id="OL001", applied=True)],
    )
    monkeypatch.setattr(facade, "trace_stats", lambda: {})
    monkeypatch.setattr("mountfile.olma.github.from_env", lambda repo, dry_run: Gh())
    assert olma_cli.olma_main(["sync", "--apply", "--repo", "o/n"]) == 0
    out = capsys.readouterr().out
    assert "publish issue: published" in out
    assert "publish PR: published" in out


def test_sync_swallows_publish_errors(capsys, project, monkeypatch):
    monkeypatch.setattr(facade, "analyze", lambda root=None: [_issue()])
    monkeypatch.setattr(facade, "fix", lambda **kw: [])
    monkeypatch.setattr(facade, "trace_stats", lambda: {})

    def boom(repo, dry_run):
        raise RuntimeError("no network")

    monkeypatch.setattr("mountfile.olma.github.from_env", boom)
    assert olma_cli.olma_main(["sync", "--apply"]) == 0
    assert "publish skipped" in capsys.readouterr().out


def test_unknown_command_falls_through_to_error():
    with pytest.raises(SystemExit):
        olma_cli.olma_main(["nonsense"])


# ============================================ 9. analyzer + learner plumbing


def test_analyzer_skips_virtualenvs_and_self_reporting_modules():
    root = Path(__file__).resolve().parents[1]
    files = list(OlmaAnalyzer(root).iter_files())
    assert files, "the project itself must still be analysed"
    assert all(".venv" not in p.parts for p in files), "venv must be skipped"
    assert all("__pycache__" not in p.parts for p in files)
    # The analyzer refuses to audit the modules that implement it, otherwise
    # its own IO patterns become issues. See OlmaAnalyzer.iter_files().
    analysed = {p.name for p in files if "olma" in p.parts}
    for self_module in ("cli.py", "analyzer.py", "fixer.py", "reporter.py", "rules.py"):
        assert self_module not in analysed, "%s must not be self-analysed" % self_module


def test_analyzer_scan_file_is_robust(project):
    analyzer = OlmaAnalyzer(project)
    issues = analyzer.scan_file(project / "mod.py")
    assert isinstance(issues, list)
    broken = project / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    assert analyzer.scan_file(broken) == [], "syntax errors must not raise"


def test_analyzer_summary_shape(project):
    analyzer = OlmaAnalyzer(project)
    issues = analyzer.scan()
    summary = analyzer.summary(issues)
    assert isinstance(summary, dict)


def test_knowledge_base_persists_and_reloads(project):
    kb = OlmaKnowledgeBase(project)
    kb.record_run([_issue()], [FixResult("mod.py", 2, "OL001", True)], smoke_ok=True)
    kb.save()
    reloaded = OlmaKnowledgeBase(project)
    assert reloaded.data["history"], "history must survive a reload"
    assert reloaded.summary()["runs"] >= 1


def test_learner_suggest_and_threshold(project):
    learner = OlmaLearner(project)
    issue = _issue()
    assert isinstance(learner.suggest(issue), list)
    assert learner.should_auto_apply(issue, 0.85) in (True, False)
    assert isinstance(learner.history(5), list)


def test_knowledge_base_recovers_from_corrupt_file(project):
    path = project / OlmaKnowledgeBase(project).path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    kb = OlmaKnowledgeBase(project)
    assert kb.data["version"] == OlmaKnowledgeBase.VERSION
