# MOUNTFILE_FIX — OLMA CLI
"""python main.py --olma <cmd>"""

from __future__ import annotations
import argparse, sys
from pathlib import Path


def olma_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="olma", description="OLMA analysis & fix")
    ap.add_argument("cmd", choices=["analyze", "fix", "report", "trace",
                                    "watch", "rules", "install", "uninstall"],
                    help="команда")
    ap.add_argument("--root", default=None, help="корень проекта")
    ap.add_argument("--format", default="text", choices=["text", "json", "md", "html"])
    ap.add_argument("--out", default=None, help="файл отчёта")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true", help="применить исправления")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-severity", default="INFO",
                    choices=["INFO", "WARN", "ERROR", "CRITICAL"])
    args = ap.parse_args(argv)

    from . import olma
    from .rules import RULES, Severity

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]

    if args.cmd == "rules":
        for rid, r in sorted(RULES.items()):
            print(f"{rid}  [{r.severity.label():8}] {r.title}"
                  f"{'  (fixable)' if r.fixable else ''}")
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

    if args.cmd == "analyze":
        issues = olma.analyze(str(root))
        min_sev = {"INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}[args.min_severity]
        issues = [i for i in issues if i.severity >= min_sev]
        from .reporter import OlmaReporter
        rep = OlmaReporter(issues[:args.limit])
        print(rep.render(fmt=args.format, out=args.out))
        return 0

    if args.cmd == "fix":
        results = olma.fix(dry_run=not args.apply, root=str(root))
        ok = sum(1 for r in results if r.applied)
        print(f"Fix results: applied={ok}, total={len(results)}")
        for r in results[:args.limit]:
            mark = "OK " if r.applied else "SKIP"
            print(f"  [{mark}] {r.file}:{r.line} {r.rule_id} — {r.detail}")
        if not args.apply:
            print("\nЭто dry-run. Запустите с --apply, чтобы применить.")
        return 0

    if args.cmd == "report":
        text = olma.report(fmt=args.format, out=args.out, root=str(root))
        if not args.out:
            print(text)
        else:
            print(f"Report written: {args.out}")
        return 0

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
        print(f"\nИТОГО: events={st.get('total', 0)} "
              f"read={st.get('read', 0)} write={st.get('write', 0)} "
              f"errors={st.get('error', 0)}")
        anomalies = olma._runtime.detect_anomalies() if olma._runtime else []
        for a in anomalies[:20]:
            print(f"  ! {a}")
        return 0

    return 1
