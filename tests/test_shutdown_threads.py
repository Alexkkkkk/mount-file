# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Закрытие окна не убивает процесс работающим QThread.

Регрессия: ``closeEvent`` ждал поток обновления 1.5 с (``wait(1500)``), а
если проверка не успевала (медленная сеть, прокси, недоступный GitHub),
окно уничтожалось вместе с дочерним QThread. Qt в этом случае печатает
``QThread: Destroyed while thread is still running`` и вызывает ``abort()``:
процесс падает с ``Fatal Python error: Aborted`` и кодом 134. Именно это
роняло CI-джобу «gui smoke test (offscreen)» и само приложение при выходе
во время проверки обновлений.

Проверка идёт в отдельном процессе: падение — это SIGABRT, внутри
pytest-раннера его не поймать.
"""

import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

try:  # pragma: no cover - зависит от окружения
    import mountfile.qt_compat as _qt_compat  # noqa: F401

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

needs_qt = pytest.mark.skipif(not HAVE_QT, reason="нет установленных Qt-биндингов")


#: Окно создаётся, закрывается и уничтожается, пока проверка обновлений
#: «висит» на недоступном адресе. Скрипт обязан напечатать OK и выйти с 0.
CHILD = textwrap.dedent(
    """
    import gc, os, sys, time
    sys.path.insert(0, os.getcwd())
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # Недоступный адрес: urlopen висит до таймаута, поток не успевает выйти.
    os.environ["MOUNTFILE_UPDATE_URL"] = "http://10.255.255.1/latest.json"

    from mountfile.qt_compat import QApplication
    from mountfile.tc_window import MainWindow, _ORPHAN_THREADS

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    app.processEvents()
    started = time.time()
    win.close()
    elapsed = time.time() - started
    del win
    gc.collect()
    app.processEvents()
    assert elapsed < 1.0, "закрытие окна ждало поток обновления: %.2f c" % elapsed
    assert _ORPHAN_THREADS, "поток обновления не сохранён — Qt его уничтожит"
    print("OK")
    """
)


@needs_qt
def test_closing_the_window_while_the_update_check_hangs_does_not_abort():
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("MOUNTFILE_NO_UPDATE", None)

    proc = subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, (
        "процесс завершился с кодом %s (SIGABRT = работающий QThread "
        "уничтожен вместе с окном)\nstdout:\n%s\nstderr:\n%s"
        % (proc.returncode, proc.stdout, proc.stderr[-3000:])
    )
    assert "OK" in proc.stdout, proc.stdout
    assert "Destroyed while thread is still running" not in proc.stderr, proc.stderr[-2000:]
