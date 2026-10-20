# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""«Центр обновлений» Mount File — окно автообновления с анимацией.

Что внутри
----------
* :class:`GradientBar` — полоса прогресса с анимированным градиентом и
  режимом «занят» (когда сервер не отдал ``Content-Length``).
* :class:`StepIndicator` — три шага: скачивание → проверка → установка.
* :class:`DownloadWorker` — поток загрузки: файл пишется на диск потоком
  (никакого ``data += chunk`` в памяти), sha256 считается инкрементально,
  наружу идут проценты, скорость и ETA. Загрузку можно отменить.
* :class:`UpdateDialog` — само окно. «Что нового» и контрольная сумма
  берутся из GitHub Releases API через :mod:`mountfile.updater`, поэтому
  они всегда настоящие, а не захардкоженные в коде.
* Установка на Windows — через ``.cmd``-подмену, который ждёт выхода
  процесса, меняет exe и запускает его заново (см.
  :func:`_write_windows_swap_bat`). Ограниченный по времени вариант уже
  есть в :mod:`mountfile.updater` — он и используется, если модуль
  доступен; иначе работает локальная копия из этого файла.

Что исправлено по сравнению с первой версией окна
--------------------------------------------------
1. ``pyqtSignal(bool, str)`` больше не пытается пронести ``bytes``:
   файл пишется на диск, а сигнал несёт путь к нему.
2. sha256 считается по ходу загрузки — обновление не держится в памяти.
3. Убрана склейка ``.from mountfile.update_dialog import UpdateDialog``,
   из-за которой файл вообще не компилировался.
4. Markdown «Что нового» проходит через :func:`dedent_markdown`, иначе
   отступы превращали весь список в блок кода.
5. Добавлены безопасная самозамена файла (Windows) и отмена загрузки.

Запуск демо-окна (без сети, для проверки внешнего вида)::

    QT_QPA_PLATFORM=offscreen python -m mountfile.update_dialog
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request

# Единственный источник Qt-имён — совместимый слой (PyQt6 / PyQt5 + Win7).
# QFrame и QTextBrowser в qt_compat не перечислены, поэтому берём их из
# того же модуля QtWidgets, чтобы не менять инвариант «оба биндинга
# экспортируют один и тот же список имён».
from . import qt_compat
from .qt_compat import (  # noqa: F401 - QColor/QBrush нужны для отрисовки
    QBrush,
    QColor,
    QDialog,
    QFont,
    QHBoxLayout,
    QLabel,
    QPainter,
    QPushButton,
    QThread,
    QTimer,
    QVBoxLayout,
    Qt,
    pyqtSignal,
)

QtCore = qt_compat.QtCore
QtGui = qt_compat.QtGui
QApplication = qt_compat.QApplication
QFrame = qt_compat.QtWidgets.QFrame
QTextBrowser = qt_compat.QtWidgets.QTextBrowser

USER_AGENT = "MountFile-Updater/2.0 (+https://github.com/Alexkkkkk/mount-file)"
CHUNK = 64 * 1024
CONNECT_TIMEOUT = 30.0
#: Как часто пересчитывать скорость и ETA (секунды). В тестах выставляется
#: в 0, чтобы получать обновление на каждом блоке.
SPEED_TICK_SECONDS = 0.25
#: Ограничение ожидания выхода процесса в .cmd-подмене (примерно секунды:
#: между проверками стоит ``ping``) и число попыток замены занятого файла.
WINDOWS_WAIT_TRIES = 60
WINDOWS_MOVE_TRIES = 10

# ------------------------------------------------------------------ палитра
BG_COLOR = "#12141a"
PANEL_COLOR = "#1b1e26"
BORDER_COLOR = "#2a2f3a"
TEXT_COLOR = "#e8ecf3"
MUTED_COLOR = "#8b93a7"
ACCENT_COLOR = "#00d2ff"
TRACK_COLOR = "#232833"
GRADIENT_START = "#00d2ff"
GRADIENT_MID = "#2f8bff"
GRADIENT_END = "#7b5cff"
OK_COLOR = "#2ee6a8"
ERROR_COLOR = "#ff5470"
WARN_COLOR = "#ffc95c"


