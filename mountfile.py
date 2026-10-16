# -*- coding: utf-8 -*-
"""
Mount File — единый файл.
Включает:
  • UI (PyQt6)
  • StateManager  — сохранение настроек (QSettings) и «самообучения» (JSON)
  • Updater       — автообновление с сохранением данных перед перезапуском
  • Точка входа с обработкой сигналов

Запуск:  python mountfile.py
Сборка:  pyinstaller --noconfirm --onefile --windowed --name MountFile mountfile.py
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer, QSize
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFileDialog,
    QMessageBox, QStatusBar, QProgressBar, QCheckBox, QLineEdit,
    QComboBox, QGroupBox, QFormLayout, QSplitter, QTextEdit
)

# ============================================================
#  КОНСТАНТЫ
# ============================================================
APP_NAME = "MountFile"
ORG_NAME = "Alexkkkkk"
APP_VERSION = "1.0.0"
GITHUB_REPO = "Alexkkkkk/mount-file"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
STATE_VERSION = 1

# ============================================================
#  ЛОГИРОВАНИЕ
# ============================================================
log = logging.getLogger(APP_NAME)


def user_data_dir() -> Path:
    """Папка пользовательских данных — живёт отдельно от exe,
       поэтому обновление её не стирает."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = user_data_dir()
LEARNING_FILE = DATA_DIR / "learning.json"
MIGRATION_FILE = DATA_DIR / "state_version.json"
LOG_FILE = DATA_DIR / "mountfile.log"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ============================================================
#  STATE MANAGER
# ============================================================
class StateManager:
    """
    - QSettings  -> настройки UI (геометрия, последний образ, тема)
    - JSON       -> «самообучение» (любимые пути, история, счётчики)
    Всё хранится в системных папках, переживает обновление.
    """

    def __init__(self):
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._learning = self._load_learning()
        self._migrate_if_needed()

    # ---------- UI ----------
    def get(self, key, default=None):
        return self.settings.value(key, default)

    def set(self, key, value):
        self.settings.setValue(key, value)

    def save_geometry(self, window):
        self.settings.setValue("ui/geometry", window.saveGeometry())
        self.settings.setValue("ui/window_state", window.saveState())

    def restore_geometry(self, window):
        geo = self.settings.value("ui/geometry")
        if geo:
            window.restoreGeometry(geo)
        st = self.settings.value("ui/window_state")
        if st:
            window.restoreState(st)

    # ---------- Learning ----------
    def _load_learning(self) -> dict:
        if LEARNING_FILE.exists():
            try:
                with open(LEARNING_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Не читается learning.json: %s", e)
        return {
            "favorite_paths": [],
            "recent_images": [],
            "frequent_actions": {},
            "custom_prefs": {},
        }

    def get_learning(self, key, default=None):
        return self._learning.get(key, default)

    def learn_path(self, path: str):
        favs = self._learning.setdefault("favorite_paths", [])
        if path in favs:
            favs.remove(path)
        favs.insert(0, path)
        self._learning["favorite_paths"] = favs[:20]

    def learn_action(self, action: str):
        c = self._learning.setdefault("frequent_actions", {})
        c[action] = c.get(action, 0) + 1

    def remember_image(self, path: str):
        recent = self._learning.setdefault("recent_images", [])
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        self._learning["recent_images"] = recent[:15]

    def flush_learning(self):
        """Атомарная запись — не потеряем данные даже при сбое питания."""
        tmp = LEARNING_FILE.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._learning, f, ensure_ascii=False, indent=2)
            os.replace(tmp, LEARNING_FILE)
        except Exception as e:
            log.error("Не сохранился learning.json: %s", e)

    # ---------- Migration ----------
    def _migrate_if_needed(self):
        current = 0
        if MIGRATION_FILE.exists():
            try:
                with open(MIGRATION_FILE, encoding="utf-8") as f:
                    current = json.load(f).get("version", 0)
            except Exception:
                current = 0
        if current == STATE_VERSION:
            return
        log.info("Миграция состояния %s -> %s", current, STATE_VERSION)
        # Сюда добавляйте шаги миграции при росте версии:
        # if current < 2: ...
        try:
            with open(MIGRATION_FILE, "w", encoding="utf-8") as f:
                json.dump({"version": STATE_VERSION}, f)
        except Exception as e:
            log.error("Не записался state_version.json: %s", e)


