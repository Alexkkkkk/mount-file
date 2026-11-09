# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Автообновление Mount File из GitHub Releases.

Как это работает
----------------
Проверка может запускаться при старте приложения в фоновом потоке (QThread),
но по умолчанию сетевой доступ при старте выключен. Поэтому отсутствие сети
или битый URL вообще не мешают работе: в лог падает одна строка и всё.

Источник обновлений — GitHub Releases репозитория ``Alexkkkkk/mount-file``
(переопределяется переменной окружения ``MOUNTFILE_UPDATE_REPO``).
Версии сравниваются по числам (``1.10.0`` > ``1.9.9``), а не как строки.

Что происходит, когда найдена новая версия:

* автономная сборка (PyInstaller, ``sys.frozen``) — скачиваем подходящий
  для платформы asset (.exe / бинарник linux), сверяем sha256, если релиз
  его публикует, и подменяем файл:
    - Windows — запускается .cmd, который ждёт выхода процесса (занятый
      exe перезаписать нельзя), затем ``move`` + ``start``. Ожидание
      ОГРАНИЧЕНО: ``WINDOWS_WAIT_TRIES`` проверок, потом файл всё равно
      подменяется, а сам .cmd работает без консольного окна;
    - Linux/macOS — подмена файла на месте и ``os.execv``;
* запуск из исходников — файлы не трогаем: показываем ссылку на релиз
  (и, если это git-чекаут и ``MOUNTFILE_GIT_PULL=1``, делаем ``git pull``).

Переменные окружения
--------------------
``MOUNTFILE_UPDATE_REPO=owner/repo``  другой репозиторий с релизами
``MOUNTFILE_NO_UPDATE=1``             выключить проверку обновлений
``MOUNTFILE_ENABLE_UPDATE_CHECK=1``   разрешить сетевую проверку при старте
``MOUNTFILE_AUTO_UPDATE=1``           ставить без вопроса и перезапускаться
``MOUNTFILE_GIT_PULL=1``              в режиме исходников разрешить git pull
``MOUNTFILE_UPDATE_URL``              прямой URL на JSON последнего релиза
                                      (для внутреннего/корпоративного сервера)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# --- MOUNTFILE_FIX: OLMA safe updater --------------------------------------
try:
    from mountfile.olma.core import OlmaTransaction as _OlmaTx
    from mountfile.olma.core import safe_download as _olma_dl
    from mountfile.olma.core import verify_sha256 as _olma_sha
except Exception:
    _olma_dl = _olma_sha = _OlmaTx = None


def _safe_download(url, *, timeout=30.0, max_size=512 * 1024 * 1024):
    if _olma_dl is None:
        data = bytearray()
        with _request(url, timeout) as response:
            while True:
                chunk = response.read(min(CHUNK, max_size - len(data) + 1))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > max_size:
                    raise OSError("обновление превышает лимит размера")
    return _olma_dl(url, timeout=timeout, max_size=max_size)


def _verify_sha256(data, expected):
    if _olma_sha is None:
        return (
            isinstance(expected, str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", expected))
            and hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected.lower())
        )
    return _olma_sha(data, expected)


def _atomic_write(path, data):
    if _OlmaTx is None:
        fd, partial = tempfile.mkstemp(
            prefix=".mountfile-", dir=os.path.dirname(os.path.abspath(path))
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(partial, path)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
        return
    with _OlmaTx(path) as tx:
        tx.write_all(data)


# --- /MOUNTFILE_FIX --------------------------------------------------------
try:  # Qt нужен только фоновым классам внизу файла
    from .qt_compat import QThread, pyqtSignal

    HAVE_QT = True
except Exception:  # pragma: no cover - проверки/тесты работают и без Qt
    HAVE_QT = False

    class _BoundSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot.emit if hasattr(slot, "emit") else slot)

        def emit(self, *args, **kwargs):
            for slot in list(self._slots):
                slot(*args, **kwargs)

    class _NoQtSignal:
        """A signal descriptor with separate subscribers for every worker."""

        def __set_name__(self, owner, name):
            self._name = "_headless_signal_" + name

        def __get__(self, instance, owner):
            if instance is None:
                return self
            return instance.__dict__.setdefault(self._name, _BoundSignal())

    def pyqtSignal(*args, **kwargs):
        return _NoQtSignal()

    class QThread:
        """Small finite-worker implementation for manual headless callers."""

        finished = _NoQtSignal()

        def __init__(self, parent=None):
            self._parent = parent
            self._thread = None

        def parent(self):
            return self._parent

        def start(self):
            if self.isRunning():
                raise RuntimeError("worker is already running")

            def execute():
                try:
                    self.run()
                finally:
                    self.finished.emit()

            self._thread = threading.Thread(target=execute, daemon=True)
            self._thread.start()

        def isRunning(self):
            return self._thread is not None and self._thread.is_alive()

        def isFinished(self):
            return self._thread is not None and not self._thread.is_alive()

        def wait(self, timeout=3000):
            if self._thread is not None:
                self._thread.join(timeout / 1000)
            return not self.isRunning()