# ------------------------------------------------------------------ Qt enums
def _member(owner, holder, member):
    """``Qt6``: ``owner.holder.member``; ``Qt5``: ``owner.member``.

    Qt5 держит перечисления прямо в классе (``Qt.AlignCenter``), Qt6 убрал
    их в namespace (``Qt.AlignmentFlag.AlignCenter``). Одна и та же строка
    работает для обоих биндингов.
    """
    holder_obj = getattr(owner, holder, None)
    if holder_obj is not None:
        return getattr(holder_obj, member)
    return getattr(owner, member)


def _cursor_pointing_hand():
    return _member(Qt, "CursorShape", "PointingHandCursor")


#: Qt требует именно enum (Qt6), а не int: ``setAlignment(w, int)`` падает
#: с TypeError, поэтому шимы ниже возвращают сам enum, без приведения.
def _align_center():
    return _member(Qt, "AlignmentFlag", "AlignCenter")


def _align_right():
    return _member(Qt, "AlignmentFlag", "AlignRight")


def _no_pen():
    return _member(Qt, "PenStyle", "NoPen")


def _antialiasing():
    return _member(QPainter, "RenderHint", "Antialiasing")


def _easing_out_cubic():
    return _member(QtCore.QEasingCurve, "Type", "OutCubic")


def _dialog_accepted():
    return _member(QDialog, "DialogCode", "Accepted")


def _window_context_help_hint():
    return _member(Qt, "WindowType", "WindowContextHelpButtonHint")


# --------------------------------------------------------------- форматирование
def human_size(num_bytes):
    """``1536`` -> ``'1.5 КБ'`` (шаг 1024, один знак после точки)."""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return "%d %s" % (int(value), unit)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f ТБ" % value


def format_speed(bytes_per_second):
    """``1.5*1024*1024`` -> ``'1.5 МБ/с'``."""
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return "%s/с" % human_size(bytes_per_second)


