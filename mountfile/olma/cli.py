# MOUNTFILE_FIX — OLMA CLI
"""OLMA CLI: analyze/fix/trace + learn/suggest/publish/sync.

Запуск:  python main.py --olma <cmd> [опции]
"""

from __future__ import annotations

import argparse
from pathlib import Path


def olma_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olma", description="OLMA full CLI")
    ap.add_argument(
        "cmd",
        choices=[
            "analyze",
            "fix",
            "report",
            "trace",
            "watch",
            "rules",
            "install",
            "uninstall",
            "learn",
            "train",
            "suggest",
            "history",
            "publish-issue",
            "publish-pr",
            "sync",
        ],
        help="команда",
    )
    ap.add_argument("--root", default=None, help="корень проекта")
    ap.add_argument("--format", default="text", choices=["text", "json", "md", "html"])
    ap.add_argument("--out", default=None, help="файл отчёта")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true", help="применить исправления")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument(
        "--min-severity", default="INFO", choices=["INFO", "WARN", "ERROR", "CRITICAL"]
    )
    ap.add_argument("--rule", default=None, help="правило для suggest")
    ap.add_argument("--repo", default=None, help="owner/name GitHub")
    ap.add_argument("--branch-prefix", default="olma-fix")
    ap.add_argument(
        "--threshold", type=float, default=0.85, help="порог авто-применения"
    )
    ap.add_argument("--no-publish", action="store_true", help="не публиковать в GitHub")
    args = ap.parse_args(argv)

    from . import olma

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]

    # ---------- simple commands ----------
    if args.cmd == "rules":
        from .rules import RULES

        for rid, r in sorted(RULES.items()):
            print(
                f"{rid}  [{r.severity.label():8}] {r.title}"
                f"{'  (fixable)' if r.fixable else ''}"
            )
            print(f"       {r.hint}")
        return 0

    if args.cmd == "install":
        olma.install(trace_files=True, trace_memory=True)
        print("OLMA runtime installed")
        return 0

    if args.cmd == "uninstall":
        olma.uninstall()
        print("OLMA runtime uninstalled")
        return 0

    # ---------- analyze / report ----------
    if args.cmd in ("analyze", "report"):
        from .reporter import OlmaReporter

        issues = olma.analyze(str(root))
        min_sev = {"INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}[args.min_severity]
        issues = [i for i in issues if i.severity >= min_sev]
        rep = OlmaReporter(issues[: args.limit])
        text = rep.render(fmt=args.format, out=args.out)
        if not args.out:
            print(text)
        else:
            print(f"Report: {args.out}")
        return 0

    # ---------- fix ----------
    if args.cmd == "fix":
        results = olma.fix(dry_run=not args.apply, root=str(root))
        ok = sum(1 for r in results if r.applied)
        print(f"Fix results: applied={ok}, total={len(results)}")
        for r in results[: args.limit]:
            mark = "OK " if r.applied else "SKIP"
            print(f"  [{mark}] {r.file}:{r.line} {r.rule_id} — {r.detail}")
        if not args.apply:
            print("\nЭто dry-run. Запустите с --apply, чтобы применить.")
        return 0

    # ---------- learn ----------
    if args.cmd in ("learn", "train"):
        from .learn import OlmaLearner

        learner = OlmaLearner(root)
        # полный цикл: analyze → fix(dry) → smoke → learn
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
            import sys as _sys

            r = subprocess.run(
                [_sys.executable, "main.py", "--diag"],
                cwd=str(root),
                capture_output=True,
                timeout=60,
            )
            smoke_ok = r.returncode == 0
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
        from .learn import OlmaLearner

        learner = OlmaLearner(root)
        issues = olma.analyze(str(root))
        if args.rule:
            issues = [i for i in issues if i.rule_id == args.rule]
        for i in issues[: args.limit]:
            sug = learner.suggest(i)
            auto = learner.should_auto_apply(i, args.threshold)
            print(f"{i.rule_id} {i.file}:{i.line} — {i.title}")
            if not sug:
                print("   (нет данных — будет изучено после первого прогона)")
            for s in sug:
                print(
                    f"   {s['strategy']} conf={s['confidence_ctx']} "
                    f"({s['successes']}/{s['attempts']})"
                )
            print(f"   auto_apply={auto}")
        return 0

    if args.cmd == "history":
        from .learn import OlmaLearner

        learner = OlmaLearner(root)
        for run in learner.history(args.limit):
            import datetime as dt

            ts = dt.datetime.fromtimestamp(run["ts"]).strftime("%Y-%m-%d %H:%M")
            print(
                f"{ts}  issues={run['issues']} applied={run['applied']} "
                f"smoke_ok={run['smoke_ok']}"
            )
        s = learner.summary()
        print(
            f"\nИтого: runs={s['runs']} rules={s['rules_learned']} "
            f"contexts={s['contexts_learned']} rate={s['overall_rate']}"
        )
        return 0

    # ---------- publish ----------
    if args.cmd == "publish-issue":
        from .github import OlmaGitHubError, from_env

        issues = olma.analyze(str(root))
        stats = olma.trace_stats()
        try:
            gh = from_env(args.repo, dry_run=args.dry_run)
        except OlmaGitHubError as e:
            print(f"GitHub: {e}")
            return 1
        r = gh.publish_analysis(issues, stats)
        print(r)
        return 0 if r.ok else 1

    if args.cmd == "publish-pr":
        from .github import OlmaGitHubError, from_env

        if not args.apply:
            print("Требуется --apply (иначе нечего публиковать).")
            return 1
        fixes = olma.fix(dry_run=False, root=str(root))
        applied = [r for r in fixes if r.applied]
        if not applied:
            print("Нет применённых фиксов.")
            return 0
        # собираем изменённые файлы
        changes: dict[str, bytes] = {}
        for r in applied:
            p = root / r.file
            if p.exists() and r.file not in changes:
                changes[r.file] = p.read_bytes()
        try:
            gh = from_env(args.repo, dry_run=args.dry_run)
        except OlmaGitHubError as e:
            print(f"GitHub: {e}")
            return 1
        res = gh.publish_fixes_pr(applied, changes, branch_prefix=args.branch_prefix)
        print(res)
        return 0 if res.ok else 1

    # ---------- trace / watch ----------
    if args.cmd == "trace":
        olma.install(trace_files=True, trace_memory=True)
        st = olma.trace_stats()
        print(f"OLMA trace installed. Stats: {st}")
        return 0

    if args.cmd == "watch":
        olma.install(trace_files=True, trace_memory=True)
        print("OLMA watching. Нажмите Ctrl+C для отчёта.")
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        st = olma.trace_stats()
        print(
            f"\nИТОГО: events={st.get('total', 0)} "
            f"read={st.get('read', 0)} write={st.get('write', 0)} "
            f"errors={st.get('error', 0)}"
        )
        anomalies = olma._runtime.detect_anomalies() if olma._runtime else []
        for a in anomalies[:20]:
            print(f"  ! {a}")
        return 0

    # ---------- sync: полный цикл ----------
    if args.cmd == "sync":
        from .learn import OlmaLearner

        learner = OlmaLearner(root)
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
                import sys as _sys

                r = subprocess.run(
                    [_sys.executable, "main.py", "--diag"],
                    cwd=str(root),
                    capture_output=True,
                    timeout=60,
                )
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
                from .github import from_env

                gh = from_env(args.repo, dry_run=args.dry_run)
                r1 = gh.publish_analysis(issues, olma.trace_stats())
                print(f"publish issue: {r1}")
                if applied:
                    changes = {}
                    for r in applied:
                        p = root / r.file
                        if p.exists() and r.file not in changes:
                            changes[r.file] = p.read_bytes()
                    r2 = gh.publish_fixes_pr(
                        applied, changes, branch_prefix=args.branch_prefix
                    )
                    print(f"publish PR: {r2}")
            except Exception as e:
                print(f"publish skipped: {e}")
        return 0

    return 1