# ============================================================
#  UPDATER
# ============================================================
class UpdateChecker(QThread):
    """Проверяет последний релиз на GitHub."""
    update_available = pyqtSignal(str, str)   # (version, download_url)
    no_update = pyqtSignal(str)

    def __init__(self, current_version: str, parent=None):
        super().__init__(parent)
        self.current_version = current_version

    def run(self):
        try:
            req = Request(GITHUB_API, headers={"User-Agent": APP_NAME})
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            tag = (data.get("tag_name") or "").lstrip("v")
            if not tag:
                self.no_update.emit(self.current_version)
                return
            if self._is_newer(tag, self.current_version):
                url = self._pick_asset(data.get("assets", []))
                if url:
                    self.update_available.emit(tag, url)
                    return
            self.no_update.emit(self.current_version)
        except URLError as e:
            log.warning("Проверка обновлений не удалась: %s", e)
            self.no_update.emit(self.current_version)
        except Exception as e:
            log.exception("Ошибка проверки обновлений: %s", e)
            self.no_update.emit(self.current_version)

    @staticmethod
    def _is_newer(new: str, cur: str) -> bool:
        def parse(v):
            return [int(x) for x in v.split(".") if x.isdigit()]
        try:
            return parse(new) > parse(cur)
        except Exception:
            return new != cur

    @staticmethod
    def _pick_asset(assets):
        # Приоритет: Windows .exe -> Linux bin -> macOS
        def find(suffixes):
            for a in assets:
                name = (a.get("name") or "").lower()
                if any(name.endswith(s) for s in suffixes):
                    return a.get("browser_download_url")
            return None
        if os.name == "nt":
            return find([".exe"])
        if sys.platform == "darwin":
            return find([".dmg", ".pkg", ".zip"])
        return find(["", ".appimage", ".bin"])


class Installer(QThread):
    """Скачивает и устанавливает обновление. Перед перезапуском
       эмитит `before_restart`, чтобы UI успел сохранить данные."""
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    before_restart = pyqtSignal()
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, download_url: str, parent=None):
        super().__init__(parent)
        self.download_url = download_url

    def run(self):
        try:
            self.status.emit("Скачивание обновления...")
            new_file = self._download()
            self.status.emit("Подготовка к установке...")

            # 1) Дать программе сохранить состояние
            self.before_restart.emit()
            time.sleep(0.7)   # слот save_state должен успеть выполниться

            # 2) Установка и перезапуск
            self.status.emit("Перезапуск...")
            self._install_and_restart(new_file)
            self.finished_ok.emit()
        except Exception as e:
            log.exception("Ошибка установки обновления")
            self.failed.emit(str(e))

    def _download(self) -> Path:
        tmp_dir = Path(tempfile.gettempdir()) / f"{APP_NAME}_update"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        name = self.download_url.split("/")[-1] or "update.bin"
        out = tmp_dir / name

        req = Request(self.download_url, headers={"User-Agent": APP_NAME})
        with urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            chunk = 64 * 1024
            with open(out, "wb") as f:
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total:
                        self.progress.emit(int(done * 100 / total))
        return out

    def _install_and_restart(self, new_file: Path):
        current = Path(sys.executable if getattr(sys, "frozen", False) else __file__)

        if os.name == "nt":
            # .cmd: ждёт выхода текущего процесса, заменяет exe, запускает заново
            cmd = Path(tempfile.gettempdir()) / f"{APP_NAME}_update.cmd"
            script = (
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f':wait\r\n'
                f'tasklist /fi "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul\r\n'
                "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
                f'copy /Y "{new_file}" "{current}" >nul\r\n'
                f'start "" "{current}"\r\n'
                'del "%~f0"\r\n'
            )
            cmd.write_text(script, encoding="ascii")
            subprocess.Popen(
                ["cmd", "/c", str(cmd)],
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                close_fds=True,
            )
            QApplication.quit()
        else:
            # Linux/macOS — заменяем бинарь и execv
            try:
                if new_file.stat().st_mode:
                    os.chmod(new_file, 0o755)
                os.replace(new_file, current)
            except Exception as e:
                log.error("Не удалось заменить бинарь: %s", e)
            os.execv(str(current), [str(current)] + sys.argv[1:])


