# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""AI-советник v2 — мульти-гипотезный разбор формата файла.

Модуль стоит поверх кнопки «AI» (см. :mod:`mountfile.ai`): сначала по
сигнатурам и расширению ставятся гипотезы о формате (:class:`HypothesisEngine`),
затем гипотезы и «слепок» файла отдаются модели через адаптер бэкенда
(:class:`LLMBackendAdapter`). При включённом консенсусе отвечают два
настроенных бэкенда и их мнения показываются рядом.

Отчёт о неизвестном формате можно отправить в GitHub Issues
(:class:`GitHubReporter`); токен не пишется ни в журнал, ни в отчёт.

Публичная поверхность (используется из ``tc_window``):

* :func:`advise_and_show` — пункт меню «AI-советник v2 (multi-hypothesis)»;
* :func:`maybe_advise` — авто-хук при открытии файла (только для
  неуверенных гипотез, включается настройкой ``ai_format_autohook``);
* :func:`build_fingerprint`, :class:`HypothesisEngine`,
  :class:`LLMBackendAdapter`, :class:`Calibration`,
  :class:`FormatAdvisorDialog`, :class:`GitHubReporter`.

Run the tests:  python -m pytest tests/test_ai_advisor.py -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request

from .qt_compat import (
    DlgActionRole,
    DlgClose,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    exec_dialog,
)

#: Сколько байт файла уходит в «слепок» (сигнатуры + энтропия).
HEAD_BYTES = 4096

#: Порог уверенности: гипотезы ниже не считаются ответом, а поводом спросить.
CONFIDENT_SCORE = 0.7

#: HTTP timeout for a GitHub issue report.
GITHUB_TIMEOUT = 15

# ------------------------------------------------------------ fingerprint


def build_fingerprint(path, head=None, size=None):
    """Слепок файла: имя, размер, первые байты, sha1 и грубая энтропия.

    ``head``/``size`` позволяют передать уже прочитанные байты — для файла
    внутри смонтированного образа, который нельзя открыть по пути.
    """
    data = b""
    if head is None:
        try:
            if size is None:
                size = os.path.getsize(path)
            with open(path, "rb") as f:
                data = f.read(HEAD_BYTES)
        except OSError:
            pass
    else:
        data = head[:HEAD_BYTES]
    return {
        "path": path,
        "name": os.path.basename(path),
        "size": size,
        "head": data,
        "sha1": hashlib.sha1(data).hexdigest() if data else "",
        "entropy": _entropy(data),
    }


