# MOUNTFILE_FIX — OLMA fixer
"""Автоисправление кода: безопасные AST-трансформации."""

from __future__ import annotations

import ast
import shutil
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
                results.append(
                    FixResult(rel, issue.line, issue.rule_id, False, "нет обработчика")
                )
                continue
            try:
                before = "".join(lines)
                new_lines, detail = handler(lines, issue)
                after = "".join(lines) if new_lines is None else "".join(new_lines)
                if new_lines is not None and after != before:
                    lines = new_lines
                    results.append(
                        FixResult(rel, issue.line, issue.rule_id, True, detail)
                    )
                else:
                    results.append(
                        FixResult(
                            rel,
                            issue.line,
                            issue.rule_id,
                            False,
                            detail or "без изменений",
                        )
                    )
            except Exception as e:
                results.append(
                    FixResult(rel, issue.line, issue.rule_id, False, f"ошибка: {e}")
                )

        new_src = "".join(lines)
        if new_src != src:
            # валидация: файл с синтаксической ошибкой не записываем
            try:
                ast.parse(new_src, filename=str(p))
            except SyntaxError as e:
                for k, r in enumerate(results):
                    if r.applied and r.file == rel:
                        results[k] = FixResult(
                            rel,
                            r.line,
                            r.rule_id,
                            False,
                            f"{r.detail} (откат: синтаксис: {e.msg})",
                        )
                new_src = src
        if new_src != src and not self.dry_run:
            bak = Path(str(p) + ".olma.bak")
            if not bak.exists():
                shutil.copy2(p, bak)
            p.write_text(new_src, encoding="utf-8")

        return results

    # --- конкретные fix-хендлеры ---
    def _fix_ol001(self, lines, issue):
        """open() без with — только рекомендация.

        Авто-обёртка одной строки оставляет остальное тело с закрытым
        файлом, а для строк `with open(...) as f:` даёт синтаксический
        мусор из-за split("=").
        """
        return None, (
            "требует ручной правки: перенос тела в with "
            "(авто-обёртка одной строки ломает тело)"
        )

    def _fix_ol002(self, lines, issue):
        """read() без размера — только рекомендация.

        Слепая замена на read(1 << 20) усекает чтение файлов больше
        1 МиБ — при работе с образами это потеря данных.
        """
        return None, (
            "требует ручной оценки: read(1 << 20) усекает чтение " "файлов больше 1 МиБ"
        )

    def _fix_ol005(self, lines, issue):
        return self._fix_ol001(lines, issue)

    def _fix_ol007(self, lines, issue):
        """try/except OSError — только рекомендация.

        Обёртка `except OSError: raise` ничего не исправляет и лишь
        засоряет код — обработка ошибки требует ручного решения.
        """
        return None, "требует ручной обработки: осмысленный except OSError"

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
        """close() → try/finally — только рекомендация.

        Обёртка `finally: pass` не закрывает ресурс и не меняет
        поведение — нужна ручная гарантия закрытия.
        """
        return None, "требует ручной обработки: гарантированное закрытие ресурса"

    def _fix_ol020(self, lines, issue):
        """open(..., 'w') → OlmaTransaction (рекомендация)."""
        return None, "требует ручной замены на OlmaTransaction"
