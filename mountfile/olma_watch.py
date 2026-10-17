# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""OLMA Watch - crash monitoring with AI triage and GitHub issue reports.

The module watches the running program for unhandled exceptions:

* ``sys.excepthook`` and ``threading.excepthook`` are wrapped;
* Qt's fatal messages are intercepted through ``qInstallMessageHandler``;
* every report is sanitized (no user names, no home paths, no tokens);
* a traceback fingerprint deduplicates reports: a repeat comments on the
  existing issue instead of opening a new one;
* the traceback is summarized by the local Ollama model already configured
  for the «AI» button (cloud backends are deliberately not used);
* reports go to the GitHub REST API (``POST /repos/{owner}/{repo}/issues``);
* the GitHub token is read from the ``MOUNTFILE_OLMA_TOKEN`` environment
  variable (or the ``olma_token`` settings key) - it is NEVER hard-coded and
  never appears in the log, the report or the repository;
* when the network or Ollama is down, the report is queued in a local state
  file and sent on the next start (``flush_pending``).

Everything here is opt-in: the feature is off until the user enables
«OLMA Watch» in Настройки (or sets ``MOUNTFILE_OLMA_WATCH=1``), and the
monitor itself never raises - a failure inside the watcher must not make
the program worse.

Run the tests:  python -m pytest tests/test_olma_watch.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.request

# ------------------------------------------------------------ constants

#: Environment switch: "1" forces the watcher on, "0" forces it off.
ENV_ENABLE = "MOUNTFILE_OLMA_WATCH"

#: Environment variable holding the GitHub token (fine-grained PAT with
#: the "issues: write" permission on this repository is enough).
ENV_TOKEN = "MOUNTFILE_OLMA_TOKEN"

#: Optional environment override of the target repository ("owner/name").
ENV_REPO = "MOUNTFILE_OLMA_REPO"

#: Default destination repository (the project itself).
DEFAULT_REPO = "Alexkkkkk/mount-file"

GITHUB_API = "https://api.github.com"
ISSUE_LABEL = "olma-watch"

#: Rolling-hour cap: a stuck loop must not flood the issue tracker.
MAX_PER_HOUR = 6

#: Seconds allowed for the local Ollama triage; the watcher must not stall
#: a crashing process for long.
ANALYZE_TIMEOUT = 20

HTTP_TIMEOUT = 15

#: Model used when the user has not configured one for the AI button.
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder"

_LOCK = threading.Lock()
_STATE = None
_PREV = {}

ANALYSIS_PROMPT = (
    "Программа упала с необработанным исключением. Трассировка ниже.\n"
    "Ответь по-русски и кратко в трёх строках:\n"
    "1) вероятная причина;\n"
    "2) какое место кода проверить;\n"
    "3) как исправить.\n"
    "Не выдумывай строки, которых нет в трассировке."
)


# ------------------------------------------------------------ state file