DEFAULT_REPO = "Alexkkkkk/mount-file"
USER_AGENT = "MountFile-Updater/1.0 (+https://github.com/Alexkkkkk/mount-file)"
TIMEOUT = 8  # секунд: проверка не должна задерживать старт
DOWNLOAD_TIMEOUT = 90
CHUNK = 64 * 1024
MAX_DOWNLOAD_SIZE = 1024 * 1024 * 1024

_TRUE = ("1", "true", "yes", "on", "да")


# ----------------------------------------------------------------- helpers
def _flag(name):
    return os.environ.get(name, "").strip().lower() in _TRUE


def repo():
    """Репозиторий с релизами (owner/repo)."""
    return (os.environ.get("MOUNTFILE_UPDATE_REPO") or DEFAULT_REPO).strip().strip("/")


def update_url():
    """URL JSON с последним релизом (по умолчанию — GitHub API)."""
    url = (os.environ.get("MOUNTFILE_UPDATE_URL") or "").strip()
    if url:
        return url
    return "https://api.github.com/repos/%s/releases/latest" % repo()


def disabled():
    """Whether automatic update checks are disabled.

    Network access is opt-in. Manual checks still call :func:`check` directly,
    while the GUI startup worker consults this gate first.
    """
    return _flag("MOUNTFILE_NO_UPDATE") or not _flag("MOUNTFILE_ENABLE_UPDATE_CHECK")


def auto_install():
    return _flag("MOUNTFILE_AUTO_UPDATE")


def is_frozen():
    """True для собранного exe/бинарника (PyInstaller ставит sys.frozen)."""
    return bool(getattr(sys, "frozen", False))


def parse_version(text):
    """``'v1.10.0-beta1'`` -> ``(1, 10, 0)``; нечисловые хвосты отбрасываются."""
    core = str(text or "").strip()
    if core[:1] in ("v", "V"):
        core = core[1:]
    core = core.split("-")[0].split("+")[0]
    parts = [int(p) for p in re.findall(r"\d+", core)]
    return tuple(parts) if parts else (0,)


def is_newer(remote, local):
    """Числовое сравнение версий: (1,10,0) > (1,9,9), (1,2) == (1,2,0)."""
    r, loc = parse_version(remote), parse_version(local)
    size = max(len(r), len(loc))
    r = r + (0,) * (size - len(r))
    loc = loc + (0,) * (size - len(loc))
    return r > loc


def _short(exc):
    text = str(exc).strip() or exc.__class__.__name__
    return text.replace("\n", " ")[:300]


def _on_windows7():
    try:
        version = sys.getwindowsversion()
        return sys.platform.startswith("win") and (version.major, version.minor) == (
            6,
            1,
        )
    except Exception:  # pragma: no cover - не Windows
        return False


