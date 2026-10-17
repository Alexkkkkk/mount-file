# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Автообновление Mount File из GitHub Releases.

Как это работает
----------------
Проверка запускается при *каждом* старте приложения в фоновом потоке
(QThread), поэтому окно не подвисает, а отсутствие сети или битый URL
вообще не мешают работе: в лог падает одна строка и всё.

Источник обновлений — GitHub Releases репозитория ``Alexkkkkk/mount-file``
(переопределяется переменной окружения ``MOUNTFILE_UPDATE_REPO``).
Версии сравниваются по числам (``1.10.0`` > ``1.9.9``), а не как строки.

Что происходит, когда найдена новая версия:

* автономная сборка (PyInstaller, ``sys.frozen``) — скачиваем подходящий
  для платформы asset (.exe / бинарник linux), сверяем sha256, если релиз
  его публикует, и подменяем файл:
    - Windows — запускается .cmd, который ждёт выхода процесса (занятый
      exe перезаписать нельзя), затем ``move`` + ``start``;
    - Linux/macOS — подмена файла на месте и ``os.execv``;
* запуск из исходников — файлы не трогаем: показываем ссылку на релиз
  (и, если это git-чекаут и ``MOUNTFILE_GIT_PULL=1``, делаем ``git pull``).

Переменные окружения
--------------------
``MOUNTFILE_UPDATE_REPO=owner/repo``  другой репозиторий с релизами
``MOUNTFILE_NO_UPDATE=1``             выключить проверку обновлений
``MOUNTFILE_AUTO_UPDATE=1``           ставить без вопроса и перезапускаться
``MOUNTFILE_GIT_PULL=1``              в режиме исходников разрешить git pull
``MOUNTFILE_UPDATE_URL``              прямой URL на JSON последнего релиза
                                      (для внутреннего/корпоративного сервера)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
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
        import urllib.request

        return urllib.request.urlopen(url, timeout=timeout).read()
    return _olma_dl(url, timeout=timeout, max_size=max_size)


def _verify_sha256(data, expected):
    if _olma_sha is None:
        return True
    return _olma_sha(data, expected)


def _atomic_write(path, data):
    if _OlmaTx is None:
        with open(path, "wb") as f:
            f.write(data)
        return
    with _OlmaTx(path) as tx:
        tx.write_all(data)


# --- /MOUNTFILE_FIX --------------------------------------------------------
try:  # Qt нужен только фоновым классам внизу файла
    from .qt_compat import QThread, pyqtSignal

    HAVE_QT = True
except Exception:  # pragma: no cover - проверки/тесты работают и без Qt
    HAVE_QT = False

    class _NoQtSignal:
        """Заглушка сигнала: позволяет импортировать модуль без PyQt."""

        def __init__(self, *args, **kwargs):
            # Сохраняем подключённые слоты и вызываем их при emit(),
            # чтобы фоновые классы работали по-настоящему и без Qt
            # (так делают тесты CI, где PyQt не установлен).
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *args, **kwargs):
            for slot in list(self._slots):
                slot(*args, **kwargs)

    def pyqtSignal(*args, **kwargs):
        return _NoQtSignal()

    QThread = object


DEFAULT_REPO = "Alexkkkkk/mount-file"
USER_AGENT = "MountFile-Updater/1.0 (+https://github.com/Alexkkkkk/mount-file)"
TIMEOUT = 8  # секунд: проверка не должна задерживать старт
DOWNLOAD_TIMEOUT = 90
CHUNK = 64 * 1024

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
    return _flag("MOUNTFILE_NO_UPDATE")


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
    r, l = parse_version(remote), parse_version(local)
    size = max(len(r), len(l))
    r = r + (0,) * (size - len(r))
    l = l + (0,) * (size - len(l))
    return r > l


def _short(exc):
    text = str(exc).strip() or exc.__class__.__name__
    return text.replace("\n", " ")[:300]


def _on_windows7():
    try:
        return sys.platform.startswith("win") and sys.getwindowsversion().major == 6
    except Exception:  # pragma: no cover - не Windows
        return False


def _asset_score(name):
    """Чем больше очков, тем лучше сборка подходит этой системе."""
    low = name.lower()
    if low.endswith((".sha256", ".txt", ".zip", ".json")):
        return -1
    if sys.platform.startswith("win"):
        if not low.endswith(".exe"):
            return -1
        score = 100 if low == "mountfile.exe" else 60
        win7_asset = "win7" in low or "win-7" in low or "windows7" in low
        score += 40 if win7_asset == _on_windows7() else 0
        return score
    if sys.platform == "darwin":
        return 80 if any(k in low for k in ("macos", "darwin", "osx", "mac")) else -1
    if sys.platform.startswith("linux"):
        if any(k in low for k in ("win", ".exe")):
            return -1
        return (
            80 if any(k in low for k in ("linux", "x86_64", "x86-64", "amd64")) else 20
        )
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
    name = os.path.basename(asset.get("name") or "MountFile-update")
    dest_dir = dest_dir or tempfile.mkdtemp(prefix="mountfile-update-")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    total = int(asset.get("size") or 0)
    got = 0
    with _request(asset.get("browser_download_url"), timeout) as resp, open(
        dest, "wb"
    ) as fh:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(min(99, int(got * 100 / total)))
    if total and got != total:
        raise OSError("скачано %d из %d байт" % (got, total))
    if progress:
        progress(100)
    return dest, got