def format_eta(seconds):
    """``75`` -> ``'1:15'``; ``None``/``<=0`` -> ``'—'``."""
    if not seconds or seconds <= 0:
        return "—"
    seconds = int(seconds) + 1
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def dedent_markdown(text):
    """Убрать общий отступ у Markdown.

    Без этого текст из тройных кавычек с отступом рендерится как блок кода:
    четыре пробела в Markdown означают ``<pre>``. Пустые строки при этом
    обнуляют ``textwrap.dedent``, поэтому сначала нормализуем их.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line if line.strip() else "" for line in raw.split("\n")]
    dedented = textwrap.dedent("\n".join(lines))
    return dedented.strip("\n")


def looks_like_sha256(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", (value or "").strip()))


def _app_version():
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover - на всякий случай
        return "0.0.0"


def check_for_update(current_version=None, timeout=None):
    """Обновление из GitHub Releases API (или ``None``, если его нет).

    Обёртка над :func:`mountfile.updater.check`: даёт окну словарь с
    ``version``, ``notes`` (changelog релиза), ``asset``, ``sha256`` и
    ``url`` страницы релиза.
    """
    from . import updater

    args = [current_version or updater.app_version()]
    if timeout is None:
        return updater.check(*args)
    return updater.check(*args, timeout=timeout)


# ------------------------------------------------------------------- полоса
class GradientBar(QtGui.QWidget if hasattr(QtGui, "QWidget") else qt_compat.QWidget):
    """Полоса прогресса с живым градиентом (и режимом «занят»).

    Обычный ``QProgressBar`` не умеет ни переливающийся градиент, ни режим
    неопределённости с «бегущей» волной, поэтому рисуем сами. Ширина
    градиента больше самой полосы, и он «едет» по фазе анимации — это и
    даёт переливание.
    """

    PHASE_STEP = 0.012
    FRAME_MS = 40

    def __init__(self, parent=None, height=14):
        super().__init__(parent)
        self._value = 0
        self._busy = False
        self._phase = 0.0
        self.setFixedHeight(height)
        self.setMinimumWidth(120)
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

    # ---------------------------------------------------------- публичное
    def value(self):
        return self._value

    def setValue(self, value):  # noqa: N802 - имя как у QProgressBar
        self._value = max(0, min(100, int(value)))
        self.update()

    def set_indeterminate(self, busy=True):
        """Режим «занят»: размер неизвестен, волна идёт по всей полосе."""
        self._busy = bool(busy)
        if self._busy:
            self._value = 0
        self.update()

    def is_indeterminate(self):
        return self._busy

    def set_text(self, text):
        self._text = text or ""
        self.update()

    # ------------------------------------------------------------- анимация
    def _tick(self):
        self._phase = (self._phase + self.PHASE_STEP) % 1.0
        self.update()

    def showEvent(self, event):  # noqa: N802
        if not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------- отрисовка
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(_antialiasing(), True)
        painter.setPen(_no_pen())
        rect = QtCore.QRectF(0, 0, float(self.width()), float(self.height()))
        radius = self.height() / 2.0

        painter.setBrush(QColor(TRACK_COLOR))
        painter.drawRoundedRect(rect, radius, radius)

        fraction = 1.0 if self._busy else self._value / 100.0
        fill_w = self.width() * fraction
        if self._busy:
            fill_w = self.width()
        if fill_w > 1.0:
            span = max(float(self.width()), 140.0)
            x0 = -span + (self.width() + span) * self._phase
            gradient = QtGui.QLinearGradient(x0, 0.0, x0 + span, 0.0)
            gradient.setColorAt(0.0, QColor(GRADIENT_START))
            gradient.setColorAt(0.45, QColor(GRADIENT_MID))
            gradient.setColorAt(1.0, QColor(GRADIENT_END))
            if self._busy:
                gradient.setColorAt(0.25, QColor(self._dim(GRADIENT_START)))
            painter.save()
            painter.setClipRect(
                QtCore.QRectF(0, 0, fill_w, float(self.height()))
            )
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(rect, radius, radius)
            painter.restore()

        text = self._bar_text()
        if text:
            painter.setPen(QColor(TEXT_COLOR))
            font = painter.font()
            font.setPointSizeF(max(7.0, self.height() / 1.45))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, _align_center(), text)

    def _bar_text(self):
        if self._busy:
            return "…"
        return "%d%%" % self._value if self._value else ""

    @staticmethod
    def _dim(hex_color):
        color = QColor(hex_color)
        color.setAlpha(90)
        return color.name(QColor.NameFormat.HexArgb) if hasattr(
            QColor, "NameFormat"
        ) else color.name()


# --------------------------------------------------------------------- шаги
class StepIndicator(qt_compat.QWidget):
    """Три состояния обновления: скачивание → проверка → установка."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"

    def __init__(self, steps, parent=None):
        super().__init__(parent)
        self._steps = list(steps)
        self._state = [self.PENDING] * len(self._steps)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._labels = []
        for index, name in enumerate(self._steps):
            if index:
                arrow = QLabel("→")
                arrow.setStyleSheet("color: %s;" % BORDER_COLOR)
                layout.addWidget(arrow)
            label = QLabel()
            label.setTextFormat(Qt.TextFormat.PlainText if hasattr(Qt, "TextFormat") else 0)
            self._labels.append(label)
            layout.addWidget(label)
        layout.addStretch(1)
        self._refresh()

    # ---------------------------------------------------------- публичное
    def activate(self, index):
        """Шаг ``index`` становится текущим, предыдущие — завершёнными."""
        for i in range(len(self._steps)):
            if i < index:
                self._state[i] = self.DONE
            elif i == index:
                self._state[i] = self.ACTIVE
            else:
                self._state[i] = self.PENDING
        self._refresh()

    def complete(self, index):
        self._state[index] = self.DONE
        self._refresh()

    def states(self):
        return list(self._state)

    def reset(self):
        self._state = [self.PENDING] * len(self._steps)
        self._refresh()

    # ----------------------------------------------------------- отрисовка
    def _mark(self, index):
        state = self._state[index]
        if state == self.DONE:
            return "✓"
        if state == self.ACTIVE:
            return "●"
        return "○"

    def _refresh(self):
        for index, label in enumerate(self._labels):
            state = self._state[index]
            if state == self.DONE:
                color, weight = OK_COLOR, "normal"
            elif state == self.ACTIVE:
                color, weight = ACCENT_COLOR, "bold"
            else:
                color, weight = MUTED_COLOR, "normal"
            label.setText("%s %s" % (self._mark(index), self._steps[index]))
            label.setStyleSheet(
                "color: %s; font-size: 12px; font-weight: %s;" % (color, weight)
            )