def _asset_score(name):
    """Чем больше очков, тем лучше сборка подходит этой системе."""
    low = name.lower()
    if low.endswith(
        (
            ".sha256",
            ".txt",
            ".zip",
            ".json",
            ".dmg",
            ".pkg",
            ".gz",
            ".xz",
            ".deb",
            ".rpm",
        )
    ):
        return -1
    machine = platform.machine().lower()
    local_arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86": "x86",
        "i386": "x86",
        "i686": "x86",
    }.get(machine)
    asset_arch = None
    if re.search(r"(?:^|[-_.])(arm64|aarch64)(?:$|[-_.])", low):
        asset_arch = "arm64"
    elif re.search(r"(?:^|[-_.])(x86[-_]64|amd64|x64)(?:$|[-_.])", low):
        asset_arch = "x64"
    elif re.search(r"(?:^|[-_.])(i[36]86|x86)(?:$|[-_.])", low):
        asset_arch = "x86"
    if not local_arch or (asset_arch and asset_arch != local_arch):
        return -1
    if sys.platform.startswith("win"):
        if not low.endswith(".exe"):
            return -1
        if _on_windows7() or "win7" in low or "win-7" in low:
            # Current releases require Python 3.10/PyQt6 and cannot run on Win7.
            return -1
        if asset_arch is None and local_arch != "x64":
            return -1  # the project's generic Windows release is an x64 build
        score = 100 if low == "mountfile.exe" else 60
        if any(marker in low for marker in ("win10", "win-10", "windows10")):
            # Keep the generic binary preferred on modern Windows; it is the
            # compatibility build used by the existing release contract.
            score += 20
        return score
    if sys.platform == "darwin":
        return (
            80
            if asset_arch and any(k in low for k in ("macos", "darwin", "osx"))
            else -1
        )
    if sys.platform.startswith("linux"):
        return 80 if asset_arch and "linux" in low and not low.endswith(".exe") else -1
    return -1


def select_asset(release):
    """Подходящий для этой платформы файл релиза (или None)."""
    best, best_score = None, -1
    for asset in (release or {}).get("assets") or []:
        score = _asset_score(asset.get("name") or "")
        if score > best_score:
            best, best_score = asset, score
    return best if best_score >= 0 else None