# --------------------------------------------------------------- установка
# «%DATE%»/«%TIME%» в шаблоне - переменные cmd.exe, поэтому никакого
# %-форматирования на этих строках быть не должно (иначе Python падает
# с "not enough arguments for format string"). Подстановка - replace().
_WINDOWS_SWAP_TEMPLATE = """@echo off
setlocal
set "LOG=__LOG__"
:wait
tasklist /FI "PID eq __PID__" | findstr /I "__PID__" >nul
if not errorlevel 1 (
  ping -n 2 127.0.0.1 >nul
  goto wait
)
echo %DATE% %TIME% installing > "%LOG%"
move /Y "__NEW__" "__TARGET__" >> "%LOG%" 2>&1
if errorlevel 1 goto failed
echo %DATE% %TIME% restart >> "%LOG%"
start "" "__TARGET__"
goto done
:failed
echo %DATE% %TIME% update FAILED, new file left at __NEW__ >> "%LOG%"
echo Update failed - see "%LOG%"
pause
:done
del "%~f0"
"""


def _windows_swap_script(new_file, target, pid, log_file):
    """Пишет .cmd, который дождётся выхода процесса и подменит exe."""
    path = os.path.join(tempfile.gettempdir(), "mountfile-update-%d.cmd" % pid)
    script = (
        _WINDOWS_SWAP_TEMPLATE.replace("__LOG__", log_file)
        .replace("__PID__", str(pid))
        .replace("__NEW__", new_file)
        .replace("__TARGET__", target)
    )
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
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
        detached = 0x00000008  # DETACHED_PROCESS
        new_group = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            ["cmd", "/c", script],
            close_fds=True,
            creationflags=detached | new_group,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "restart"

    mode = os.stat(target).st_mode
    staged = target + ".new"
    shutil.copy2(new_file, staged)
    os.chmod(staged, mode | 0o111)
    os.replace(staged, target)
    os.execv(target, [target] + sys.argv[1:])
    return "restart"  # pragma: no cover - execv не возвращается


def git_upgrade(root):
    """``git pull --ff-only`` в чекауте (только если разрешено явно)."""
    if not _flag("MOUNTFILE_GIT_PULL") or not os.path.isdir(os.path.join(root, ".git")):
        return None
    proc = subprocess.run(
        ["git", "-C", root, "pull", "--ff-only"],
        capture_output=True,
        text=True,
        timeout=120,
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
        if disabled():
            self.done.emit(False)
            return
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
    """Скачивание, проверка и установка обновления."""

    status = pyqtSignal(str)
    progress = pyqtSignal(int)
    failed = pyqtSignal(str)
    restarting = pyqtSignal()
    installed = pyqtSignal()

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info or {}

    def run(self):  # pragma: no cover - требует Qt-цикла
        try:
            asset = self.info.get("asset")
            if not asset:
                if self.info.get("url"):
                    self.status.emit("Открываю страницу релиза: %s" % self.info["url"])
                    webbrowser.open(self.info["url"])
                    return
                raise RuntimeError("в релизе нет сборки для этой платформы")
            self.status.emit("Скачивание %s…" % asset.get("name"))
            path, size = download_asset(asset, progress=self.progress.emit)
            wanted = self.info.get("sha256")
            if wanted:
                actual = sha256_file(path)
                if actual != wanted:
                    raise RuntimeError("контрольная сумма не совпала (файл повреждён)")
                self.status.emit("Контрольная сумма совпала")
            self.status.emit("Скачано %d байт" % size)

            if not is_frozen():
                note = None
                if self.info.get("url"):
                    for root in (
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        os.getcwd(),
                    ):
                        try:
                            note = git_upgrade(root)
                        except Exception as exc:
                            note = "git pull не удался: %s" % _short(exc)
                        if note is not None:
                            break
                if note:
                    self.status.emit(note)
                elif self.info.get("url"):
                    webbrowser.open(self.info["url"])
                self.status.emit("Запуск из исходников: файлы не меняются")
                return

            self.progress.emit(100)
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
        if self._installer is not None:
            return self._installer
        self._installer = self._installer_factory(info, parent or self.parent())
        self._installer.status.connect(self.status)
        self._installer.progress.connect(self.progress)
        self._installer.installed.connect(self.installed)
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
