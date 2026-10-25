# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Pluggable AI backends behind the «AI» toolbar button.

The module is deliberately free of Qt and performs no network traffic on
import: a backend is contacted only when :meth:`AiBackend.analyze` is called
from an explicit user action. A local Ollama server and any OpenAI-compatible
``/v1/chat/completions`` endpoint are supported, plus a :class:`NullBackend`
that answers with a readable message instead of raising when nothing is
configured.

Reading a file for analysis goes through :func:`collect`, which streams it in
chunks and reports whether the data was truncated, so a disk image is never
loaded into memory whole.
"""

from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Never read more than this many bytes of a file for one analysis.
DEFAULT_MAX_BYTES = 256 * 1024

#: HTTP timeout (seconds) for a single request.
DEFAULT_TIMEOUT = 60

#: Read the file in chunks of this size.
CHUNK = 1 << 20

#: How many bytes of a binary file are rendered as a hex dump for the prompt.
HEX_PREVIEW = 4096

#: How many bytes are sniffed to decide text vs binary.
SNIFF = 4096

#: Bytes that make a file look like text.
_TEXT_OK = frozenset(bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b")

SYSTEM_PROMPT = (
    "Ты — инженер-исследователь, разбирающий содержимое файлов, дампов и "
    "образов дисков. Отвечай по-русски, кратко и по делу: сначала что это за "
    "данные, затем структура, затем что подозрительно или интересно. "
    "Не выдумывай факты, которых нет в переданных данных."
)


@dataclass
class AiResult:
    """Outcome of one analysis: text on success, ``error`` otherwise."""

    ok: bool
    backend: str = ""
    model: str = ""
    text: str = ""
    error: str = ""
    bytes_read: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "model": self.model,
            "text": self.text,
            "error": self.error,
            "bytes_read": self.bytes_read,
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #


def collect(reader, max_bytes=DEFAULT_MAX_BYTES, chunk=CHUNK):
    """Read up to ``max_bytes`` through ``reader(offset, length)``.

    Returns ``(data, truncated)``. ``reader`` works for both a local file and
    a file inside a mounted image, so the two paths stay identical.
    """
    out = bytearray()
    offset = 0
    while len(out) < max_bytes:
        want = min(chunk, max_bytes - len(out))
        part = reader(offset, want)
        if not part:
            break
        out += part
        offset += len(part)
    truncated = bool(reader(offset, 1))
    return bytes(out), truncated


def local_reader(path):
    """A ``reader(offset, length)`` over a file on disk."""

    def _read(offset, length):
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    return _read


def looks_textual(data):
    """True for UTF-8 text (any script) or mostly printable ASCII bytes.

    A plain byte-class check alone misclassifies multi-byte UTF-8 (Cyrillic,
    CJK): those bytes are outside ``_TEXT_OK`` and push the printable ratio
    under the threshold, so a Russian text file went to the model as a hex
    dump. A valid UTF-8 decode of the sniffed sample settles it first.
    """
    sample = data[:SNIFF]
    if not sample or b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    good = sum(1 for b in sample if b in _TEXT_OK)
    return good / len(sample) >= 0.90


def summarize_bytes(name, data, truncated=False, size=None):
    """Render the file as prompt text: decoded text or a hex dump."""
    total = len(data) if size is None else size
    head = "Файл: %s\nРазмер: %s байт\nПрочитано: %d байт%s\n\n" % (
        name,
        total,
        len(data),
        " (данные усечены)" if truncated else "",
    )
    if looks_textual(data):
        return head + data.decode("utf-8", "replace")
    lines = []
    view = data[:HEX_PREVIEW]
    for off in range(0, len(view), 16):
        row = view[off : off + 16]
        hexpart = " ".join("%02X" % b for b in row)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append("%08X  %-47s  %s" % (off, hexpart, ascii_part))
    if len(data) > HEX_PREVIEW:
        lines.append("... (показаны первые %d байт)" % HEX_PREVIEW)
    return head + "\n".join(lines)


def build_prompt(name, data, user_prompt=None, truncated=False, size=None):
    """Full prompt: the rendered file plus the optional user instruction."""
    parts = [summarize_bytes(name, data, truncated=truncated, size=size)]
    if user_prompt:
        parts.append("\n\nЗадача от пользователя:\n" + user_prompt)
    return "\n".join(parts)


def _post_json(url, payload, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST ``payload`` as JSON; return ``(data, error)``, never raise."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        return None, "HTTP %s: %s" % (e.code, detail or e.reason)
    except urllib.error.URLError as e:
        return None, "Нет соединения: %s" % (e.reason,)
    except Exception as e:  # pragma: no cover - defensive
        return None, "Ошибка запроса: %s" % e
    try:
        return json.loads(raw.decode("utf-8", "replace")), ""
    except Exception as e:
        return None, "Некорректный ответ сервера: %s" % e


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class AiBackend:
    """Base class: one backend answers one ``analyze`` call."""

    name = "base"
    label = "—"

    def available(self):
        """True when the backend looks reachable/configured."""
        return False

    def analyze(self, name, data, prompt=None, truncated=False, size=None):
        raise NotImplementedError


class NullBackend(AiBackend):
    """Used when nothing is configured: a message instead of a traceback."""

    name = "none"
    label = "Не настроен"

    def analyze(self, name, data, prompt=None, truncated=False, size=None):
        return AiResult(
            ok=False,
            backend=self.name,
            error=(
                "AI-бэкенд не настроен.\n"
                "Откройте Конфигурация → Настройки и нажмите "
                "«Проверить и настроить Ollama», либо укажите адрес и модель "
                "локального "
                "Ollama (по умолчанию http://127.0.0.1:11434) либо "
                "OpenAI-совместимый эндпоинт с ключом."
            ),
            bytes_read=len(data or b""),
        )


#: The default local Ollama address; the OpenAI backend must never inherit
#: it silently (a /v1/chat/completions call against an Ollama server only
#: produces a confusing error).
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


def discover_ollama(base_url=OLLAMA_DEFAULT_URL, timeout=3):
    """Return ``(reachable, models, error)`` for an explicit Ollama probe."""
    url = (base_url or OLLAMA_DEFAULT_URL).rstrip("/")
    request = urllib.request.Request(url + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return False, [], "Нет соединения с Ollama: %s" % exc.reason
    except Exception as exc:  # pragma: no cover - defensive network boundary
        return False, [], "Ollama недоступна: %s" % exc
    models = (
        [
            str(item["name"])
            for item in payload.get("models", [])
            if isinstance(item, dict) and item.get("name")
        ]
        if isinstance(payload, dict)
        else []
    )
    return True, models, ""


def configure_local_ollama(settings, base_url=OLLAMA_DEFAULT_URL):
    """Configure settings from reachable Ollama without downloading anything."""
    reachable, models, error = discover_ollama(base_url)
    if not reachable or not models:
        return {
            "ok": False,
            "reachable": reachable,
            "models": models,
            "error": error or ("Ollama запущена, но локальная модель не загружена."),
        }
    current = str((settings or {}).get("ai_model") or "")
    model = current if current in models else models[0]
    settings["ai_backend"] = "ollama"
    settings["ai_base_url"] = (base_url or OLLAMA_DEFAULT_URL).rstrip("/")
    settings["ai_model"] = model
    return {
        "ok": True,
        "reachable": True,
        "models": models,
        "model": model,
        "error": "",
    }


def _openai_base_url(settings):
    """Base URL for the OpenAI backend, never inheriting Ollama's default."""
    url = (settings.get("ai_base_url_openai") or "").strip()
    if url:
        return url
    generic = (settings.get("ai_base_url") or "").strip()
    if generic and generic.rstrip("/") != OLLAMA_DEFAULT_URL:
        return generic
    return ""  # empty -> the backend's own default (https://api.openai.com)