def state_path():
    """Same config directory the UI settings live in."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "mount-file", "olma-watch.json")


def _new_state():
    return {"sent": [], "pending": [], "issues": {}}


def _load_state():
    global _STATE
    with _LOCK:
        if _STATE is None:
            _STATE = _new_state()
            try:
                with open(state_path(), encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _STATE.update(
                        {
                            "sent": [float(t) for t in data.get("sent", [])],
                            "pending": list(data.get("pending", [])),
                            "issues": dict(data.get("issues", {})),
                        }
                    )
            except (OSError, ValueError, TypeError):
                pass
        return dict(_STATE)


def _save_state(state):
    global _STATE
    with _LOCK:
        _STATE = state
        try:
            os.makedirs(os.path.dirname(state_path()), exist_ok=True)
            with open(state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True)
        except OSError:
            pass


def reset_state_for_tests():
    """Drop the cached state (only tests use this)."""
    global _STATE
    with _LOCK:
        _STATE = None


# ------------------------------------------------------------ switches


def enabled():
    """The watcher is opt-in: a settings toggle or the environment flag."""
    force = os.environ.get(ENV_ENABLE, "").strip().lower()
    if force in ("0", "false", "off", "no"):
        return False
    if force in ("1", "true", "on", "yes"):
        return True
    try:
        from mountfile import tc_style

        return bool(tc_style.SETTINGS.get("olma_watch"))
    except Exception:
        return False


def github_token():
    """Token from the environment, else from the user settings file.

    The token is never hard-coded here and never written to the log or
    to any generated report.
    """
    token = os.environ.get(ENV_TOKEN, "").strip()
    if token:
        return token
    try:
        from mountfile import tc_style

        return str(tc_style.SETTINGS.get("olma_token") or "").strip()
    except Exception:
        return ""


def _ollama_settings():
    """Settings snapshot for the local Ollama triage, or ``None``.

    Cloud (OpenAI-compatible) backends are deliberately not used: a crash
    report must not leave the machine through a paid third-party endpoint.
    """
    base = "http://127.0.0.1:11434"
    model = ""
    try:
        from mountfile import tc_style

        settings = tc_style.SETTINGS
        if settings.get("ai_backend") == "openai":
            return None
        base = settings.get("ai_base_url") or base
        model = settings.get("ai_model") or ""
    except Exception:
        pass
    return {
        "ai_backend": "ollama",
        "ai_base_url": base,
        "ai_model": model or DEFAULT_OLLAMA_MODEL,
        "ai_max_bytes": 262144,
    }


# ------------------------------------------------------------ sanitizing

_HOME = os.path.expanduser("~")
_USER_PATH_RE = re.compile(r"(?i)(/home/|/Users/|C:\\Users\\)[^/\\\s:;\"]+")
_SECRET_RE = re.compile(
    r"(?i)(gh[a-z]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|Bearer\s+\S+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)


def sanitize(text):
    """Strip user names, home paths, e-mails and token-like strings."""
    text = str(text)
    if _HOME and _HOME not in ("/",):
        text = text.replace(_HOME, "~")
    text = _USER_PATH_RE.sub(lambda m: m.group(1) + "<user>", text)
    text = _SECRET_RE.sub("<secret>", text)
    return text


def fingerprint(tb_text):
    """Stable short hash of a traceback: frames plus the exception line."""
    frames = re.findall(r'File "([^"]+)", line (\d+)', tb_text or "")
    tail = " | ".join("%s:%s" % (os.path.basename(f), n) for f, n in frames[-6:])
    lines = (tb_text or "").strip().splitlines()
    exc_line = lines[-1] if lines else ""
    return hashlib.sha1(
        sanitize(tail + "|" + exc_line).encode("utf-8", "replace")
    ).hexdigest()[:12]


# ------------------------------------------------------------ HTTP helper


def _http_json(method, url, payload=None, token=None, timeout=HTTP_TIMEOUT):
    """One JSON request; returns ``(status, data, error)``, never raises."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "mountfile-olma-watch",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = resp.read()
    except Exception as e:  # HTTPError, URLError, socket errors - all alike
        return None, None, str(e) or e.__class__.__name__
    try:
        return status, json.loads(raw.decode("utf-8", "replace")), ""
    except ValueError as e:
        return status, None, "bad json: %s" % e


# ------------------------------------------------------------ Ollama triage


def _ollama_analyze(tb_text):
    """Local model triage of the traceback; '' when unavailable."""
    settings = _ollama_settings()
    if settings is None:
        return ""
    try:
        from mountfile import ai
    except Exception:
        return ""
    backend = ai.build_backend(settings)
    result = backend.analyze(
        "traceback.txt",
        tb_text.encode("utf-8", "replace")[:60000],
        prompt=ANALYSIS_PROMPT,
    )
    if result is not None and getattr(result, "ok", False):
        return sanitize(result.text)
    return ""


# ------------------------------------------------------------ report body


