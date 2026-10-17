# Интеграция UniversalFSRW в `mountfile/tc_window.py`

Фрагмент ниже — присланный код для класса `TcWindow`. Это **не самостоятельный
модуль**, а справочный фрагмент: методы `open_image` / `act_write` /
`act_flush` / `act_restore` предназначены для существующего класса
`TcWindow` в `mountfile/tc_window.py`.

Существующий `mountfile/tc_window.py` в этой ветке намеренно **не изменялся** —
фрагмент вынесен отдельным файлом, чтобы ничего не перезаписать.

Как применить:

1. Скопируйте импорт `from mountfile.universal_fs_rw import UniversalFSRW, FSEntry, FSInfo`
   в шапку `mountfile/tc_window.py`.
2. Перенесите четыре метода ниже в класс `TcWindow`.
3. Имена `QMainWindow`, `QFileDialog`, `QInputDialog`, `QMessageBox` и `Path`
   уже доступны в контексте `tc_window.py` (через `qt_compat.py`).

```python
# mountfile/tc_window.py
from mountfile.universal_fs_rw import UniversalFSRW, FSEntry, FSInfo

class TcWindow(QMainWindow):
    def open_image(self, path: Path) -> None:
        self.fs = UniversalFSRW(path, auto_backup=True)
        info = self.fs.info()
        self.statusBar().showMessage(
            f"{info.fs_type} @ 0x{info.offset:x} — "
            f"{'RW' if info.writable else 'RO'} ({info.write_strategy})"
        )
        self.refresh_panels()

    def act_write(self) -> None:
        src = QFileDialog.getOpenFileName(self, "Файл для записи")[0]
        if not src:
            return
        dst = QInputDialog.getText(self, "Путь в образе", "Куда:")[0]
        try:
            self.fs.write_file(dst, Path(src).read_bytes())
            self.refresh_panels()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def act_flush(self) -> None:
        r = self.fs.flush()
        QMessageBox.information(self, "fsck", str(r))

    def act_restore(self) -> None:
        if QMessageBox.question(self, "Откат?",
                "Восстановить образ из последнего бэкапа?") == QMessageBox.Yes:
            self.fs.restore_from_backup()
            self.refresh_panels()
```