def _api_key_transport_error(base_url):
    """Require HTTPS for API keys, except HTTP endpoints on loopback."""
    try:
        parsed = urlsplit(base_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
    except ValueError:
        return "Некорректный адрес OpenAI-совместимого эндпоинта."
    if scheme == "https":
        return ""
    if scheme == "http" and host:
        if host.lower().rstrip(".") == "localhost":
            return ""
        try:
            if ipaddress.ip_address(host).is_loopback:
                return ""
        except ValueError:
            pass
    return "Ключ API можно отправлять только через HTTPS или loopback HTTP."


class OllamaBackend(AiBackend):
    """Local Ollama server (``POST /api/generate``)."""

    name = "ollama"
    label = "Ollama (локально)"

    def __init__(
        self, base_url=OLLAMA_DEFAULT_URL, model="llama3", timeout=DEFAULT_TIMEOUT
    ):
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or "llama3"
        self.timeout = timeout

    def available(self):
        req = urllib.request.Request(self.base_url + "/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return getattr(resp, "status", 200) == 200
        except Exception:
            return False

    def analyze(self, name, data, prompt=None, truncated=False, size=None):
        payload = {
            "model": self.model,
            "prompt": build_prompt(name, data, prompt, truncated, size),
            "system": SYSTEM_PROMPT,
            "stream": False,
        }
        body, err = _post_json(
            self.base_url + "/api/generate", payload, timeout=self.timeout
        )
        if body is None:
            return AiResult(
                ok=False,
                backend=self.name,
                model=self.model,
                error=err,
                bytes_read=len(data or b""),
                truncated=truncated,
            )
        text = (body.get("response") or "").strip()
        if not text:
            return AiResult(
                ok=False,
                backend=self.name,
                model=self.model,
                error="Модель вернула пустой ответ. Проверьте, что модель "
                "«%s» загружена (ollama pull %s)." % (self.model, self.model),
                bytes_read=len(data or b""),
                truncated=truncated,
            )
        return AiResult(
            ok=True,
            backend=self.name,
            model=self.model,
            text=text,
            bytes_read=len(data or b""),
            truncated=truncated,
        )


class OpenAiCompatBackend(AiBackend):
    """Any OpenAI-compatible ``/v1/chat/completions`` endpoint."""

    name = "openai"
    label = "OpenAI-совместимый эндпоинт"

    def __init__(
        self,
        base_url="https://api.openai.com",
        model="gpt-4o-mini",
        api_key="",
        timeout=DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or "https://api.openai.com").rstrip("/")
        self.model = model or "gpt-4o-mini"
        self.api_key = api_key or ""
        self.timeout = timeout

    def available(self):
        return bool(self.api_key)

    def analyze(self, name, data, prompt=None, truncated=False, size=None):
        if self.api_key:
            transport_error = _api_key_transport_error(self.base_url)
            if transport_error:
                return AiResult(
                    ok=False,
                    backend=self.name,
                    model=self.model,
                    error=transport_error,
                    bytes_read=len(data or b""),
                    truncated=truncated,
                )
        headers = {}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_prompt(name, data, prompt, truncated, size),
                },
            ],
        }
        body, err = _post_json(
            self.base_url + "/v1/chat/completions",
            payload,
            headers=headers,
            timeout=self.timeout,
        )
        if body is None:
            return AiResult(
                ok=False,
                backend=self.name,
                model=self.model,
                error=err,
                bytes_read=len(data or b""),
                truncated=truncated,
            )
        try:
            text = body["choices"][0]["message"]["content"].strip()
        except Exception:
            text = ""
        if not text:
            return AiResult(
                ok=False,
                backend=self.name,
                model=self.model,
                error="Сервер вернул ответ без текста (проверьте модель и ключ).",
                bytes_read=len(data or b""),
                truncated=truncated,
            )
        return AiResult(
            ok=True,
            backend=self.name,
            model=self.model,
            text=text,
            bytes_read=len(data or b""),
            truncated=truncated,
        )


#: Registry of the selectable backends: key -> (label, factory).
BACKENDS = {
    "none": (NullBackend.label, lambda s: NullBackend()),
    "ollama": (
        OllamaBackend.label,
        lambda s: OllamaBackend(base_url=s.get("ai_base_url"), model=s.get("ai_model")),
    ),
    "openai": (
        OpenAiCompatBackend.label,
        lambda s: OpenAiCompatBackend(
            base_url=_openai_base_url(s),
            model=s.get("ai_model"),
            api_key=s.get("ai_api_key"),
        ),
    ),
}


def build_backend(settings):
    """Build the backend selected in ``settings`` (a plain dict)."""
    key = (settings or {}).get("ai_backend") or "none"
    entry = BACKENDS.get(key)
    if entry is None:
        return NullBackend()
    return entry[1](settings or {})


def max_bytes(settings):
    """Configured read cap, clamped to a sane range."""
    try:
        value = int((settings or {}).get("ai_max_bytes") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return max(4096, min(value, 64 * 1024 * 1024))
