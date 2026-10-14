<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
# Mount File

Утилита и файл-менеджер для монтирования образов дисков (raw-дампов NAND/eMMC/дисков) и работы с файлами внутри них — аналог функции **Mount File** из UFPI (MSL Works), реализованный с нуля на Python.

A file manager for raw disk images (NAND/eMMC/disk dumps). Browse, extract and (on FAT32) modify files inside an image without flashing it back to the device.

## Download / Скачивание

Готовые сборки доступны на странице релизов: **[Download v1.0.0](https://github.com/Alexkkkkk/mount-file/releases/latest)**

| Файл | Платформа |
|---|---|
| [`MountFile.exe`](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile.exe) | Windows 10/11 x64 — автономный exe, Python не нужен |
| [`MountFile-linux-x86-64`](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-linux-x86-64) | Linux x86-64 — автономный бинарник |

Сборку под Windows 7 (Python 3.8 + PyQt5) соберите сами по инструкции ниже.

## Возможности / Features

- 🔍 **Разметка**: автодетекция MBR и GPT, выбор раздела из списка.
- 📂 **Просмотр**: дерево файлов любой ФС, поддерживаемой The Sleuth Kit — FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 (через `pytsk3`).
- 📤 **Извлечение** файлов из образа на диск.
- ✏️ **Запись в FAT32** (собственный движок, без внешних зависимостей):
  - создание и перезапись файлов (включая файлы больше кластера),
  - длинные имена (LFN) и короткие 8.3,
  - создание каталогов (`mkdir`),
  - корректная аллокация/освобождение кластеров (проверено тестами на утечки).
- 🧮 **HEX-просмотр** первого мегабайта выбранного файла, выделение мышью.
- 🪟 **Windows 7** поддерживается через слой совместимости (PyQt5).

## Windows 7

На Windows 7 работает та же кодовая база через слой совместимости (`mountfile/qt_compat.py`):
PyQt6 на Win7 не поддерживается, поэтому используется **PyQt5 (Qt 5.15 LTS — последний Qt для Win7)**
и **Python 3.8.10** (последняя версия Python для Win7).

```bat
:: установка
py -3.8 -m pip install -r requirements-win7.txt

:: запуск
py -3.8 main.py dump.img

:: сборка exe (dist\MountFile.exe, работает без установленного Python)
build_exe.bat
```

На Windows 10/11 и Linux/macOS используйте `requirements.txt` (PyQt6) — код тот же.

## Установка / Installation

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
pip install -r requirements.txt        # Win10+/Linux/macOS (PyQt6)
```

> `pytsk3` предоставляет готовые wheel для Windows/Linux/macOS. Если на вашей платформе wheel нет — соберите libtsk из исходников.

## Запуск / Usage

```bash
python main.py                 # затем File → Open image…
python main.py dump.img        # сразу открыть образ
```

1. **Open image…** — выберите raw-образ (`.img`, `.bin`, `.dump`…).
2. В списке **Volume** выберите раздел (или «Whole image»).
3. Двойной клик по файлу — извлечь; кнопка **Extract…** — то же самое.
4. Если том — FAT32, активируются **Import file…** (записать файл в образ) и **New folder**.

> ⚠️ **Внимание**: запись изменяет файл образа на диске. Делайте бэкап перед импортом.

## Архитектура / Architecture

```
mount-file/
├── main.py               # точка входа (PyQt6 → PyQt5 fallback)
├── mountfile/
│   ├── qt_compat.py      # слой совместимости Qt5/Qt6
│   ├── partition.py      # парсер MBR/GPT
│   ├── fat32.py          # собственный драйвер FAT32 (чтение + запись)
│   ├── vfs.py            # read-only доступ к ФС через pytsk3 (TSK)
│   ├── hexview.py        # HEX-просмотрщик
│   └── mainwindow.py     # главное окно, дерево образа, ленивая загрузка
└── tests/
    └── test_fat32.py     # автотесты на синтетических образах
```

## Тесты / Tests

```bash
python -m pytest tests/ -v
# или без pytest:
python tests/test_fat32.py
```

Тесты создают синтетические FAT32/MBR/GPT-образы в temp-каталоге и проверяют round-trip записи/чтения, перезапись, LFN, mkdir, отсутствие утечек FAT-кластеров и корректность парсинга разметки.

## Ограничения / Limitations (roadmap)

- Запись пока только в **FAT32** (FAT12/16, exFAT, EXT, UBIFS — чтение через TSK; запись — в планах).
- eMMC-специфика (OOB/ECC-области NAND-дампов) не обрабатывается — подавайте образ user-раздела без OOB.

## Лицензия / License

MIT, см. [LICENSE](LICENSE).

## Отказ от ответственности / Disclaimer

Инструмент предоставляется «как есть». Всегда держите резервную копию образа, в который пишете.