def _entropy(data):
    """Shannon entropy of the head, 0..8 bits/byte (0 for empty)."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    ent = 0.0
    for c in counts:
        if c:
            p = c / len(data)
            ent -= p * math.log2(p)
    return round(ent, 2)


# ------------------------------------------------------------ hypotheses

#: (смещение, сигнатура, название гипотезы, базовая уверенность)
_SIGNATURES = (
    (0, b"\x55\xaa", None, 0.0),  # placeholder, MBR checked separately
    (0, b"PK\x03\x04", "ZIP-контейнер (архив/docx/apk)", 0.95),
    (0, b"\x1f\x8b", "gzip-поток", 0.9),
    (0, b"\x89PNG", "PNG-изображение", 0.95),
    (0, b"\xff\xd8\xff", "JPEG-изображение", 0.95),
    (0, b"%PDF", "PDF-документ", 0.95),
    (0, b"\x7fELF", "ELF-исполняемый файл", 0.95),
    (0, b"MZ", "PE/EXE-исполняемый файл", 0.9),
    (0, b"\x28\xb5\x2f\xfd", "Zstandard-сжатие", 0.9),
    (0, b"BZh", "bzip2-сжатие", 0.9),
    (0, b"\xfd7zXZ", "xz-сжатие", 0.9),
    (0x8001, b"CD001", "ISO 9660 образ диска", 0.95),
)

#: Расширение -> (название, уверенность подсказки).
_EXTENSIONS = {
    ".bin": ("двоичный дамп (прошивка/RT809?)", 0.4),
    ".img": ("сырой образ диска", 0.5),
    ".iso": ("образ оптического диска", 0.6),
    ".zip": ("ZIP-архив", 0.6),
    ".fw": ("файл прошивки", 0.4),
    ".hex": ("текстовый hex-дамп", 0.5),
    ".rom": ("дамп ПЗУ", 0.4),
}


def _mbr_like(fp):
    """Классическая загрузочная подпись MBR в конце первого сектора."""
    head = fp.get("head") or b""
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        return "Загрузочная подпись MBR (0x55AA в конце сектора)", 0.7
    return "", 0.0


class HypothesisEngine:
    """Ставит гипотезы по слепку: сигнатуры, расширение, MBR, энтропия."""

    def __init__(self, adapter, calibration=None):
        self.adapter = adapter
        self.calibration = calibration or Calibration()

    def diagnose(self, fp):
        """Список гипотез (dict: name/score/evidence), лучший — первым."""
        hyps = []
        head = fp.get("head") or b""
        for offset, magic, name, score in _SIGNATURES:
            if name and head[offset : offset + len(magic)] == magic:
                hyps.append({"name": name, "score": score, "evidence": "сигнатура"})
        mbr_evidence, mbr_score = _mbr_like(fp)
        if mbr_evidence:
            hyps.append(
                {
                    "name": "Образ диска с MBR",
                    "score": mbr_score,
                    "evidence": mbr_evidence,
                }
            )
        ext = os.path.splitext(fp.get("name") or "")[1].lower()
        if ext in _EXTENSIONS:
            name, score = _EXTENSIONS[ext]
            hyps.append({"name": name, "score": score, "evidence": "расширение"})
        if fp.get("entropy", 0.0) > 7.5 and not hyps:
            hyps.append(
                {
                    "name": "Сжатые или зашифрованные данные",
                    "score": 0.45,
                    "evidence": "высокая энтропия",
                }
            )
        if not hyps:
            hyps.append(
                {
                    "name": "Неизвестный формат",
                    "score": 0.2,
                    "evidence": "совпадений не найдено",
                }
            )
        hyps.sort(key=lambda h: h["score"], reverse=True)
        return hyps


class Calibration:
    """Пороги уверенности (вынесены, чтобы можно было калибровать)."""

    confident_score = CONFIDENT_SCORE

    def is_confident(self, hyps):
        return bool(hyps) and hyps[0]["score"] >= self.confident_score


# ------------------------------------------------------------ LLM adapter


class LLMBackendAdapter:
    """Единый «спросить модель» поверх любого бэкенда кнопки AI."""

    def __init__(self, backend):
        self.backend = backend

    @property
    def name(self):
        return getattr(self.backend, "name", "unknown")

    def ask(self, fp, hypotheses):
        """Ответ модели по слепку и гипотезам; текстом, без исключений."""
        head = fp.get("head") or b""
        prompt = (
            "Гипотезы о формате: %s\n"
            "Энтропия: %s бит/байт, размер: %s байт.\n"
            "Подтверди или опровергни гипотезы, укажи наиболее вероятный "
            "формат и как его проверить."
            % (
                "; ".join("%s (%.2f)" % (h["name"], h["score"]) for h in hypotheses)
                or "нет",
                fp.get("entropy"),
                fp.get("size"),
            )
        )
        result = self.backend.analyze(
            fp.get("name") or "file",
            head,
            prompt=prompt,
            size=fp.get("size"),
        )
        return result


# ------------------------------------------------------------ GitHub


class GitHubReporter:
    """Отчёт о неизвестном формате в GitHub Issues (opt-in, нужен токен)."""

    def __init__(self, repo, token):
        repo = (repo or "").strip()
        token = (token or "").strip()
        if "/" not in repo or not token:
            raise ValueError("нужны репозиторий owner/name и токен")
        self.repo = repo
        self.token = token

    def create_issue(self, title, body):
        """``(ok, url|error)`` — никогда не бросает исключений наружу."""
        url = "https://api.github.com/repos/%s/issues" % self.repo
        payload = json.dumps(
            {"title": title[:200], "body": body[:6000], "labels": ["ai-advisor"]}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                return True, data.get("html_url", "")
        except Exception as e:
            # Токен в текст ошибки не попадает.
            return False, str(e)[:200]


# ------------------------------------------------------------ dialog


class FormatAdvisorDialog(QDialog):
    """Диалог советника: гипотезы, ответы модели, отчёт на GitHub."""

    def __init__(self, fp, adapters, calibration=None, github=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI-советник — %s" % (fp.get("name") or ""))
        self.resize(720, 520)
        self.fp = fp
        self.adapters = adapters
        self.calibration = calibration or Calibration()
        self.github = github

        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "Файл: %s · %s байт · энтропия %s бит/байт"
                % (fp.get("name"), fp.get("size"), fp.get("entropy"))
            )
        )
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        lay.addWidget(self.txt, 1)
        btn_issue = QPushButton("Создать issue на GitHub")
        btn_issue.clicked.connect(self.report_issue)
        lay.addWidget(btn_issue, 0)
        buttons = QDialogButtonBox()
        self.btn_run = buttons.addButton("Спросить модель", DlgActionRole)
        self.btn_run.clicked.connect(self.ask_models)
        buttons.addButton(DlgClose)
        lay.addWidget(buttons)
        self._render(hyps=None, results=[])

    def _render(self, hyps, results):
        lines = []
        if hyps is not None:
            lines.append("Гипотезы:")
            lines += [
                "  · %s — уверенность %.2f (%s)"
                % (h["name"], h["score"], h["evidence"])
                for h in hyps
            ]
            confident = self.calibration.is_confident(hyps)
            lines.append(
                "Оценка: %s"
                % (
                    "формат распознан уверенно"
                    if confident
                    else "уверенности нет — стоит спросить модель"
                )
            )
        for r in results:
            lines.append("\n— ответ модели (%s) —\n%s" % (r[0], r[1]))
        if not results:
            lines.append("\nНажмите «Спросить модель» для анализа.")
        self.txt.setPlainText("\n".join(lines))

    def ask_models(self):
        """Опросить все подключённые бэкенды (консенсус — если их двое)."""
        self.btn_run.setEnabled(False)
        try:
            engine = HypothesisEngine(self.adapters[0], self.calibration)
            hyps = engine.diagnose(self.fp)
            results = []
            for adapter in self.adapters:
                result = adapter.ask(self.fp, hyps)
                text = result.text if result.ok else "Ошибка: " + result.error
                results.append((adapter.name, text))
            self._render(hyps, results)
        finally:
            self.btn_run.setEnabled(True)

    def report_issue(self):
        """Отчёт о разборе файла; токен не пишется в диалог и в журнал."""
        if self.github is None:
            self.txt.appendPlainText(
                "\nОтчёт отключён: заполните репозиторий и токен в Настройках."
            )
            return
        ok, info = self.github.create_issue(
            "AI-советник: неизвестный формат — %s" % (self.fp.get("name"),),
            "Файл: %s\nРазмер: %s байт\nЭнтропия: %s\nsha1(head): %s\n\n%s"
            % (
                self.fp.get("name"),
                self.fp.get("size"),
                self.fp.get("entropy"),
                self.fp.get("sha1"),
                self.txt.toPlainText()[:3000],
            ),
        )
        self.txt.appendPlainText(
            "\nIssue создан: %s" % info
            if ok
            else "\nНе удалось создать issue: %s" % info
        )


# ------------------------------------------------------------ entry points


def advise_and_show(path, backends, github=None, parent=None, head=None, size=None):
    """Меню «AI-советник v2»: слепок, гипотезы, ответы моделей, issue."""
    fp = build_fingerprint(path, head=head, size=size)
    adapters = [LLMBackendAdapter(b) for b in backends]
    dlg = FormatAdvisorDialog(fp, adapters, Calibration(), github=github, parent=parent)
    return exec_dialog(dlg)


def maybe_advise(settings, path, parent=None, log=None, head=None):
    """Авто-хук: диалог только для файлов с неуверенными гипотезами."""
    log = log or (lambda msg: None)
    fp = build_fingerprint(path, head=head)
    calibration = Calibration()
    # Бэкенд берётся тем же построителем, что и кнопка «AI».
    from .ai import build_backend

    adapter = LLMBackendAdapter(build_backend(settings or {}))
    engine = HypothesisEngine(adapter, calibration)
    hyps = engine.diagnose(fp)
    if calibration.is_confident(hyps):
        return  # формат и так ясен — не мешаем пользователю
    github = None
    repo = (settings or {}).get("ai_github_repo") or ""
    token = (settings or {}).get("ai_github_token") or ""
    if repo and token:
        try:
            github = GitHubReporter(repo, token)
        except ValueError:
            github = None
    log("AI-советник: формат %s не распознан уверенно" % fp.get("name"))
    dlg = FormatAdvisorDialog(fp, [adapter], calibration, github, parent)
    exec_dialog(dlg)
