# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Индикатор обновления не «висит» и закрытие окна не ждёт поток.

Проверяется то, из-за чего окно выглядело замороженным при обновлении:
индикатор этапа сбрасывается сторожем, а ``closeEvent`` не блокирует
поток GUI ожиданием фонового потока (``QThread.wait``).

``MOUNTFILE_NO_UPDATE`` здесь НЕ выставляется: переменная глобальная, и её
установка ломает ``tests/test_updater.py::test_updater_thread_never_raises_offline``,
который проверяет работу проверки обновлений. Поток глушится подменой
``updates_disabled`` на время теста.

Run:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_update_ui.py -q
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("mountfile.qt_compat", reason="нет установленных Qt-биндингов")

from mountfile import tc_window  # noqa: E402
from mountfile.qt_compat import QApplication  # noqa: E402
from mountfile.tc_window import MainWindow, _ORPHAN_THREADS  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_update(monkeypatch):
    """Не поднимать поток проверки обновлений (без сети и без env-переменной)."""
    monkeypatch.setattr(tc_window, "updates_disabled", lambda: True)


def _pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_stage_indicator_is_reset_by_watchdog(app, no_update):
    """Сторож гасит индикатор, если этап обновления так и не закончился."""
    win = MainWindow()
    win.UPDATE_WATCHDOG_MS = 80

    win._update_stage("Установка обновления…")
    # окно не показано, поэтому проверяется явное скрытие, а не isVisible()
    assert not win._update_bar.isHidden(), "индикатор не показался"
    assert win._update_stage_label.text() == "Установка обновления…"

    _pump(app, 0.5)

    assert win._update_bar.isHidden(), "индикатор остался висеть"
    assert win._update_stage_label.isHidden()
    log = win.log.toPlainText()
    assert "не завершилось" in log, log[-300:]

    win.close()
    assert app is not None


def test_progress_then_hide_stops_the_watchdog(app, no_update):
    """Обычный ход обновления (прогресс -> скрытие) сторож не ломает."""
    win = MainWindow()
    win.UPDATE_WATCHDOG_MS = 80

    win._update_progress(42)
    assert win._update_bar.value() == 42
    win._update_hide()
    _pump(app, 0.3)

    assert win._update_bar.isHidden()
    assert "не завершилось" not in win.log.toPlainText()

    win.close()
    assert app is not None


def test_close_event_does_not_wait_for_the_updater(app, no_update):
    """Закрытие окна мгновенное: поток обновления не ожидается в GUI."""

    class SlowUpdater:
        def __init__(self):
            self.waited = False

        def isRunning(self):  # noqa: N802 - имя из Qt API
            return True

        def wait(self, *_a, **_k):
            self.waited = True
            time.sleep(5)  # если бы его ждали, тест это заметит

    win = MainWindow()
    slow = SlowUpdater()
    win.updater = slow

    start = time.time()
    win.close()
    elapsed = time.time() - start

    assert slow.waited is False, "closeEvent всё ещё ждёт поток обновления"
    assert elapsed < 1.0, "закрытие окна заняло %.2f с" % elapsed
    assert slow in _ORPHAN_THREADS, "ссылка на поток потеряна (Qt убьёт поток)"
