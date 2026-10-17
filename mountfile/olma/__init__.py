# MOUNTFILE_FIX — OLMA package
"""OLMA — Object-Level Memory Analysis & Access.

Единая точка входа:
    from mountfile.olma import olma
    olma.install()                # включить трассировку
    olma.analyze()                # статический анализ проекта
    olma.fix(dry_run=True)        # автоисправление
    olma.report(fmt="html")       # отчёт
    olma.trace_stats()            # статистика runtime-трассировки
"""

from __future__ import annotations

from .analyzer import Issue, OlmaAnalyzer
from .cli import olma_main
from .core import (FAT32_BAD, FAT32_EOC_MIN, FAT32_MAX_CLUSTER, SECTOR,
                   OlmaBoundsError, OlmaBuffer, OlmaChain, OlmaCluster,
                   OlmaError, OlmaHandle, OlmaIntegrityError, OlmaPartition,
                   OlmaPartitionTable, OlmaStateError, OlmaTransaction,
                   OlmaView, OlmaXorDecoder, safe_download, verify_sha256)
from .fixer import FixResult, OlmaFixer
from .reporter import OlmaReporter
from .rules import RULES, Rule, Severity
from .runtime import OlmaRuntime, RuntimeEvent


class Olma:
    """Фасад для всех операций OLMA."""

    def __init__(self) -> None:
        self._runtime: OlmaRuntime | None = None
        self._analyzer: OlmaAnalyzer | None = None

    # --- runtime ---
    def install(self, *, trace_files: bool = True, trace_memory: bool = False) -> None:
        if self._runtime is not None:
            return
        self._runtime = OlmaRuntime(
            trace_files=trace_files,
            trace_memory=trace_memory,
        )
        self._runtime.install()

    def uninstall(self) -> None:
        if self._runtime:
            self._runtime.uninstall()
            self._runtime = None

    def trace_stats(self) -> dict:
        return self._runtime.stats() if self._runtime else {}

    def trace_events(self, limit: int = 100) -> list:
        return self._runtime.events(limit) if self._runtime else []

    # --- analysis ---
    def analyzer(self, root: str | None = None) -> OlmaAnalyzer:
        if self._analyzer is None:
            from pathlib import Path

            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._analyzer = OlmaAnalyzer(base)
        return self._analyzer

    def analyze(self, root: str | None = None) -> list:
        return self.analyzer(root).scan()

    # --- fix ---
    def fix(self, *, dry_run: bool = True, root: str | None = None) -> list:
        from .fixer import OlmaFixer

        issues = self.analyze(root)
        fixer = OlmaFixer(self.analyzer(root).root, dry_run=dry_run)
        return fixer.apply(issues)

    # --- report ---
    def report(
        self, *, fmt: str = "html", out: str | None = None, root: str | None = None
    ) -> str:
        from .reporter import OlmaReporter

        issues = self.analyze(root)
        rep = OlmaReporter(issues, self.trace_stats())
        return rep.render(fmt=fmt, out=out)


olma = Olma()

__all__ = [
    "olma",
    "Olma",
    "OlmaError",
    "OlmaBoundsError",
    "OlmaIntegrityError",
    "OlmaStateError",
    "OlmaHandle",
    "OlmaView",
    "OlmaBuffer",
    "OlmaCluster",
    "OlmaChain",
    "OlmaPartition",
    "OlmaPartitionTable",
    "OlmaTransaction",
    "OlmaXorDecoder",
    "OlmaRuntime",
    "OlmaAnalyzer",
    "OlmaFixer",
    "OlmaReporter",
    "Issue",
    "Rule",
    "Severity",
    "RULES",
    "FixResult",
    "safe_download",
    "verify_sha256",
    "olma_main",
    "SECTOR",
    "FAT32_MAX_CLUSTER",
    "FAT32_EOC_MIN",
    "FAT32_BAD",
]