def _request(url, timeout=TIMEOUT):
    if not url:
        raise ValueError("пустой URL обновления")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("URL обновления должен использовать HTTP(S)")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(repo_slug=None, timeout=TIMEOUT):
    """JSON последнего релиза (GitHub API или MOUNTFILE_UPDATE_URL)."""
    url = (
        update_url()
        if repo_slug is None
        else ("https://api.github.com/repos/%s/releases/latest" % repo_slug)
    )
    with _request(url, timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_sha256(release, asset, timeout=TIMEOUT):
    """sha256 из соседнего asset-а ``<имя>.sha256`` (None, если его нет)."""
    if not asset:
        return None
    # GitHub now exposes the digest directly on release assets.  Prefer it to
    # an extra sidecar request, while retaining sidecar compatibility with old
    # releases and private mirrors.
    digest = str(asset.get("digest") or "")
    direct = re.search(r"\b([0-9a-fA-F]{64})\b", digest)
    if direct:
        return direct.group(1).lower()
    wanted = ((asset.get("name") or "") + ".sha256").lower()
    for other in (release or {}).get("assets") or []:
        if (other.get("name") or "").lower() == wanted:
            try:
                with _request(other.get("browser_download_url"), timeout) as resp:
                    text = resp.read(8192).decode("utf-8", "replace")
            except Exception:
                return None
            match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
            return match.group(1).lower() if match else None
    return None


def check(current_version, repo_slug=None, timeout=TIMEOUT):
    """Возвращает dict обновления или None (обновлений нет / не новее).

    Исключения пробрасываются наружу — вызывающий код обязан их поймать.
    """
    release = fetch_latest(repo_slug, timeout)
    tag = release.get("tag_name") or release.get("name") or ""
    if not is_newer(tag, current_version):
        return None
    asset = select_asset(release)
    return {
        "version": tag.lstrip("vV") or tag,
        "tag": tag,
        "url": release.get("html_url") or "",
        "notes": (release.get("body") or "").strip()[:4000],
        "asset": asset,
        "sha256": fetch_sha256(release, asset, timeout) if asset else None,
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download_asset(asset, dest_dir=None, progress=None, timeout=DOWNLOAD_TIMEOUT):
    """Скачивает asset во временный каталог. Возвращает (путь, размер)."""
    name = asset.get("name") or "MountFile-update"
    # Treat both path syntaxes as untrusted even when downloaded on POSIX.
    if (
        not isinstance(name, str)
        or name in (".", "..")
        or re.search(r'[\\/:\x00-\x1f"<>|]', name)
    ):
        raise ValueError("некорректное имя файла обновления")
    dest_dir = dest_dir or tempfile.mkdtemp(prefix="mountfile-update-")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    total = int(asset.get("size") or 0)
    if total < 0 or total > MAX_DOWNLOAD_SIZE:
        raise OSError("обновление слишком большое: %d байт" % total)
    fd, partial = tempfile.mkstemp(
        prefix=".mountfile-download-", suffix=".part", dir=dest_dir
    )
    got = 0
    try:
        with (
            os.fdopen(fd, "wb") as fh,
            _request(asset.get("browser_download_url"), timeout) as resp,
        ):
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD_SIZE:
                    raise OSError("обновление превышает лимит размера")
                fh.write(chunk)
                if progress:
                    # -1 = размер неизвестен (нет Content-Length): индикатор
                    # переходит в режим «занят» вместо процентов
                    progress(min(99, int(got * 100 / total)) if total else -1)
            fh.flush()
            os.fsync(fh.fileno())
        if total and got != total:
            raise OSError("скачано %d из %d байт" % (got, total))
        os.replace(partial, dest)
        if progress:
            progress(100)
        return dest, got
    except Exception:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- установка
# «%DATE%»/«%TIME%» в шаблоне - переменные cmd.exe, поэтому никакого
# %-форматирования на этих строках быть не должно (иначе Python падает
# с "not enough arguments for format string"). Подстановка - replace().
#: Сколько раз (примерно секунд: между проверками стоит ``ping``) .cmd
#: ждёт выхода процесса перед подменой файла. Ожидание было бесконечным
#: ``goto wait`` без счётчика: если процесс не завершался или ``tasklist``
#: его не находил, окно ``findstr`` висело вечно, и обновление не
#: заканчивалось никогда ("при обновлении программы висит").
WINDOWS_WAIT_TRIES = 60
#: Сколько раз .cmd пытается заменить файл, пока он ещё занят процессом.
WINDOWS_MOVE_TRIES = 10

_WINDOWS_SWAP_TEMPLATE = """@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
set "LOG=__LOG__"
set /a TRIES=0
:wait
tasklist /FI "PID eq __PID__" 2>nul | findstr /I "__PID__" >nul
if errorlevel 1 goto replace
set /a TRIES+=1
if %TRIES% GEQ __WAIT_TRIES__ goto forced
ping -n 2 127.0.0.1 >nul
goto wait
:forced
echo %DATE% %TIME% PID __PID__ still alive after __WAIT_TRIES__ checks - replacing anyway >> "%LOG%"
:replace
echo %DATE% %TIME% installing >> "%LOG%"
set /a MOVES=0
:trymove
move /Y "__NEW__" "__TARGET__" >> "%LOG%" 2>&1
if not errorlevel 1 goto started
set /a MOVES+=1
if %MOVES% GEQ __MOVE_TRIES__ goto failed
ping -n 2 127.0.0.1 >nul
goto trymove
:started
echo %DATE% %TIME% restart >> "%LOG%"
start "" "__TARGET__"
goto done
:failed
echo %DATE% %TIME% update FAILED, new file left at "__NEW__" >> "%LOG%"
:done
del "%~f0"
"""


def _windows_swap_script(new_file, target, pid, log_file):
    """Пишет .cmd, который дождётся выхода процесса и подменит exe."""

    def batch_path(value):
        value = os.fspath(value)
        if any(char in value for char in ('"', "\r", "\n", "\x00")):
            raise ValueError("некорректный путь для скрипта обновления")
        return value.replace("%", "%%")

    replacements = {
        "__LOG__": batch_path(log_file),
        "__PID__": str(int(pid)),
        "__WAIT_TRIES__": str(WINDOWS_WAIT_TRIES),
        "__MOVE_TRIES__": str(WINDOWS_MOVE_TRIES),
        "__NEW__": batch_path(new_file),
        "__TARGET__": batch_path(target),
    }
    script = re.sub(
        r"__(?:LOG|PID|WAIT_TRIES|MOVE_TRIES|NEW|TARGET)__",
        lambda match: replacements[match.group()],
        _WINDOWS_SWAP_TEMPLATE,
    )
    fd, path = tempfile.mkstemp(prefix="mountfile-update-%d-" % pid, suffix=".cmd")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(script)
    return path


def install_and_restart(new_file):
    """Подменяет текущий файл программы обновлённым и перезапускает её.

    На Windows возвращает строку 'restart' (перезапуск сделает .cmd),
    на POSIX не возвращает управление (os.execv).
    """
    if not is_frozen():
        raise RuntimeError("режим исходников: самозамена файлов отключена")
    target = os.path.abspath(sys.executable)
    if not os.path.exists(target):
        raise RuntimeError("не найден исполняемый файл: %s" % target)

    if os.name == "nt":
        log_file = os.path.join(tempfile.gettempdir(), "mountfile-update.log")
        script = _windows_swap_script(new_file, target, os.getpid(), log_file)
        # CREATE_NO_WINDOW (0x08000000) — документированный «без окна консоли»
        # флаг для консольного дочернего процесса. Раньше здесь стоял
        # DETACHED_PROCESS, и на живой машине был виден висящий консольный
        # скрипт подмены (окно с заголовком вида ``findstr /i "<pid>"`` —
        # это и есть ожидание PID из шаблона). DETACHED_PROCESS и
        # CREATE_NO_WINDOW несовместимы (второй игнорируется вместе с
        # первым), поэтому выбран CREATE_NO_WINDOW.
        no_window = 0x08000000  # CREATE_NO_WINDOW
        new_group = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            ["cmd", "/c", script],
            close_fds=True,
            creationflags=no_window | new_group,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "restart"

    mode = os.stat(target).st_mode
    fd, staged = tempfile.mkstemp(
        prefix=".mountfile-update-", dir=os.path.dirname(target)
    )
    os.close(fd)
    try:
        shutil.copy2(new_file, staged)
        os.chmod(staged, mode | 0o111)
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    os.execv(target, [target] + sys.argv[1:])
    return "restart"  # pragma: no cover - execv не возвращается


def git_upgrade(root):
    """``git pull --ff-only`` в чекауте (только если разрешено явно)."""
    if not _flag("MOUNTFILE_GIT_PULL") or not os.path.exists(
        os.path.join(root, ".git")
    ):
        return None
    proc = subprocess.run(
        ["git", "-C", root, "pull", "--ff-only"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode:
        raise RuntimeError(
            "git pull не удался: %s"
            % _short(proc.stderr or proc.stdout or proc.returncode)
        )
    return (proc.stdout or proc.stderr or "").strip()[:1000]


# ------------------------------------------------------------------- потоки
def app_version():
    from . import __version__

    return __version__


class Updater(QThread):
    """Фоновая проверка обновлений при старте (UI не блокируется)."""

    status = pyqtSignal(str)  # сообщение в лог окна
    available = pyqtSignal(dict)  # найдено обновление
    done = pyqtSignal(bool)  # проверка завершена

    def __init__(self, parent=None, current_version=None):
        try:
            super().__init__(parent)
        except TypeError:  # pragma: no cover - заглушка без Qt
            super().__init__()
        self.current_version = current_version or app_version()

    def run(self):  # pragma: no cover - требует Qt-цикла
        # StartupUpdater is started only after the GUI startup gate has
        # approved network access. Direct callers of this worker are explicit
        # manual checks and must remain functional even in offline-default
        # mode.
        try:
            info = check(self.current_version)
        except Exception as exc:
            self.status.emit(
                "Проверка обновлений не удалась (%s) — продолжаем работу" % _short(exc)
            )
            self.done.emit(False)
            return
        if not info:
            self.status.emit("Обновлений нет (установлено v%s)" % self.current_version)
            self.done.emit(False)
            return
        if info.get("asset"):
            self.status.emit(
                "Доступна версия v%s (у вас v%s): %s"
                % (info["version"], self.current_version, info["asset"].get("name"))
            )
        else:
            self.status.emit(
                "Доступна версия v%s (у вас v%s), сборки для этой платформы нет в релизе"
                % (info["version"], self.current_version)
            )
        self.available.emit(info)
        self.done.emit(True)


class Installer(QThread):
    """Скачивание, проверка и установка обновления.

    Сигналы для строки состояния GUI: ``stage`` — текст текущего этапа
    (скачивание → контрольная сумма → установка), ``progress`` — ход
    скачивания в процентах (0–100 или ``-1``, когда размер файла
    неизвестен — тогда индикатор работает в режиме «занят»).
    """

    status = pyqtSignal(str)
    progress = pyqtSignal(int)
    stage = pyqtSignal(str)
    failed = pyqtSignal(str)
    restarting = pyqtSignal()
    installed = pyqtSignal()

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info or {}

    def run(self):  # pragma: no cover - требует Qt-цикла
        try:
            if not is_frozen():
                # Source installs do not use a platform binary or its digest.
                # Never download hundreds of MB just to open the release page.
                self.stage.emit("Обновление исходников…")
                for root in (
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    os.getcwd(),
                ):
                    note = git_upgrade(root)
                    if note is not None:
                        self.status.emit(note)
                        return
                if self.info.get("url"):
                    webbrowser.open(self.info["url"])
                    self.status.emit("Запуск из исходников: файлы не меняются")
                    return
                raise RuntimeError("режим исходников: нет ссылки на релиз")
            asset = self.info.get("asset")
            if not asset:
                if self.info.get("url"):
                    self.status.emit("Открываю страницу релиза: %s" % self.info["url"])
                    webbrowser.open(self.info["url"])
                    return
                raise RuntimeError("в релизе нет сборки для этой платформы")
            wanted = self.info.get("sha256")
            if not isinstance(wanted, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", wanted
            ):
                raise RuntimeError(
                    "отсутствует или некорректна доверенная SHA256-сумма"
                )
            self.stage.emit("Скачивание обновления…")
            self.status.emit("Скачивание %s…" % asset.get("name"))
            path, size = download_asset(asset, progress=self.progress.emit)
            self.stage.emit("Проверка контрольной суммы…")
            actual = sha256_file(path)
            if actual != wanted.lower():
                raise RuntimeError("контрольная сумма не совпала (файл повреждён)")
            self.status.emit("Контрольная сумма совпала")
            self.status.emit("Скачано %d байт" % size)

            self.progress.emit(100)
            self.stage.emit("Установка обновления…")
            self.status.emit("Установка v%s и перезапуск…" % self.info.get("version"))
            self.restarting.emit()
            install_and_restart(path)
            # На Windows install_and_restart() только запускает .cmd, который
            # ждёт выхода нашего процесса: окно должно закрыться именно
            # после этого сигнала, иначе файл останется не подменённым.
            self.installed.emit()
        except Exception as exc:
            self.failed.emit(_short(exc))


class StartupUpdater(Updater):
    """Проверка обновлений при старте + автоустановка, если разрешено.

    Отдельный класс нужен, чтобы GUI-код подключал один набор сигналов
    вместо трёх: подробности установки остаются внутри модуля.
    """

    installing = pyqtSignal(dict)
    restarting = pyqtSignal()
    installed = pyqtSignal()
    # Проброс сигналов установщика наверх: GUI подключается только к
    # StartupUpdater. Раньше сигнала progress не было, и подключение
    # ``self._installer.progress.connect(self.progress)`` падало с
    # AttributeError — индикатор хода установки не работал совсем.
    progress = pyqtSignal(int)
    stage = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None, current_version=None, installer_factory=None):
        super().__init__(parent, current_version)
        self._installer_factory = installer_factory or (
            lambda info, p: Installer(info, p)
        )
        self._installer = None
        self.available.connect(self._on_available)

    def _on_available(self, info):
        if auto_install() and is_frozen():
            self.installing.emit(info)
            self.install(info, self.parent())

    def install(self, info, parent=None):
        if self._installer is not None and self._installer.isRunning():
            return self._installer
        self._installer = self._installer_factory(info, parent or self.parent())
        self._installer.status.connect(self.status)
        self._installer.progress.connect(self.progress)
        self._installer.stage.connect(self.stage)
        self._installer.installed.connect(self.installed)
        self._installer.failed.connect(self.failed)
        self._installer.failed.connect(
            lambda msg: self.status.emit("Обновление не установлено: %s" % msg)
        )
        self._installer.restarting.connect(self.restarting)
        self._installer.start()
        return self._installer

    def wait_installer(self, timeout=3000):
        """Дождаться потока установки перед выходом из приложения."""
        installer = self._installer
        if installer is not None and installer.isRunning():
            return installer.wait(timeout)
        return True
