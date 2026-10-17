# MOUNTFILE_FIX — OLMA analyzer
"""Статический AST-анализ проекта на 20 правил."""

from __future__ import annotations
import ast, os, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .rules import RULES, Rule, Severity


@dataclass
class Issue:
    rule_id: str
    title: str
    severity: int
    file: str
    line: int
    col: int
    snippet: str
    hint: str
    fixable: bool

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id, "title": self.title,
            "severity": self.severity,
            "severity_label": Severity(self.severity).label(),
            "file": self.file, "line": self.line, "col": self.col,
            "snippet": self.snippet, "hint": self.hint, "fixable": self.fixable,
        }


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, src_lines: list[str]) -> None:
        self.path = path
        self.lines = src_lines
        self.issues: list[Issue] = []

    def _emit(self, rule_id: str, node: ast.AST, snippet: str = "") -> None:
        r = RULES[rule_id]
        ln = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0)
        snip = snippet or (self.lines[ln - 1].strip() if 0 < ln <= len(self.lines) else "")
        self.issues.append(Issue(
            rule_id=r.id, title=r.title, severity=int(r.severity),
            file=self.path, line=ln, col=col,
            snippet=snip[:160], hint=r.hint, fixable=r.fixable,
        ))

    # --- visitors ---
    def visit_Call(self, node: ast.Call) -> None:
        name = self._call_name(node)

        # OL001: open без with
        if name == "open":
            parent = getattr(node, "_olma_parent", None)
            if not isinstance(parent, (ast.With, ast.AsyncWith)):
                self._emit("OL001", node)

        # OL002 / OL009: .read() без размера
        if name.endswith(".read"):
            if not node.args and not node.keywords:
                self._emit("OL002", node)
                # .read() без size в выражении, присвоенном переменной → read_all
                self._emit("OL009", node)

        # OL003: seek() без проверки
        if name.endswith(".seek"):
            self._emit("OL003", node)

        # OL013 / OL020: open(..., 'w') без backup
        if name == "open":
            mode = self._mode_arg(node)
            if mode and "w" in mode and "x" not in mode:
                self._emit("OL020", node)
            if "r+" in (mode or "") or "w" in (mode or ""):
                self._emit("OL013", node)

        # OL014: int.from_bytes без slice
        if name == "int.from_bytes":
            src = node.args[0] if node.args else None
            if isinstance(src, ast.Subscript) and isinstance(src.slice, ast.Constant):
                pass
            elif isinstance(src, ast.Name):
                # не можем проверить длину — предупреждаем
                self._emit("OL014", node)

        # OL015: os.replace
        if name == "os.replace":
            self._emit("OL015", node)

        # OL017: print(...)
        if name == "print":
            self._emit("OL017", node)

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # OL016: silent except
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self._emit("OL016", node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        # OL004: while True без break внутри
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(n, ast.Break) for n in ast.walk(node)):
                self._emit("OL004", node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # OL006 / OL018: захардкоженные пути и магические offset
        if isinstance(node.value, str):
            s = node.value
            if re.match(r"^[A-Za-z]:[\\/]|^/[/\\w]", s) and len(s) < 120:
                # исключаем строки, содержащие пробелы — вероятно текст
                if " " not in s:
                    self._emit("OL006", node, snippet=repr(s))
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            if 0x1000 <= node.value <= 0xFFFFFF and node.value not in (1000, 10000, 100000):
                # вероятный offset
                self._emit("OL018", node, snippet=str(node.value))
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        # OL011: ручной обход FAT-цепочки
        src = ast.unparse(node.iter) if hasattr(ast, "unparse") else ""
        if "fat" in src.lower() and "while" not in src.lower():
            self._emit("OL011", node)
        self.generic_visit(node)

    # --- helpers ---
    def _call_name(self, node: ast.Call) -> str:
        f = node.func
        if isinstance(f, ast.Name): return f.id
        if isinstance(f, ast.Attribute):
            parts, cur = [], f
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr); cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            return ".".join(reversed(parts))
        return ""

    def _mode_arg(self, node: ast.Call):
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            return node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return kw.value.value
        return None


def _mark_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._olma_parent = node  # type: ignore[attr-defined]


class OlmaAnalyzer:
    """Сканирует проект и возвращает список Issue."""

    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                 "dist", "build", ".pytest_cache"}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def iter_files(self) -> Iterator[Path]:
        for p in self.root.rglob("*.py"):
            if any(s in p.parts for s in self.SKIP_DIRS):
                continue
            if p.name == "mountfile_fix.py":
                continue
            if "olma" in p.parts and p.name in {
                "core.py", "runtime.py", "rules.py", "analyzer.py",
                "fixer.py", "reporter.py", "cli.py", "__init__.py"
            }:
                continue  # не анализируем сам OLMA
            yield p

    def scan_file(self, path: Path) -> list[Issue]:
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(path))
        except (SyntaxError, OSError):
            return []
        _mark_parents(tree)
        v = _Visitor(str(path.relative_to(self.root)), src.splitlines())
        v.visit(tree)
        return v.issues

    def scan(self) -> list[Issue]:
        all_issues: list[Issue] = []
        for p in self.iter_files():
            all_issues.extend(self.scan_file(p))
        all_issues.sort(key=lambda i: (-i.severity, i.file, i.line))
        return all_issues

    def summary(self, issues: list[Issue]) -> dict:
        by_sev = {1: 0, 2: 0, 3: 0, 4: 0}
        by_rule: dict[str, int] = {}
        by_file: dict[str, int] = {}
        for i in issues:
            by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
            by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
            by_file[i.file] = by_file.get(i.file, 0) + 1
        return {
            "total": len(issues),
            "by_severity": {Severity(k).label(): v for k, v in by_sev.items()},
            "by_rule": by_rule,
            "by_file": by_file,
            "fixable": sum(1 for i in issues if i.fixable),
        }
