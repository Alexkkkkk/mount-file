# MOUNTFILE_FIX — OLMA reporter
"""Отчёты: json / markdown / html / text."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .analyzer import Issue
from .rules import Severity


class OlmaReporter:
    def __init__(self, issues: list[Issue], trace_stats: dict | None = None) -> None:
        self.issues = issues
        self.trace = trace_stats or {}
        self.ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def render(self, *, fmt: str = "text", out: str | None = None) -> str:
        fmt = {"md": "markdown", "htm": "html"}.get(fmt, fmt)
        fn = getattr(self, f"_render_{fmt}", None)
        if fn is None:
            raise ValueError(f"unsupported format: {fmt}")
        text = fn()
        if out:
            Path(out).write_text(text, encoding="utf-8")
        return text

    # --- formatters ---
    def _summary(self) -> dict:
        by_sev = {"INFO": 0, "WARN": 0, "ERROR": 0, "CRITICAL": 0}
        by_rule: dict[str, int] = {}
        for i in self.issues:
            by_sev[Severity(i.severity).label()] += 1
            by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
        return {"total": len(self.issues), "by_severity": by_sev, "by_rule": by_rule}

    def _render_text(self) -> str:
        s = self._summary()
        lines = [
            f"OLMA report — {self.ts}",
            f"Всего проблем: {s['total']}",
            "  " + " ".join(f"{k}={v}" for k, v in s["by_severity"].items()),
            "",
            "По правилам:",
        ]
        for r, n in sorted(s["by_rule"].items(), key=lambda x: -x[1]):
            lines.append(f"  {r}: {n}")
        lines.append("")
        for i in self.issues[:100]:
            tag = Severity(i.severity).label()
            lines.append(f"[{tag}] {i.rule_id} {i.file}:{i.line} — {i.title}")
            if i.snippet:
                lines.append(f"      > {i.snippet}")
            lines.append(f"      fix: {i.hint}")
        if len(self.issues) > 100:
            lines.append(f"... и ещё {len(self.issues) - 100}")
        if self.trace:
            lines.append("")
            lines.append("Runtime trace:")
            lines.append(
                f"  events={self.trace.get('total', 0)} "
                f"read={self.trace.get('read', 0)} "
                f"write={self.trace.get('write', 0)} "
                f"errors={self.trace.get('error', 0)}"
            )
        return "\n".join(lines)

    def _render_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "summary": self._summary(),
                "issues": [i.to_dict() for i in self.issues],
                "trace": self.trace,
            },
            indent=2,
            ensure_ascii=False,
        )

    def _render_markdown(self) -> str:
        s = self._summary()
        out = [f"# OLMA report ({self.ts})", "", f"**Всего:** {s['total']}  "]
        for k, v in s["by_severity"].items():
            out.append(f"**{k}:** {v}  ")
        out += ["", "## По правилам", ""]
        for r, n in sorted(s["by_rule"].items(), key=lambda x: -x[1]):
            out.append(f"- `{r}` — {n}")
        out += [
            "",
            "## Проблемы",
            "",
            "| Sev | Rule | File | Line | Title |",
            "|-----|------|------|------|-------|",
        ]
        for i in self.issues[:200]:
            out.append(
                f"| {Severity(i.severity).label()} | `{i.rule_id}` | "
                f"`{i.file}` | {i.line} | {i.title} |"
            )
        return "\n".join(out)

    def _render_html(self) -> str:
        s = self._summary()
        rows = []
        for i in self.issues[:500]:
            sev = Severity(i.severity).label()
            cls = {"INFO": "i", "WARN": "w", "ERROR": "e", "CRITICAL": "c"}[sev]
            rows.append(
                f"<tr class='{cls}'><td>{sev}</td><td>{i.rule_id}</td>"
                f"<td>{i.file}</td><td>{i.line}</td>"
                f"<td>{_html(i.title)}</td><td>{_html(i.snippet)}</td>"
                f"<td>{_html(i.hint)}</td></tr>"
            )
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>OLMA Report</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f7f8fa}}
h1{{color:#222}} .sum{{display:flex;gap:1rem;margin:1rem 0}}
.badge{{padding:.4rem .8rem;border-radius:6px;color:#fff}}
.badge.INFO{{background:#3498db}} .badge.WARN{{background:#f39c12}}
.badge.ERROR{{background:#e74c3c}} .badge.CRITICAL{{background:#8e44ad}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
th,td{{padding:.5rem .7rem;text-align:left;border-bottom:1px solid #eee;font-size:.9rem}}
th{{background:#2c3e50;color:#fff}} tr.c{{background:#f4e6ff}}
tr.e{{background:#ffe6e6}} tr.w{{background:#fff8e1}} tr.i{{background:#eef6ff}}
code{{background:#f0f0f0;padding:.1rem .3rem;border-radius:3px}}
</style></head><body>
<h1>OLMA Report — {self.ts}</h1>
<div class="sum">
<div class="badge INFO">INFO {s['by_severity']['INFO']}</div>
<div class="badge WARN">WARN {s['by_severity']['WARN']}</div>
<div class="badge ERROR">ERROR {s['by_severity']['ERROR']}</div>
<div class="badge CRITICAL">CRITICAL {s['by_severity']['CRITICAL']}</div>
</div>
<h2>Всего проблем: {s['total']}</h2>
<table><thead><tr>
<th>Sev</th><th>Rule</th><th>File</th><th>Line</th>
<th>Title</th><th>Snippet</th><th>Hint</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