# ----------------------------------------------------------------- загрузчик
class DownloadWorker(QThread):
    """Поток скачивания: диск + sha256 по ходу + скорость и ETA.

    Сигналы::

        progress(percent, done_bytes, speed_bps, eta_seconds)
        stage(str)     текст текущего этапа
        done(str)      путь к скачанному файлу (проверенному)
        failed(str)    текст ошибки
        cancelled()    загрузка отменена пользователем

    ``percent == -1`` означает «размер неизвестен» — полоса уходит в режим
    «занят». Файл никогда не собирается в памяти целиком.
    """

    progress = pyqtSignal(int, float, float, float)
    stage = pyqtSignal(str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, url, sha256=None, size=0, asset_name=None, dest_dir=None,
                 parent=None, timeout=CONNECT_TIMEOUT, chunk=CHUNK,
                 tick_seconds=SPEED_TICK_SECONDS):
        super().__init__(parent)
        self.url = url or ""
        self.sha256 = (sha256 or "").strip().lower() or None
        self.size = int(size or 0)
        self.asset_name = asset_name
        self.dest_dir = dest_dir
        self.timeout = timeout
        self.chunk = chunk
        self.tick_seconds = tick_seconds
        self._cancel = False

    # ---------------------------------------------------------- управление
    def cancel(self):
        """Попросить поток остановиться (проверяется на каждом блоке)."""
        self._cancel = True
        try:
            self.requestInterruption()
        except Exception:  # pragma: no cover - заглушка без Qt
            pass

    def is_cancelled(self):
        return self._cancel

    # ------------------------------------------------------------ загрузка
    def run(self):  # pragma: no cover - покрыто тестами через прямой вызов
        try:
            if not self.url:
                self.failed.emit("не указан адрес сборки")
                return
            dest_dir = self.dest_dir or tempfile.mkdtemp(prefix="mountfile-update-")
            os.makedirs(dest_dir, exist_ok=True)
            name = os.path.basename(
                self.asset_name or self.url.split("?")[0].rstrip("/").split("/")[-1]
            ) or "MountFile-update"
            dest = os.path.join(dest_dir, name)

            self.stage.emit("Скачивание обновления…")
            request = urllib.request.Request(
                self.url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
            )

            digest = hashlib.sha256()
            got = 0
            total = self.size
            last_time = time.monotonic()
            last_bytes = 0
            speed = 0.0
            eta = -1.0

            with urllib.request.urlopen(request, timeout=self.timeout) as response, \
                    open(dest, "wb") as handle:
                header_size = response.headers.get("Content-Length")
                if header_size and str(header_size).isdigit():
                    total = int(header_size)
                while True:
                    if self._cancel:
                        handle.close()
                        self._discard(dest)
                        self.cancelled.emit()
                        return
                    block = response.read(self.chunk)
                    if not block:
                        break
                    handle.write(block)
                    digest.update(block)
                    got += len(block)
                    now = time.monotonic()
                    elapsed = now - last_time
                    if elapsed >= self.tick_seconds or last_bytes == 0:
                        speed = (got - last_bytes) / elapsed if elapsed > 0 else 0.0
                        last_time, last_bytes = now, got
                        eta = ((total - got) / speed) if (speed > 0 and total) else -1.0
                        percent = int(got * 100 / total) if total else -1
                        self.progress.emit(
                            min(99, percent), float(got), float(speed), float(eta)
                        )

            if self._cancel:
                self._discard(dest)
                self.cancelled.emit()
                return

            if total and got != total:
                self._discard(dest)
                self.failed.emit("загрузка неполная: %s из %s" % (got, total))
                return

            self.progress.emit(100, float(got), float(speed), 0.0)

            if self.sha256:
                self.stage.emit("Проверка SHA-256…")
                actual = digest.hexdigest()
                if actual != self.sha256:
                    self._discard(dest)
                    self.failed.emit(
                        "контрольная сумма не совпала (файл повреждён или подменён)"
                    )
                    return

            if self._cancel:
                self._discard(dest)
                self.cancelled.emit()
                return

            self.done.emit(dest)
        except Exception as exc:  # сеть, диск, отмена на уровне ОС
            if self._cancel:
                self.cancelled.emit()
                return
            self.failed.emit((str(exc).strip() or exc.__class__.__name__)[:300])

    @staticmethod
    def _discard(path):
        """Удалить недокачанный файл, чтобы он не попал в установку."""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:  # pragma: no cover - файл занят/нет прав
            pass