def compose_report(tb_text, context="", occurrences=1, analysis=""):
    """``(title, body)`` for the issue; everything passed is sanitized."""
    fp = fingerprint(tb_text)
    lines = (tb_text or "").strip().splitlines()
    short = lines[-1] if lines else "Unknown error"
    title = sanitize(short).replace("`", "'")[:90] or "Unknown error"
    parts = [
        "## OLMA Watch: автоматический отчёт об ошибке",
        "",
        "<!-- olma-fp: %s -->" % fp,
        "",
        "**Исключение:** %s" % sanitize(short).replace("`", "'"),
        "**Контекст:** %s" % (sanitize(context) or "неизвестно"),
        "**Повторов:** %d" % occurrences,
        "",
        "<details><summary>Трассировка (пути и имена очищены)</summary>",
        "",
        "```",
        sanitize(tb_text).strip()[:6000],
        "```",
        "",
        "</details>",
    ]
    if analysis:
        parts += ["", "### Разбор OLMA (локальная модель)", "", analysis.strip()[:4000]]
    parts += [
        "",
        "---",
        "Отчёт собран автоматически (OLMA Watch). Имена пользователей, "
        "домашние пути и токены вычищены; модель разбора - локальная Ollama.",
    ]
    return "OLMA Watch: %s" % title, "\n".join(parts)


# ------------------------------------------------------------ delivery


def _repo():
    return os.environ.get(ENV_REPO, "").strip() or DEFAULT_REPO


def _find_issue(repo, fp, token):
    """Number of the open labelled issue holding this fingerprint, or None."""
    url = "%s/repos/%s/issues?state=open&labels=%s&per_page=100" % (
        GITHUB_API,
        repo,
        ISSUE_LABEL,
    )
    status, data, _err = _http_json("GET", url, token=token)
    if status != 200 or not isinstance(data, list):
        return None
    marker = "olma-fp: %s" % fp
    for item in data:
        if marker in (item.get("body") or ""):
            return item.get("number")
    return None


def _deliver(tb_text, context, state, analysis=""):
    """Send one report (create or comment). Returns the issue URL or ''."""
    fp = fingerprint(tb_text)
    known = state["issues"].get(fp) or {}
    occurrences = int(known.get("count", 0)) + 1
    title, body = compose_report(tb_text, context, occurrences, analysis=analysis)
    repo, token = _repo(), github_token()
    if not token:
        return "", "нет токена (%s не задан)" % ENV_TOKEN

    number = known.get("number") or _find_issue(repo, fp, token)
    if number:
        url = "%s/repos/%s/issues/%s/comments" % (GITHUB_API, repo, number)
        comment = "Повтор №%d того же исключения (olma-fp: %s).\n\nКонтекст: %s" % (
            occurrences,
            fp,
            sanitize(context) or "неизвестно",
        )
        status, _data, err = _http_json(
            "POST", url, payload={"body": comment}, token=token
        )
        if status in (201, 200):
            issue_url = "https://github.com/%s/issues/%s" % (repo, number)
        else:
            issue_url, err = "", err or "HTTP %s" % status
    else:
        status, data, err = _http_json(
            "POST",
            "%s/repos/%s/issues" % (GITHUB_API, repo),
            payload={"title": title, "body": body, "labels": [ISSUE_LABEL]},
            token=token,
        )
        if status == 201 and isinstance(data, dict):
            issue_url = data.get("html_url") or ""
        else:
            issue_url, err = "", err or "HTTP %s" % status
    return issue_url, err


