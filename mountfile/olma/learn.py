# MOUNTFILE_FIX — OLMA self-learning
"""OLMA self-learning: статистика успешных фиксов по контекстам.

Знание хранится в .olma/knowledge.json:
  - сколько раз правило встречалось
  - какие стратегии применялись
  - сколько раз фикс привёл к успешному smoke-тесту
  - токены контекста вокруг места проблемы

Далее OLMA ранжирует стратегии по confidence и предлагает лучшую.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

from .analyzer import Issue

DEFAULT_DB = ".olma/knowledge.json"
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\.|\(|\)|\[|\]|=|,")


def _tokenize(line: str) -> list[str]:
    return TOKEN_RE.findall(line or "")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


class OlmaKnowledgeBase:
    """Персистентное хранилище знаний (.olma/knowledge.json)."""

    VERSION = 1

    def __init__(self, root: Path, path: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / (path or DEFAULT_DB)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "version": self.VERSION,
            "created": time.time(),
            "updated": time.time(),
            "issues": {},  # rule_id -> {attempts, successes, strategies}
            "contexts": {},  # ctx_hash -> {rule_id, tokens, attempts, successes, ...}
            "history": [],  # список прогонов
            "candidates": [],  # паттерны без правила
        }

    def save(self) -> None:
        self.data["updated"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # --- mutations ---
    def record_run(
        self, issues: list[Issue], fix_results: list, smoke_ok: bool
    ) -> None:
        run = {
            "ts": time.time(),
            "issues": len(issues),
            "applied": sum(1 for r in fix_results if r.applied),
            "smoke_ok": bool(smoke_ok),
            "details": [],
        }
        for r in fix_results:
            if not r.applied:
                continue
            ctx = self._context_for(r.file, r.line)
            ctx_h = _hash(f"{r.rule_id}|{ctx['tokens']}")
            rule = self.data["issues"].setdefault(
                r.rule_id, {"attempts": 0, "successes": 0, "strategies": {}}
            )
            rule["attempts"] += 1
            if smoke_ok:
                rule["successes"] += 1
            strategy = f"fix_{r.rule_id.lower()}"
            st = rule["strategies"].setdefault(
                strategy, {"attempts": 0, "successes": 0}
            )
            st["attempts"] += 1
            if smoke_ok:
                st["successes"] += 1

            c = self.data["contexts"].setdefault(
                ctx_h,
                {
                    "rule_id": r.rule_id,
                    "tokens": ctx["tokens"],
                    "file_base": ctx["file_base"],
                    "attempts": 0,
                    "successes": 0,
                    "best_strategy": strategy,
                },
            )
            c["attempts"] += 1
            if smoke_ok:
                c["successes"] += 1
            run["details"].append(
                {
                    "rule_id": r.rule_id,
                    "ctx": ctx_h,
                    "ok": smoke_ok,
                    "file": r.file,
                    "line": r.line,
                }
            )
        self.data["history"].append(run)
        # храним не более 500 прогонов
        if len(self.data["history"]) > 500:
            self.data["history"] = self.data["history"][-500:]

    # --- queries ---
    def rate_for_rule(self, rule_id: str) -> float:
        r = self.data["issues"].get(rule_id)
        if not r or r["attempts"] == 0:
            return 0.0
        return r["successes"] / r["attempts"]

    def rate_for_context(self, issue: Issue) -> float:
        ctx = self._context_for(issue.file, issue.line)
        h = _hash(f"{issue.rule_id}|{ctx['tokens']}")
        c = self.data["contexts"].get(h)
        if not c or c["attempts"] == 0:
            return 0.0
        return c["successes"] / c["attempts"]

    def suggest(self, issue: Issue, top_k: int = 3) -> list[dict]:
        """Возвращает список стратегий, ранжированных по confidence."""
        rule = self.data["issues"].get(issue.rule_id, {})
        strategies = rule.get("strategies", {})
        result = []
        for name, s in strategies.items():
            att = s.get("attempts", 0)
            suc = s.get("successes", 0)
            if att == 0:
                continue
            # байесовская сглаженная оценка
            conf = (suc + 1) / (att + 2)
            result.append(
                {
                    "strategy": name,
                    "confidence": round(conf, 3),
                    "attempts": att,
                    "successes": suc,
                }
            )
        # добавляем контекстную оценку как бонус
        ctx_conf = self.rate_for_context(issue)
        for r in result:
            r["confidence_ctx"] = round((r["confidence"] + ctx_conf) / 2, 3)
        result.sort(key=lambda x: -x["confidence_ctx"])
        return result[:top_k]

    def should_auto_apply(self, issue: Issue, threshold: float = 0.85) -> bool:
        for s in self.suggest(issue, top_k=1):
            if s["confidence_ctx"] >= threshold and s["attempts"] >= 3:
                return True
        return False

    # --- pattern discovery ---
    def discover_patterns(self, files: Iterable[Path]) -> list[dict]:
        """Ищет повторяющиеся токен-паттерны вокруг IO-вызовов."""
        counter: Counter[str] = Counter()
        examples: dict[str, list[str]] = defaultdict(list)
        IO_TOKENS = ("open", "read", "write", "seek", "close", "flush", "truncate")
        for p in files:
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                toks = _tokenize(line)
                if not any(t in IO_TOKENS for t in toks):
                    continue
                for w in (3, 4, 5):
                    for j in range(len(toks) - w + 1):
                        key = " ".join(toks[j : j + w])
                        counter[key] += 1
                        if len(examples[key]) < 3:
                            examples[key].append(f"{p.name}:{i + 1}")
        # фильтруем по частоте
        cand = []
        for key, cnt in counter.most_common(200):
            if cnt < 3:
                continue
            cand.append(
                {
                    "pattern": key,
                    "count": cnt,
                    "examples": examples[key],
                    "covered": False,
                }
            )
        self.data["candidates"] = cand[:100]
        return cand

    # --- helpers ---
    def _context_for(self, file: str, line: int) -> dict:
        p = self.root / file
        tokens: list[str] = []
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for i in range(max(0, line - 2), min(len(lines), line + 1)):
                tokens.extend(_tokenize(lines[i]))
        except OSError:
            pass
        return {"file_base": Path(file).name, "tokens": tokens[:40]}

    # --- reports ---
    def summary(self) -> dict:
        rules = self.data["issues"]
        total_att = sum(r["attempts"] for r in rules.values())
        total_suc = sum(r["successes"] for r in rules.values())
        top = sorted(
            [
                {
                    "rule_id": k,
                    "attempts": v["attempts"],
                    "successes": v["successes"],
                    "rate": round(v["successes"] / max(1, v["attempts"]), 3),
                }
                for k, v in rules.items()
            ],
            key=lambda x: -x["rate"],
        )
        return {
            "rules_learned": len(rules),
            "contexts_learned": len(self.data["contexts"]),
            "runs": len(self.data["history"]),
            "total_attempts": total_att,
            "total_successes": total_suc,
            "overall_rate": round(total_suc / max(1, total_att), 3),
            "top_rules": top[:10],
            "candidates": len(self.data["candidates"]),
        }


class OlmaLearner:
    """Высокоуровневый фасад для обучения."""

    def __init__(self, root: Path, *, db_path: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.kb = OlmaKnowledgeBase(self.root, db_path)

    def learn(self, issues, fix_results, *, smoke_ok: bool) -> None:
        self.kb.record_run(issues, fix_results, smoke_ok)
        self.kb.save()

    def discover(self) -> list[dict]:
        files = [
            p
            for p in self.root.rglob("*.py")
            if not any(
                s in p.parts for s in (".git", "__pycache__", ".venv", "venv", "olma")
            )
        ]
        cands = self.kb.discover_patterns(files)
        self.kb.save()
        return cands

    def suggest(self, issue: Issue) -> list[dict]:
        return self.kb.suggest(issue)

    def should_auto_apply(self, issue: Issue, threshold: float = 0.85) -> bool:
        return self.kb.should_auto_apply(issue, threshold)

    def summary(self) -> dict:
        return self.kb.summary()

    def history(self, limit: int = 20) -> list[dict]:
        return self.kb.data["history"][-limit:]