# ------------------------------------------------------- установка и подмена
def _windows_swap_bat(new_file, target, pid, log_file):
    """Записать ``.cmd``, который дождётся выхода процесса и подменит exe.

    Занятый запущенный exe перезаписать нельзя, поэтому подмена делается
    отдельным процессом: он ждёт исчезновения PID из ``tasklist`` (счётчик
    ограничен — «висения» не будет), меняет файл с повторами, запускает
    новую версию и удаляет себя. ``%DATE%``/``%TIME%`` — переменные cmd.exe,
    поэтому никакого ``%``-форматирования на этих строках быть не должно.
    """
    template = """@echo off
setlocal
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
set /a MOVES=0
:trymove
move /Y "__NEW__" "__TARGET__" >> "%LOG%" 2>&1
if not errorlevel 1 goto started
set /a MOVES+=1
if %MOVES% GEQ __MOVE_TRIES__ goto failed
ping -n 2 127.0.0.1 >nul
goto trymove
:started
echo %DATE% %TIME% update installed, restarting >> "%LOG%"
start "" "__TARGET__"
goto done
:failed
echo %DATE% %TIME% update FAILED, new file left at __NEW__ >> "%LOG%"
:done
del "%~f0"
"""
    path = os.path.join(tempfile.gettempdir(), "mountfile-update-%s.cmd" % pid)
    script = (
        template.replace("__LOG__", log_file)
        .replace("__PID__", str(pid))
        .replace("__WAIT_TRIES__", str(WINDOWS_WAIT_TRIES))
        .replace("__MOVE_TRIES__", str(WINDOWS_MOVE_TRIES))
        .replace("__NEW__", new_file)
        .replace("__TARGET__", target)
    )
    with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(script)
    return path


def install_and_restart(new_file):
    """Подменить текущий файл программы обновлённым и перезапустить её.

    Если доступен :mod:`mountfile.updater`, работа делегируется его
    проверенной реализации (там подмена уже ограничена по времени и идёт
    без окна консоли). Локальная копия ниже — на случай, если модуль
    недоступен: поведение то же.
    """
    try:
        from . import updater as _updater

        return _updater.install_and_restart(new_file)
    except ImportError:  # pragma: no cover - updater.py есть в репозитории
        pass

    if not getattr(sys, "frozen", False):
        raise RuntimeError("запуск из исходников: самозамена файлов отключена")
    target = os.path.abspath(sys.executable)
    if not os.path.exists(target):
        raise RuntimeError("не найден исполняемый файл: %s" % target)

    if os.name == "nt":
        log_file = os.path.join(tempfile.gettempdir(), "mountfile-update.log")
        script = _windows_swap_bat(new_file, target, os.getpid(), log_file)
        no_window = 0x08000000  # CREATE_NO_WINDOW: подмена идёт без консоли
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
    staged = target + ".new"
    shutil.copy2(new_file, staged)
    os.chmod(staged, mode | 0o111)
    os.replace(staged, target)
    os.execv(target, [target] + sys.argv[1:])
    return "restart"  # pragma: no cover - execv не возвращается


