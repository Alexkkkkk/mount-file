# SPDX-License-Identifier: MIT
"""Smoke-тест: окно стартует без сети и с битым URL обновления.

Запуск:  QT_QPA_PLATFORM=offscreen python3 tests/smoke_startup.py
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main():
    from PyQt5.QtWidgets import QApplication

    with tempfile.TemporaryDirectory() as tmp:
        # заведомо нерабочий: локальный порт, где никто не слушает
        os.environ["MOUNTFILE_UPDATE_URL"] = "http://127.0.0.1:9/latest.json"
        from mountfile import updater
        from mountfile.tc_window import MainWindow

        app = QApplication(sys.argv)
        win = MainWindow()

        deadline = time.time() + 12
        while time.time() < deadline:
            app.processEvents()
            if win.updater is not None and win.updater.isFinished():
                break
            time.sleep(0.05)
        text = win.log.toPlainText()
        print("--- журнал окна ---")
        print(text)
        assert "Проверка обновлений" in text, "проверка не запустилась"
        assert ("не удалась" in text) or (
            "Обновлений нет" in text
        ), "нет строки о результате проверки"
        win._close_writer()
        win.close()

        # офлайн-проверка без Qt вообще
        print("--- офлайн-режим Updater.run() ---")
        worker = updater.Updater(current_version="0.0.1")
        seen = []
        worker.status.connect(seen.append)
        worker.run()
        print(seen)
        assert seen, "нет сообщения о неудачной проверке"

        # битый JSON/URL не должны бросать исключение в потоке
        os.environ["MOUNTFILE_UPDATE_URL"] = "not-a-url"
        worker2 = updater.Updater(current_version="0.0.1")
        seen2 = []
        worker2.status.connect(seen2.append)
        worker2.run()
        print(seen2)
        assert seen2, "нет сообщения о неудачной проверке (битый URL)"

    print("SMOKE OK: приложение стартует без сети и с битым URL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
