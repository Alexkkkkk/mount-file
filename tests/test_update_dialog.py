# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Тесты «Центра обновлений» (mountfile/update_dialog.py).

Здесь проверяется то, из-за чего первая версия окна не работала:

* сигнал больше не пытается пронести ``bytes`` — файл пишется на диск;
* sha256 считается по ходу загрузки и реально сравнивается;
* Markdown «Что нового» рендерится как Markdown, а не как блок кода
  (общий отступ снимается);
* загрузку можно отменить, а .cmd-подмена на Windows ограничена по
  времени и не показывает окно консоли.

Загрузка проверяется по-настоящему — через локальный HTTP-сервер на
``127.0.0.1``, без доступа в интернет.

Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_update_dialog.py -q
"""

import hashlib
import http.server
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip(
    "mountfile.qt_compat",
    exc_type=ImportError,
    reason="нет установленных Qt-биндингов",
)

from mountfile import update_dialog as ud  # noqa: E402
from mountfile.qt_compat import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------- форматирование
def test_human_size_and_speed():
    assert ud.human_size(0) == "0 Б"
    assert ud.human_size(1536) == "1.5 КБ"
    assert ud.human_size(5 * 1024 * 1024) == "5.0 МБ"
    assert ud.format_speed(1.5 * 1024 * 1024) == "1.5 МБ/с"
    assert ud.format_speed(0) == "—"


def test_format_eta():
    assert ud.format_eta(0) == "—"
    assert ud.format_eta(-5) == "—"
    assert ud.format_eta(75) == "1:16"
    assert ud.format_eta(3671) == "1:01:12"


def test_dedent_markdown_removes_common_indent():
    """Иначе четыре пробела превращают весь changelog в блок кода."""
    raw = """
    ### Новое
    * поддержка exFAT
    """
    result = ud.dedent_markdown(raw)
    assert result.startswith("### Новое")
    assert "\n* поддержка exFAT" in result
    assert not any(line.startswith(" ") for line in result.split("\n"))
    # пустые строки не мешают снятию отступа
    assert ud.dedent_markdown("\n\n    ### A\n\n    * b\n").startswith("### A")


def test_looks_like_sha256():
    assert ud.looks_like_sha256(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert not ud.looks_like_sha256("abc")


# --------------------------------------------------------------- локальный HTTP
class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    truncate = False

    def do_GET(self):  # noqa: N802
        # Заголовок о длине отдаётся всегда, но при truncate=True тело
        # короче — так проверяется детектор неполной загрузки.
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        body = (
            self.payload[: max(1, len(self.payload) // 2)]
            if self.truncate
            else self.payload
        )
        for start in range(0, len(body), 8192):
            self.wfile.write(body[start : start + 8192])

    def log_message(self, *args):  # noqa: D102 - без шума в тестах
        pass


@pytest.fixture
def http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    _Handler.payload = b""
    _Handler.truncate = False
    yield base
    server.shutdown()
    server.server_close()


# ------------------------------------------------------------------- загрузка
def test_download_worker_streams_to_disk_and_verifies_sha(tmp_path, http_server):
    """Файл скачивается на диск, хеш совпадает, прогресс доходит до 100 %."""
    payload = os.urandom(200 * 1024)
    _Handler.payload = payload
    digest = hashlib.sha256(payload).hexdigest()

    seen = []
    worker = ud.DownloadWorker(
        http_server + "/MountFile.exe",
        sha256=digest,
        size=len(payload),
        dest_dir=str(tmp_path),
        tick_seconds=0.0,
    )
    done, failed, cancelled = [], [], []
    worker.progress.connect(lambda *args: seen.append(args))
    worker.done.connect(done.append)
    worker.failed.connect(failed.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.run()  # синхронно: сеть локальная, цикла событий не нужно

    assert not failed and not cancelled
    assert done, "сигнал done должен нести путь к файлу"
    path = done[0]
    assert isinstance(path, str) and os.path.exists(path)
    assert open(path, "rb").read() == payload, "файл должен быть целым на диске"
    assert seen[-1][0] == 100
    # последний прогресс: байты, скорость и ETA — числа, а не bytes
    _percent, done_bytes, speed, eta = seen[-1]
    assert done_bytes == float(len(payload))
    assert speed >= 0.0 and eta <= 0.0


def test_download_worker_detects_bad_checksum(tmp_path, http_server):
    payload = os.urandom(64 * 1024)
    _Handler.payload = payload

    done, failed = [], []
    worker = ud.DownloadWorker(
        http_server + "/MountFile.exe",
        sha256="0" * 64,
        dest_dir=str(tmp_path),
        tick_seconds=0.0,
    )
    worker.done.connect(done.append)
    worker.failed.connect(failed.append)
    worker.run()

    assert not done
    assert failed and "контрольн" in failed[0].lower()
    assert list(tmp_path.iterdir()) == [], "битый файл не должен оставаться"


def test_download_worker_detects_truncated_body(tmp_path, http_server):
    _Handler.payload = os.urandom(128 * 1024)
    _Handler.truncate = True

    failed = []
    worker = ud.DownloadWorker(
        http_server + "/MountFile.exe",
        dest_dir=str(tmp_path),
        tick_seconds=0.0,
    )
    worker.failed.connect(failed.append)
    worker.run()
    _Handler.truncate = False

    assert failed and ("неполн" in failed[0] or "из " in failed[0])


def test_download_worker_cancel_stops_mid_stream(tmp_path, http_server):
    _Handler.payload = os.urandom(200 * 1024)

    done, cancelled = [], []
    worker = ud.DownloadWorker(
        http_server + "/MountFile.exe",
        dest_dir=str(tmp_path),
        tick_seconds=0.0,
    )
    worker.done.connect(done.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    def _stop_after_first_tick(*_args):
        worker.cancel()

    worker.progress.connect(_stop_after_first_tick)
    worker.run()

    assert cancelled == [True], "отмена должна доходить до потока"
    assert not done
    assert list(tmp_path.iterdir()) == [], "недокачанный файл удаляется"


def test_download_worker_reports_unknown_size_as_minus_one(tmp_path, http_server):
    """Без Content-Length полоса работает в режиме «занят» (percent = -1)."""
    worker = ud.DownloadWorker("", dest_dir=str(tmp_path))
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert failed and "адрес" in failed[0]


# ------------------------------------------------------------------- подмена
def test_windows_swap_bat_is_bounded_and_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(ud.tempfile, "gettempdir", lambda: str(tmp_path))
    script = ud._windows_swap_bat(
        "C:\\new\\MountFile.exe",
        "C:\\app\\MountFile.exe",
        4242,
        str(tmp_path / "u.log"),
    )
    text = open(script, encoding="utf-8").read()
    assert text.startswith("@echo off")
    assert "4242" in text
    assert "move /Y" in text and 'start ""' in text
    assert "set /a TRIES+=1" in text
    assert "GEQ %d" % ud.WINDOWS_WAIT_TRIES in text
    assert "GEQ %d" % ud.WINDOWS_MOVE_TRIES in text
    assert "__WAIT_TRIES__" not in text and "__NEW__" not in text
    assert "pause" not in text.lower(), "ожидания нажатия клавиши быть не должно"


def test_install_and_restart_refuses_source_mode(monkeypatch):
    monkeypatch.delattr(ud.sys, "frozen", raising=False)
    try:
        ud.install_and_restart("/nonexistent/MountFile.exe")
    except RuntimeError as exc:
        assert "исходник" in str(exc) or "самозамена" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("в режиме исходников подмена файлов запрещена")


# -------------------------------------------------------------------- виджеты
def test_gradient_bar_animates_and_supports_busy_mode(app):
    bar = ud.GradientBar()
    bar.setValue(43)
    assert bar.value() == 43
    assert not bar.is_indeterminate()
    bar.set_indeterminate(True)
    assert bar.is_indeterminate()
    bar.set_indeterminate(False)
    bar.show()
    app.processEvents()
    phase = bar._phase
    bar._tick()
    assert bar._phase != phase, "фаза анимации должна меняться"
    bar.hide()


def test_step_indicator_states(app):
    steps = ud.StepIndicator(["Скачивание", "Проверка", "Установка"])
    steps.activate(0)
    assert steps.states() == [steps.ACTIVE, steps.PENDING, steps.PENDING]
    steps.complete(0)
    steps.activate(1)
    assert steps.states() == [steps.DONE, steps.ACTIVE, steps.PENDING]
    steps.complete(2)
    assert steps.states()[2] == steps.DONE
    steps.reset()
    assert steps.states() == [steps.PENDING] * 3


# --------------------------------------------------------------------- окно
def _dialog(**kwargs):
    changelog = """
    ### 🚀 Новое
    * поддержка exFAT
    """
    params = dict(
        current_ver="1.26.0",
        new_ver="1.27.0",
        changelog=changelog,
        download_url="https://example.invalid/MountFile.exe",
        sha256_hash="0" * 64,
        asset_name="MountFile.exe",
        release_url="https://github.com/Alexkkkkk/mount-file/releases",
        size=1024 * 1024,
    )
    params.update(kwargs)
    return ud.UpdateDialog(**params)


def test_dialog_renders_markdown_not_code_block(app):
    dialog = _dialog()
    text = dialog.changelog_view.toPlainText()
    assert "поддержка exFAT" in text
    assert "###" not in text, "Markdown должен рендериться, а не показываться как текст"
    assert dialog.version_label.text().endswith("<b>v1.27.0</b>")
    assert dialog.progress_bar.value() == 0
    assert dialog.button_update.isEnabled()
    dialog.close()


def test_dialog_progress_shows_speed_and_eta(app):
    dialog = _dialog()
    dialog._on_progress(37, 400.0 * 1024, 2.5 * 1024 * 1024, 12.0)
    status = dialog.status_label.text()
    details = dialog.detail_label.text()
    assert "37%" in status and "из" in status
    assert "МБ/с" in details and "осталось" in details
    assert dialog.progress_bar.value() == 37
    dialog.close()


def test_dialog_unknown_size_switches_bar_to_busy(app):
    dialog = _dialog()
    dialog._on_progress(-1, 250.0 * 1024)
    assert dialog.progress_bar.is_indeterminate()
    assert "КБ" in dialog.status_label.text()
    dialog.close()


def test_dialog_stage_moves_step_indicator(app):
    dialog = _dialog()
    dialog._on_stage("Проверка SHA-256…")
    assert dialog.steps.states()[1] == dialog.steps.ACTIVE
    dialog.close()


def test_dialog_failure_offers_retry(app):
    dialog = _dialog()
    dialog._on_failed("сеть недоступна")
    assert dialog.button_update.isEnabled()
    assert dialog.button_update.text() == "Повторить"
    assert dialog.button_later.text() == "Закрыть"
    assert "Ошибка" in dialog.status_label.text()
    dialog.close()


def test_dialog_cancel_restores_buttons(app):
    dialog = _dialog()
    dialog._busy = True
    dialog.button_update.setEnabled(False)
    dialog.cancel_download()
    assert dialog._busy is False
    assert dialog.button_update.isEnabled()
    assert dialog.button_update.text() == "Обновить сейчас"
    assert dialog.progress_bar.value() == 0
    dialog.close()


def test_dialog_source_mode_does_not_replace_files(tmp_path, monkeypatch, app):
    """Запуск из исходников: файл скачан, но подмена не выполняется."""
    payload = b"fake-binary"
    target = tmp_path / payload.decode()
    target.write_bytes(payload)

    monkeypatch.setattr(ud.UpdateDialog, "_is_frozen", lambda self: False)
    calls = []
    monkeypatch.setattr(ud, "install_and_restart", lambda path: calls.append(path))

    dialog = _dialog()
    dialog._on_downloaded(str(target))
    assert calls == [], "в режиме исходников install_and_restart не вызывается"
    assert "исходник" in dialog.status_label.text().lower()
    assert dialog.steps.states()[1] == dialog.steps.DONE
    dialog.close()


def test_dialog_from_release_maps_api_payload(app):
    info = {
        "version": "1.27.0",
        "tag": "v1.27.0",
        "url": "https://github.com/Alexkkkkk/mount-file/releases/tag/v1.27.0",
        "notes": "    ### Исправления\n    * починили LBA\n",
        "asset": {
            "name": "MountFile.exe",
            "size": 12345,
            "browser_download_url": "https://example.invalid/MountFile.exe",
        },
        "sha256": "a" * 64,
    }
    dialog = ud.UpdateDialog.from_release(info, "1.26.0")
    assert dialog.new_ver == "1.27.0"
    assert dialog.download_url == "https://example.invalid/MountFile.exe"
    assert dialog.sha256_hash == "a" * 64
    assert dialog.size == 12345
    assert dialog.changelog.startswith("### Исправления")
    dialog.close()