# ------------------------------------------------------------------- диалог
class UpdateDialog(QDialog):
    """Красивое окно обновления.

    Совместимо с прежним вызовом::

        UpdateDialog(current_ver, new_ver, changelog, download_url, sha256_hash)

    и с данными GitHub Releases API::

        UpdateDialog.from_release(info, current_version).exec()

    Сигнал :attr:`installed` испускается после успешной подмены файла.
    """

    installed = pyqtSignal()
    snoozed = pyqtSignal()

    def __init__(self, current_ver, new_ver, changelog, download_url,
                 sha256_hash=None, parent=None, *, asset_name=None,
                 release_url=None, size=0, installer=None):
        super().__init__(parent)
        self.current_ver = str(current_ver or "?")
        self.new_ver = str(new_ver or "?")
        self.changelog = dedent_markdown(changelog)
        self.download_url = download_url or ""
        self.sha256_hash = (sha256_hash or "").strip().lower()
        self.asset_name = asset_name
        self.release_url = release_url or ""
        self.size = int(size or 0)
        self._installer_factory = installer or (
            lambda: DownloadWorker(
                self.download_url,
                sha256=self.sha256_hash or None,
                size=self.size,
                asset_name=self.asset_name,
            )
        )

        self.worker = None
        self._cancel_requested = False
        self._download_path = None
        self._busy = False

        self.setWindowTitle("Mount File — Центр обновлений")
        self.setMinimumSize(620, 470)
        try:
            self.setWindowFlags(
                self.windowFlags() & ~_window_context_help_hint()
            )
        except Exception:  # pragma: no cover - экзотический биндинг
            pass
        self._setup_ui()
        self._apply_style()
        self._fade_in()

    # ------------------------------------------------------------- фабрика
    @classmethod
    def from_release(cls, info, current_ver=None, parent=None):
        """Собрать окно из словаря :func:`mountfile.updater.check`."""
        info = info or {}
        asset = info.get("asset") or {}
        return cls(
            current_ver or _app_version(),
            info.get("version") or "?",
            info.get("notes") or "",
            asset.get("browser_download_url") or info.get("url") or "",
            info.get("sha256"),
            parent,
            asset_name=asset.get("name"),
            release_url=info.get("url"),
            size=int(asset.get("size") or 0),
        )

    # ------------------------------------------------------------ интерфейс
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        title = QLabel("Доступно обновление")
        title.setFont(QFont("Segoe UI", 17, QFont.Weight.Bold))
        title.setStyleSheet("color: %s;" % ACCENT_COLOR)

        self.version_label = QLabel(
            "%s  ➜  <b>v%s</b>" % (self.current_ver, self.new_ver)
        )
        self.version_label.setTextFormat(
            Qt.TextFormat.RichText if hasattr(Qt, "TextFormat") else 1
        )
        self.version_label.setStyleSheet(
            "color: %s; font-size: 13px; padding-top: 2px;" % MUTED_COLOR
        )

        header = QVBoxLayout()
        header.setSpacing(3)
        header.addWidget(title)
        header.addWidget(self.version_label)

        if self.release_url:
            self.link_button = QPushButton("Открыть страницу релиза")
            self.link_button.setObjectName("linkButton")
            self.link_button.setCursor(_cursor_pointing_hand())
            self.link_button.clicked.connect(self._open_release_page)
            header.addWidget(self.link_button)
            header.setAlignment(self.link_button, _align_right())

        root.addLayout(header)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine if hasattr(QFrame, "Shape")
                              else QFrame.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: %s; border: none;" % BORDER_COLOR)
        root.addWidget(divider)

        changelog_caption = QLabel("Что нового")
        changelog_caption.setStyleSheet(
            "color: %s; font-weight: bold; font-size: 12px;" % MUTED_COLOR
        )
        root.addWidget(changelog_caption)

        self.changelog_view = QTextBrowser()
        self.changelog_view.setOpenExternalLinks(True)
        if hasattr(self.changelog_view, "setMarkdown"):
            self.changelog_view.setMarkdown(self.changelog or "_Список изменений пуст_")
        else:  # pragma: no cover - Qt < 5.14
            self.changelog_view.setPlainText(self.changelog)
        self.changelog_view.setMinimumHeight(150)
        root.addWidget(self.changelog_view, 1)

        self.steps = StepIndicator(["Скачивание", "Проверка", "Установка"])
        root.addWidget(self.steps)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_label = QLabel("Готово к загрузке")
        self.status_label.setStyleSheet("color: %s; font-size: 12px;" % MUTED_COLOR)
        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: %s; font-size: 12px;" % MUTED_COLOR)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        status_row.addWidget(self.detail_label)
        root.addLayout(status_row)

        self.progress_bar = GradientBar(self)
        self.progress_bar.set_text("")
        root.addWidget(self.progress_bar)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.button_later = QPushButton("Напомнить позже")
        self.button_later.setObjectName("ghostButton")
        self.button_later.setCursor(_cursor_pointing_hand())
        self.button_later.clicked.connect(self._on_later)
        self.button_update = QPushButton("Обновить сейчас")
        self.button_update.setObjectName("primaryButton")
        self.button_update.setCursor(_cursor_pointing_hand())
        self.button_update.clicked.connect(self._on_update_clicked)
        self.button_update.setDefault(True)
        buttons.addWidget(self.button_later)
        buttons.addWidget(self.button_update)
        root.addLayout(buttons)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QDialog { background-color: %(bg)s; }
            QLabel { color: %(text)s; }
            QTextBrowser {
                background-color: %(panel)s;
                color: %(text)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
                padding: 10px;
                selection-background-color: %(accent)s;
                selection-color: #08131c;
            }
            QPushButton#primaryButton {
                background-color: %(accent)s;
                color: #06141c;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                padding: 10px 22px;
            }
            QPushButton#primaryButton:hover { background-color: #45e0ff; }
            QPushButton#primaryButton:disabled {
                background-color: %(border)s; color: %(muted)s;
            }
            QPushButton#ghostButton {
                background-color: transparent;
                color: %(muted)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
                padding: 10px 18px;
            }
            QPushButton#ghostButton:hover { background-color: %(panel)s; color: %(text)s; }
            QPushButton#linkButton {
                background: transparent;
                color: %(accent)s;
                border: none;
                text-align: left;
                padding: 2px 0;
                font-size: 12px;
            }
            QPushButton#linkButton:hover { text-decoration: underline; }
            """
            % {
                "bg": BG_COLOR,
                "panel": PANEL_COLOR,
                "border": BORDER_COLOR,
                "text": TEXT_COLOR,
                "muted": MUTED_COLOR,
                "accent": ACCENT_COLOR,
            }
        )

    def _fade_in(self):
        self.setWindowOpacity(0.0)
        self.animation = QtCore.QPropertyAnimation(self, b"windowOpacity", self)
        self.animation.setDuration(260)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(_easing_out_cubic())
        self.animation.start()

    # ------------------------------------------------------------------ слоты
    def _open_release_page(self):
        if self.release_url:
            try:
                import webbrowser

                webbrowser.open(self.release_url)
            except Exception:  # pragma: no cover - браузер есть не всегда
                pass

    def _on_later(self):
        if self._busy:
            self.cancel_download()
            return
        self.snoozed.emit()
        self.reject()

    def _on_update_clicked(self):
        if self._busy:
            return
        self.start_download()

    # -------------------------------------------------------------- загрузка
    def start_download(self):
        """Начать (или повторить) скачивание обновления."""
        if self._busy:
            return
        self._cancel_requested = False
        self._download_path = None
        self._busy = True
        self.steps.reset()
        self.steps.activate(0)
        self.progress_bar.setValue(0)
        self._set_status("Скачивание…", "info")
        self.detail_label.setText("")
        self.button_update.setEnabled(False)
        self.button_update.setText("Загрузка…")
        self.button_later.setText("Отмена")

        self.worker = self._installer_factory()
        self.worker.progress.connect(self._on_progress)
        self.worker.stage.connect(self._on_stage)
        self.worker.done.connect(self._on_downloaded)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.start()

    def cancel_download(self):
        """Отменить загрузку (кнопка становится «Отмена»)."""
        self._cancel_requested = True
        if self.worker is not None:
            cancel = getattr(self.worker, "cancel", None)
            if callable(cancel):
                cancel()
        self._on_cancelled()

    def _on_progress(self, percent, done_bytes=0.0, speed=0.0, eta=-1.0):
        """Ход загрузки: 0–100, ``-1`` — размер неизвестен («занят»)."""
        if percent < 0:
            self.progress_bar.set_indeterminate(True)
            self.status_label.setText("Скачивание… %s" % human_size(done_bytes))
            return
        self.progress_bar.set_indeterminate(False)
        self.progress_bar.setValue(percent)
        parts = ["Скачивание… %d%%" % percent]
        if self.size:
            parts.append("%s из %s" % (human_size(done_bytes), human_size(self.size)))
        else:
            parts.append(human_size(done_bytes))
        self._set_status(" · ".join(parts), "info")
        details = []
        if speed and speed > 0:
            details.append(format_speed(speed))
        if eta and eta > 0:
            details.append("осталось %s" % format_eta(eta))
        self.detail_label.setText(" · ".join(details))

    def _on_stage(self, text):
        self.status_label.setText(text)
        if "SHA" in text or "Проверк" in text:
            self.steps.activate(1)
            self.progress_bar.set_indeterminate(True)

    def _on_downloaded(self, path):
        """Файл скачан и проверен: переходим к установке."""
        self._download_path = path
        self.progress_bar.set_indeterminate(False)
        self.progress_bar.setValue(100)
        self.steps.complete(0)
        self.steps.activate(1)
        self.detail_label.setText("")

        if not self._is_frozen():
            self.steps.complete(1)
            self._set_status(
                "Файл скачан и проверен. Запуск из исходников — файлы не "
                "меняются, обновите репозиторий вручную.",
                "warn",
            )
            self._finish_buttons(installed=False)
            return

        self.steps.complete(1)
        self.steps.activate(2)
        self._set_status("Установка обновления и перезапуск…", "info")
        self._busy = False
        try:
            install_and_restart(path)
        except Exception as exc:
            self._on_failed("установка не удалась: %s" % exc)
            return
        self.steps.complete(2)
        self._set_status("Готово! Программа перезапускается…", "ok")
        self._finish_buttons(installed=True)
        self.installed.emit()
        QTimer.singleShot(900, self.accept)

    def _on_failed(self, message):
        self._busy = False
        self.progress_bar.set_indeterminate(False)
        self._set_status("Ошибка: %s" % message, "error")
        self.detail_label.setText("")
        self.button_update.setEnabled(True)
        self.button_update.setText("Повторить")
        self.button_later.setText("Закрыть")

    def _on_cancelled(self):
        self._busy = False
        self.progress_bar.set_indeterminate(False)
        self.progress_bar.setValue(0)
        self.steps.reset()
        self._set_status("Загрузка отменена.", "warn")
        self.detail_label.setText("")
        self.button_update.setEnabled(True)
        self.button_update.setText("Обновить сейчас")
        self.button_later.setText("Напомнить позже")

    def _finish_buttons(self, installed):
        self.button_update.setEnabled(False)
        self.button_update.setText("Установлено" if installed else "Скачано")
        self.button_later.setText("Закрыть")

    # -------------------------------------------------------------- утилиты
    def _set_status(self, text, kind="info"):
        color = {
            "ok": OK_COLOR,
            "error": ERROR_COLOR,
            "warn": WARN_COLOR,
        }.get(kind, MUTED_COLOR)
        weight = "bold" if kind in ("ok", "error") else "normal"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color: %s; font-size: 12px; font-weight: %s;" % (color, weight)
        )

    def _is_frozen(self):
        """Собранная сборка (PyInstaller) или запуск из исходников?"""
        try:
            from .updater import is_frozen

            return is_frozen()
        except Exception:  # pragma: no cover - модуль недоступен
            return bool(getattr(sys, "frozen", False))

    def asset_present(self):
        return bool(self.download_url)

    # --------------------------------------------------------------- события
    def reject(self):
        if self._busy:
            self.cancel_download()
        super().reject()

    def closeEvent(self, event):  # noqa: N802
        """Закрытие не ждёт поток: загрузка отменяется, поток отпускается."""
        if self._busy:
            self.cancel_download()
        worker, self.worker = self.worker, None
        if worker is not None:
            try:
                if worker.isRunning():
                    cancel = getattr(worker, "cancel", None)
                    if callable(cancel):
                        cancel()
            except Exception:  # pragma: no cover - заглушка без Qt
                pass
        super().closeEvent(event)


# --------------------------------------------------------------- демо-режим
def _demo():  # pragma: no cover - ручной просмотр
    app = QApplication.instance() or QApplication([])
    changelog = dedent_markdown(
        """
        ### 🚀 Новые возможности
        * **Поддержка exFAT** — чтение и запись.
        * **Ускоренный автопоиск** — работает на 30% быстрее.

        ### 🐛 Исправления
        * Исправлено чтение LBA на дампах больше 32 ГБ.
        * Исправлен краш при открытии пустого образа.
        """
    )
    dialog = UpdateDialog(
        "1.26.0",
        "1.27.0",
        changelog,
        "",
        None,
        asset_name="MountFile.exe",
        release_url="https://github.com/Alexkkkkk/mount-file/releases",
    )
    return dialog, app


if __name__ == "__main__":  # pragma: no cover - ручной просмотр
    _dialog, _app = _demo()
    _dialog.show()
    sys.exit(_app.exec())
