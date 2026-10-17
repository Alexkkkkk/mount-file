# MOUNTFILE_FIX — OLMA fixer
"""Автоисправление кода: безопасные AST-трансформации."""

from __future__ import annotations
import ast, shutil, textwrap
from dataclasses import dataclass
from pathlib import Path

from .analyzer import Issue


@dataclass
class FixResult:
    file: str
    line: int
    rule_id: str
    applied: bool
    detail: str = ""


class OlmaFixer:
    """Применяет безопасные исправления к исходникам."""

    def __init__(self, root: str | Path, *, dry_run: bool = True) -> None:
        self.root = Path(root).resolve()
        self.dry_run = dry_run

    # --- публичное API ---
    def apply(self, issues: list[Issue]) -> list[FixResult]:
        by_file: dict[str, list[Issue]] = {}
        for i in issues:
            if i.fixable:
                by_file.setdefault(i.file, []).append(i)

        results: list[FixResult] = []
        for rel, file_issues in by_file.items():
            p = self.root / rel
            if not p.exists():
                continue
            results.extend(self._fix_file(p, rel, file_issues))
        return results

    # --- внутренняя логика ---
    def _fix_file(self, p: Path, rel: str, issues: list[Issue]) -> list[FixResult]:
        src = p.read_text(encoding="utf-8", errors="replace")
        lines = src.splitlines(keepends=True)
        results: list[FixResult] = []

        # сортируем по убыванию строки, чтобы смещения не сбивались
        for issue in sorted(issues, key=lambda i: -i.line):
            handler = getattr(self, f"_fix_{issue.rule_id.lower()}", None)
            if handler is None:
                results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                         "нет обработчика"))
                continue
            try:
                new_lines, detail = handler(lines, issue)
                if new_lines is not None and new_lines != lines:
                    lines = new_lines
                    results.append(FixResult(rel, issue.line, issue.rule_id, True, detail))
                else:
                    results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                             detail or "без изменений"))
            except Exception as e:
                results.append(FixResult(rel, issue.line, issue.rule_id, False,
                                         f"ошибка: {e}"))

        new_src = "".join(lines)
        if new_src != src and not self.dry_run:
            bak = Path(str(p) + ".olma.bak")
            if not bak.exists():
                shutil.copy2(p, bak)
            p.write_text(new_src, encoding="utf-8")

        return results

    # --- конкретные fix-хендлеры ---
    def _fix_ol001(self, lines, issue):
        """open() без with → with open() as _f: ... (оборачиваем одну строку)."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        # Простейший случай: var = open(...)
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        if "=" in stripped and "open(" in stripped:
            var, expr = stripped.split("=", 1)
            var = var.strip()
            new = f"{indent}with {expr.strip()} as {var}:\n"
            # добавляем TODO — надо перенести тело внутрь with
            new += f"{indent}    pass  # OLMA: перенесите тело сюда\n"
            new += f"{indent}# OLMA-TODO: закрыть with-блок для {var}\n"
            lines[idx] = new
            return lines, "обёрнуто в with (нужно вручную перенести тело)"
        return None, "не поддержано"

    def _fix_ol002(self, lines, issue):
        """read() → read(size=1<<20)."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        if ".read()" in line:
            lines[idx] = line.replace(".read()", ".read(1 << 20)", 1)
            return lines, "добавлен size=1MiB"
        return None, ""

    def _fix_ol005(self, lines, issue):
        return self._fix_ol001(lines, issue)

    def _fix_ol007(self, lines, issue):
        """Оборачивает строку в try/except OSError."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        indent = line[:len(line) - len(line.lstrip())]
        body = line[len(indent):]
        new = (f"{indent}try:\n"
               f"{indent}    {body}"
               f"{indent}except OSError as _e:\n"
               f"{indent}    raise  # OLMA: обработайте ошибку\n")
        lines[idx] = new
        return lines, "добавлен try/except OSError"

    def _fix_ol010(self, lines, issue):
        """Рекомендация — ручная замена на OlmaBuffer."""
        return None, "требует ручной замены на OlmaBuffer/OlmaView"

    def _fix_ol011(self, lines, issue):
        """Подсказка — ручная замена на OlmaChain."""
        return None, "требует ручной замены на OlmaChain"

    def _fix_ol012(self, lines, issue):
        return None, "требует ручной замены на OlmaXorDecoder"

    def _fix_ol013(self, lines, issue):
        return None, "требует ручной замены на OlmaTransaction"

    def _fix_ol019(self, lines, issue):
        """close() → try/finally."""
        idx = issue.line - 1
        if idx < 0 or idx >= len(lines):
            return None, ""
        line = lines[idx]
        indent = line[:len(line) - len(line.lstrip())]
        body = line[len(indent):]
        new = (f"{indent}try:\n"
               f"{indent}    {body}"
               f"{indent}finally:\n"
               f"{indent}    pass  # OLMA: убедитесь, что ресурс закрыт\n")
        lines[idx] = new
        return lines, "добавлен try/finally"

    def _fix_ol020(self, lines, issue):
        """open(..., 'w') → OlmaTransaction (рекомендация)."""
        return None, "требует ручной замены на OlmaTransaction"