# ============================================================
#  UI
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self, state: StateManager):
        super().__init__()
        self.state = state
        self.current_image: str = ""

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(900, 600)

        self._build_ui()
        self._build_menu()
        self._build_status()

        # Восстановление
        self.state.restore_geometry(self)
        self._restore_state()

        # Проверка обновлений через 3 секунды после старта
        QTimer.singleShot(3000, self.check_for_updates)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Левая панель — история и любимые пути
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Недавние образы:"))
        self.recent_list = QListWidget()
        self.recent_list.itemDoubleClicked.connect(self._open_recent)
        left_layout.addWidget(self.recent_list)

        left_layout.addWidget(QLabel("Любимые пути:"))
        self.fav_list = QListWidget()
        self.fav_list.itemDoubleClicked.connect(self._open_recent)
        left_layout.addWidget(self.fav_list)

        splitter.addWidget(left)

        # Правая панель — настройки + журнал
        right = QWidget()
        right_layout = QVBoxLayout(right)

        grp = QGroupBox("Настройки")
        form = QFormLayout(grp)

        self.chk_autoupdate = QCheckBox("Проверять обновления при запуске")
        self.chk_autoupdate.setChecked(self.state.get("ui/autoupdate", True, type=bool)
                                       if hasattr(self.state.get, "__call__") else True)
        form.addRow(self.chk_autoupdate)

        self.cmb_theme = QComboBox()
        self.cmb_theme.addItems(["Светлая", "Тёмная", "Системная"])
        form.addRow("Тема:", self.cmb_theme)

        self.edit_mountpoint = QLineEdit()
        self.edit_mountpoint.setPlaceholderText("/mnt/image или диск Z:")
        form.addRow("Точка монтирования:", self.edit_mountpoint)

        right_layout.addWidget(grp)

        right_layout.addWidget(QLabel("Журнал:"))
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        right_layout.addWidget(self.log_view)

        splitter.addWidget(right)
        splitter.setSizes([400, 500])
        root.addWidget(splitter)

    def _build_menu(self):
        mb = self.menuBar()

        m_file = mb.addMenu("Файл")
        a_open = QAction("Открыть образ...", self)
        a_open.setShortcut("Ctrl+O")
        a_open.triggered.connect(self.open_image)
        m_file.addAction(a_open)

        a_save = QAction("Сохранить состояние", self)
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self.save_state)
        m_file.addAction(a_save)

        m_file.addSeparator()
        a_exit = QAction("Выход", self)
        a_exit.setShortcut("Ctrl+Q")
        a_exit.triggered.connect(self.close)
        m_file.addAction(a_exit)

        m_help = mb.addMenu("Справка")
        a_upd = QAction("Проверить обновления", self)
        a_upd.triggered.connect(lambda: self.check_for_updates(force=True))
        m_help.addAction(a_upd)

        a_about = QAction("О программе", self)
        a_about.triggered.connect(self._about)
        m_help.addAction(a_about)

    def _build_status(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        sb.addPermanentWidget(self.progress)
        sb.showMessage("Готово")

    # ---------- Действия ----------
    def _restore_state(self):
        # Настройки
        autoupd = self.state.get("ui/autoupdate")
        if autoupd is not None:
            self.chk_autoupdate.setChecked(str(autoupd).lower() in ("true", "1"))

        theme = self.state.get("ui/theme", "Светлая")
        idx = self.cmb_theme.findText(theme)
        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)

        self.edit_mountpoint.setText(self.state.get("ui/mountpoint", ""))

        # Последний образ
        last = self.state.get("session/last_image", "")
        if last and Path(last).exists():
            self.current_image = last
            self._log(f"Восстановлен последний образ: {last}")

        # Самообучение
        self._refresh_lists()

    def _refresh_lists(self):
        self.recent_list.clear()
        for p in self.state.get_learning("recent_images", []):
            it = QListWidgetItem(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.recent_list.addItem(it)

        self.fav_list.clear()
        for p in self.state.get_learning("favorite_paths", []):
            it = QListWidgetItem(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.fav_list.addItem(it)

    def _log(self, msg: str):
        log.info(msg)
        self.log_view.append(msg)

    def _open_recent(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            self.current_image = path
            self.state.remember_image(path)
            self.state.flush_learning()
            self._refresh_lists()
            self.statusBar().showMessage(f"Открыт: {path}")
            self._log(f"Открыт образ: {path}")
        else:
            QMessageBox.warning(self, APP_NAME, f"Файл не найден:\n{path}")

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть образ", "",
            "Образы (*.img *.iso *.bin *.raw);;Все файлы (*.*)"
        )
        if not path:
            return
        self.current_image = path
        self.state.remember_image(path)
        self.state.learn_path(str(Path(path).parent))
        self.state.learn_action("open_image")
        self.state.set("session/last_image", path)
        self.state.flush_learning()
        self._refresh_lists()
        self.statusBar().showMessage(f"Открыт: {path}")
        self._log(f"Открыт образ: {path}")

    def save_state(self):
        """Единая точка сохранения. Вызывается при закрытии
           и ПЕРЕД автообновлением."""
        try:
            self.state.set("session/last_image", self.current_image)
            self.state.set("ui/autoupdate", self.chk_autoupdate.isChecked())
            self.state.set("ui/theme", self.cmb_theme.currentText())
            self.state.set("ui/mountpoint", self.edit_mountpoint.text())
            self.state.save_geometry(self)
            self.state.flush_learning()
            self.state.settings.sync()
            log.info("Состояние сохранено: %s", DATA_DIR)
        except Exception as e:
            log.exception("Не удалось сохранить состояние: %s", e)

    def closeEvent(self, event):
        self.save_state()
        event.accept()

    def _about(self):
        QMessageBox.information(
            self, f"О {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            f"Данные пользователя: {DATA_DIR}\n"
            f"Репозиторий: https://github.com/{GITHUB_REPO}"
        )

    # ---------- Обновления ----------
    def check_for_updates(self, force: bool = False):
        if not force and not self.chk_autoupdate.isChecked():
            return
        self._log("Проверка обновлений...")
        self.checker = UpdateChecker(APP_VERSION, self)
        self.checker.update_available.connect(self._on_update_available)
        self.checker.no_update.connect(self._on_no_update)
        self.checker.start()

    def _on_no_update(self, ver):
        self._log(f"Обновлений нет (текущая {ver})")
        self.statusBar().showMessage("Обновлений нет", 3000)

    def _on_update_available(self, version, url):
        self._log(f"Доступна версия {version}")
        ans = QMessageBox.question(
            self, "Обновление",
            f"Доступна версия {version}.\nОбновиться сейчас?\n\n"
            f"Настройки и история сохранятся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._install_update(url)

    def _install_update(self, url):
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.installer = Installer(url, self)
        self.installer.progress.connect(self.progress.setValue)
        self.installer.status.connect(self.statusBar().showMessage)
        self.installer.status.connect(self._log)
        # ГЛАВНОЕ: сохраняем данные ПЕРЕД заменой exe
        self.installer.before_restart.connect(self.save_state)
        self.installer.failed.connect(lambda e: QMessageBox.critical(
            self, "Ошибка обновления", e))
        self.installer.start()


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================
def main():
    setup_logging()
    log.info("Запуск %s %s", APP_NAME, APP_VERSION)
    log.info("Папка данных: %s", DATA_DIR)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(APP_VERSION)

    state = StateManager()
    window = MainWindow(state)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