def report_text(tb_text, context="", wait=True):
    """Public entry: analyze + deliver one formatted traceback.

    Returns ``(issue_url, error)``; the report is queued locally when the
    network is down.
    """
    if not enabled():
        return None, "мониторинг отключён"
    state = _load_state()
    now = time.time()
    state["sent"] = [t for t in state["sent"] if now - t < 3600]
    if len(state["sent"]) >= MAX_PER_HOUR:
        _save_state(state)
        return None, "достигнут лимит отчётов (%d в час)" % MAX_PER_HOUR

    try:
        analysis = _ollama_analyze(tb_text)
    except Exception:
        analysis = ""

    url, err = _deliver(tb_text, context, state, analysis=analysis)
    if url:
        fp = fingerprint(tb_text)
        known = state["issues"].get(fp) or {}
        state["issues"][fp] = {
            "number": known.get("number")
            or (url.rstrip("/").split("/")[-1] if url else None),
            "url": url,
            "count": int(known.get("count", 0)) + 1,
        }
        state["sent"].append(now)
        _save_state(state)
        return url, ""
    # Network down / token missing: keep the report for the next start.
    state["pending"].append(
        {
            "traceback": tb_text[:20000],
            "context": context,
            "time": now,
        }
    )
    _save_state(state)
    return None, err or "отчёт отложен"


def flush_pending(log=None):
    """Try to deliver the reports queued while offline."""
    state = _load_state()
    if not state["pending"]:
        return 0
    sent = 0
    still = []
    for item in state["pending"]:
        url, _err = _deliver(item.get("traceback", ""), item.get("context", ""), state)
        if url:
            sent += 1
            fp = fingerprint(item.get("traceback", ""))
            known = state["issues"].get(fp) or {}
            state["issues"][fp] = {
                "number": known.get("number")
                or (url.rstrip("/").split("/")[-1] if url else None),
                "url": url,
                "count": int(known.get("count", 0)) + 1,
            }
            state["sent"].append(time.time())
        else:
            still.append(item)
    state["pending"] = still
    _save_state(state)
    if sent and log is not None:
        try:
            log("OLMA Watch: отправлено отложенных отчётов: %d" % sent)
        except Exception:
            pass
    return sent


# ------------------------------------------------------------ the watcher


def _handle_text(tb_text, context="", wait=True):
    """Worker behind all hooks; never raises."""
    if "olma_watch" in tb_text:
        return  # never report the watcher's own failures
    if wait:
        try:
            report_text(tb_text, context, wait=True)
        except Exception:
            pass
        return

    def _worker():
        try:
            report_text(tb_text, context, wait=True)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True, name="olma-watch").start()


def install(qt_app=None, log=None):
    """Wrap the excepthooks (and Qt's message handler); returns True.

    Called once at startup from ``main()``. Every failure inside the
    watcher is swallowed: monitoring must never break the program.
    """
    previous = sys.excepthook

    def sys_hook(exc_type, exc_value, exc_tb):
        try:
            if exc_type not in (KeyboardInterrupt, SystemExit):
                text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
                _handle_text(text, context="основной поток", wait=True)
        except Exception:
            pass
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = sys_hook

    previous_thread = threading.excepthook

    def thread_hook(args):
        try:
            if args.exc_type not in (SystemExit, KeyboardInterrupt):
                text = "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                )
                name = getattr(args.thread, "name", "") or "поток"
                _handle_text(text, context="фоновый поток (%s)" % name)
        except Exception:
            pass
        if previous_thread is not None:
            previous_thread(args)

    threading.excepthook = thread_hook

    if qt_app is not None:
        try:
            from mountfile import qt_compat

            QtCore = qt_compat.QtCore

            def qt_handler(mode, _context, message):
                try:
                    fatal = getattr(QtCore.QtMsgType, "QtFatalMsg", None)
                    warning = getattr(QtCore.QtMsgType, "QtWarningMsg", None)
                    critical = getattr(QtCore.QtMsgType, "QtCriticalMsg", None)
                    if fatal is not None and mode == fatal:
                        _handle_text("QtFatal: %s" % message, context="Qt")
                    elif log is not None and mode in (warning, critical):
                        log("Qt: %s" % message)
                except Exception:
                    pass

            QtCore.qInstallMessageHandler(qt_handler)
        except Exception:
            pass

    try:
        if enabled():
            flush_pending(log=log)
    except Exception:
        pass
    return True
