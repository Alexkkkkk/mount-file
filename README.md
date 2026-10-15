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

- 🔍 **Разметка**: автодетекция MBR и GPT, выбор раздела из списка; если таблицы разделов нет (типично для дампов NAND/eMMC) — поиск ФС по сигнатурам в сыром потоке (FAT, NTFS, exFAT, ext2/3/4, ISO9660, HFS+, squashfs, UBIFS, JFFS2) с проверкой кандидатов.
- 📂 **Просмотр**: дерево файлов любой ФС, поддерживаемой The Sleuth Kit — FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 (через `pytsk3`). Если `pytsk3` не установлен, работает встроенный движок FAT32.
- 📤 **Извлечение** файлов и **целых каталогов** (с подкаталогами) из образа на диск.
- ✏️ **Запись в FAT32** (собственный движок, без внешних зависимостей):
  - создание и перезапись файлов (включая файлы больше кластера),
  - длинные имена (LFN) и короткие 8.3,
  - создание каталогов (`mkdir`), **переименование** (`mv`) и **удаление** файлов/пустых каталогов (`rm`) с корректным освобождением кластеров,
  - корректная аллокация/освобождение кластеров (проверено тестами на утечки).
- 🧮 **HEX-просмотр** первого мегабайта выбранного файла, выделение мышью.
- ⌨️ **CLI** (`python -m mountfile`) для скриптов и сборки образов без графики.
- 🪟 **Windows 7** поддерживается через слой совместимости (PyQt5).

## Установка / Installation

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
pip install -r requirements.txt        # Win10+/Linux/macOS (PyQt6)
```

> `pytsk3` предоставляет готовые wheel для Windows/Linux/macOS. Если на вашей платформе wheel нет — соберите libtsk из исходников. Без `pytsk3` программа всё равно запускается и умеет читать/писать FAT32 встроенным движком.

## Запуск / Usage

### Графический интерфейс

```bash
python main.py                 # затем File → Open image…
python main.py dump.img        # сразу открыть образ
python main.py --diag          # диагностика окружения (что установлено)
```

1. **Open image…** — выберите raw-образ (`.img`, `.bin`, `.dump`…).
2. В списке **Volume** выберите раздел (или «Whole image»).
3. Двойной клик по файлу — извлечь; кнопка **Extract…** — то же самое (для каталога выбирается папка назначения и выгружается всё поддерево).
4. Если том — FAT32, активируются **Import file…**, **New folder**, **Rename** и **Delete**.

> ⚠️ **Внимание**: запись изменяет файл образа на диске. Делайте бэкап перед импортом/удалением.

### Командная строка

```bash
python -m mountfile volumes dump.img                    # список томов
python -m mountfile info dump.img                       # сводка по образу
python -m mountfile ls dump.img /photos                 # листинг каталога
python -m mountfile get dump.img /boot -o ./out         # выгрузить файл/каталог

python -m mountfile put dump.img hello.txt --dest /docs --yes    # записать файл
python -m mountfile mkdir dump.img /docs/new
python -m mountfile mv  dump.img /docs/old.txt new.txt
python -m mountfile rm  dump.img /docs/old.txt --yes
```

Том выбирается автоматически (первый распознанный), либо явно: `--volume 2` (номер из `volumes`) или `--offset 0x400000`. Ключ `--backup` делает копию `<image>.bak` перед записью. Разрушающие операции (`put`, `rm`) без `--yes` только печатают предупреждение и ничего не меняют.

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

## Сборка автономного бинарника

```bat
build_exe.bat            :: Windows -> dist\MountFile.exe
```
```bash
./build_exe.sh           # Linux x86-64 -> dist/MountFile-linux-x86-64
```

## Архитектура / Architecture

```
mount-file/
├── main.py               # точка входа GUI (PyQt6 → PyQt5 fallback), --diag
├── mountfile/
│   ├── __main__.py       # python -m mountfile
│   ├── cli.py            # командная строка (volumes/ls/get/put/mkdir/mv/rm)
│   ├── qt_compat.py      # слой совместимости Qt5/Qt6
│   ├── partition.py      # парсер MBR/GPT
│   ├── firmware.py       # поиск томов: таблица разделов + сигнатуры ФС
│   ├── fat32.py          # собственный драйвер FAT32 (чтение, запись, rm, mv)
│   ├── extract.py        # выгрузка файлов и каталогов из образа
│   ├── vfs.py            # read-only доступ к ФС через pytsk3 (TSK)
│   ├── hexview.py        # HEX-просмотрщик
│   └── mainwindow.py     # главное окно, дерево образа, ленивая загрузка
├── build_exe.bat         # PyInstaller, Windows
├── build_exe.sh          # PyInstaller, Linux
├── .github/workflows/    # CI: тесты + smoke-тест GUI
└── tests/
    ├── test_fat32.py         # FAT32 + разметка, синтетические образы
    ├── test_fat32_write.py   # удаление, переименование, провайдер FAT32
    ├── test_firmware.py      # поиск томов по таблице и по сигнатурам
    └── test_gui_smoke.py     # модель дерева и главное окно (offscreen)
```

## Тесты / Tests

```bash
python -m pytest tests/ -v
# или без pytest:
python tests/test_fat32.py
python tests/test_firmware.py
python tests/test_fat32_write.py
```

Тесты создают синтетические FAT32/MBR/GPT-образы в temp-каталоге и проверяют round-trip записи/чтения, перезапись, LFN, mkdir, переименование, удаление (с освобождением кластеров), отсутствие утечек и корректность парсинга разметки и сигнатур ФС. Smoke-тесты GUI требуют PyQt6/PyQt5 и без него пропускаются. CI (GitHub Actions) прогоняет тесты на Linux и Windows; GUI-тест запускается в режиме `offscreen`.

## Ограничения / Limitations (roadmap)

- Запись пока только в **FAT32** (FAT12/16, exFAT, EXT, UBIFS — чтение через TSK; запись — в планах).
- Удаление каталога возможно только когда он пуст.
- eMMC-специфика (OOB/ECC-области NAND-дампов) не обрабатывается — подавайте образ user-раздела без OOB.

## Лицензия / License

MIT, см. [LICENSE](LICENSE).

## Отказ от ответственности / Disclaimer

Инструмент предоставляется «как есть». Всегда держите резервную копию образа, в который пишете.
