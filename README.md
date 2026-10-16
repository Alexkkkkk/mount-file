<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
# Mount File

Графический файловый менеджер для просмотра raw-образов NAND, eMMC и дисков. Позволяет извлекать файлы, а в образах FAT32 — создавать и перезаписывать файлы и папки.

> **English:** Mount File is a desktop file manager for browsing and extracting files from raw disk images. It can also create or replace files and folders in FAT32 images.

## Скачать

Откройте страницу [GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases). Готовые сборки доступны в разделе **Assets**, если они приложены к выбранному релизу. Если бинарников нет, установите приложение из исходников по инструкции ниже.

## Возможности

- Открытие raw-образов и обнаружение разделов MBR/GPT.
- Просмотр и извлечение файловых систем, поддерживаемых The Sleuth Kit (`pytsk3`): FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 и других — в зависимости от сборки `libtsk`.
- **Дампы RT809H:** расшифровка зашифрованных дампов eMMC плат MStar (MST) и MediaTek (MTK) по XOR-таблицам (таблицы `XOR_MST_00.bin` / `XOR_MTK_00.bin` встроены), разбиение дампа на партиции по файлу `udev` (секции `[PARTITIONS]`, переводы строк Windows/Unix/Mac).
- Создание и перезапись файлов, длинные имена (LFN), имена 8.3 и создание папок в FAT32.
- Двухпанельный интерфейс и HEX-просмотр первых 1 МБ выбранного файла.
- Автоматическая проверка обновлений GitHub Releases при запуске.
- Windows 7 через Python 3.8 и PyQt5; Windows 10/11, Linux и macOS — через PyQt6.

## Установка и запуск

Для Windows 10/11, Linux и macOS установите Python, клонируйте репозиторий и зависимости:

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
python -m venv .venv
```

Активируйте окружение:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows Command Prompt
.venv\Scripts\activate.bat
```

Установите зависимости и запустите программу:

```bash
python -m pip install -r requirements.txt
python main.py
```

Чтобы сразу открыть образ, передайте путь к нему:

```bash
python main.py path/to/image.img
```

В окне выберите образ и раздел (или весь образ). Двойной щелчок открывает папку или файл; для FAT32 доступны **Import file…** и **New folder**.

> **Важно:** запись изменяет файл образа. Перед импортом и созданием папок сделайте резервную копию; безопаснее работать с копией образа.

## Командная строка

Команды доступны и через `python main.py`, и через собранный бинарник;
для CLI установка Qt не требуется. Все пути ниже — примеры.

```bash
python -m mountfile --help
python main.py volumes dump.img
python main.py ls dump.img /
python main.py get dump.img /photos -o ./out
python main.py put dump.img ./hello.txt --dest / --yes
python main.py mkdir dump.img /new
python main.py mv dump.img /hello.txt renamed.txt
python main.py rm dump.img /new --recursive --yes
python main.py decode dump.bin                # расшифровка дампа RT809H (MST/MTK) -> dump.bin_decode.bin
python main.py split dump.bin udev.txt -o ./parts   # разбиение дампа на партиции по udev
```

Без `pytsk3` CLI и классическое дерево используют встроенный провайдер FAT32.
Запись, переименование и удаление поддерживаются только на FAT32. Команды
`put` и `rm` требуют `--yes`; перед изменением образа сделайте резервную копию.
`mv` переименовывает файл в том же каталоге, а не переносит между томами.

Сборка Linux: `./build_exe.sh`; результат — `dist/MountFile-linux-x86-64`.
Для запуска Qt на Linux могут потребоваться системные пакеты `libgl1`,
`libegl1`, `libxkbcommon-x11-0`, `libdbus-1-3` и `libxcb-cursor0`.

## Windows 7

Для Windows 7 используйте Python 3.8.10 и PyQt5:

```bat
py -3.8 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements-win7.txt
python main.py path\to\image.img
```

Чтобы собрать автономный `dist\MountFile.exe`, оставив это окружение активным, запустите:

```bat
build_exe.bat
```

## Автообновление

При запуске приложение проверяет GitHub Releases в фоновом потоке. Отсутствие сети не блокирует работу. В исходниках программа показывает ссылку на релиз; готовая сборка может скачать подходящий файл и установить обновление.

Поведение можно настроить переменными окружения:

| Переменная | Действие |
|---|---|
| `MOUNTFILE_NO_UPDATE=1` | Отключить проверку обновлений |
| `MOUNTFILE_AUTO_UPDATE=1` | Устанавливать найденное обновление без подтверждения |
| `MOUNTFILE_UPDATE_REPO=owner/repo` | Проверять релизы другого репозитория |
| `MOUNTFILE_UPDATE_URL=https://…` | Использовать прямой URL на JSON последнего релиза |
| `MOUNTFILE_GIT_PULL=1` | Разрешить `git pull --ff-only` при запуске из git-репозитория |

Полезные команды:

```bash
python main.py --version
python main.py --diag
python main.py --check-updates
```

## Разработка и тесты

Для тестов и проверки стиля установите инструменты разработчика:

```bash
python -m pip install pytest ruff
python -m pytest tests/ -v
python -m ruff check . --select E9,F63,F7,F82
```

Тесты используют синтетические образы и проверяют FAT32, таблицы разделов, пользовательский интерфейс и автообновление.

Основные части проекта:

| Путь | Назначение |
|---|---|
| `main.py` | Запуск приложения и диагностика |
| `mountfile/partition.py` | Чтение таблиц MBR/GPT |
| `mountfile/vfs.py` | Просмотр и извлечение через The Sleuth Kit |
| `mountfile/fat32.py` | Чтение и запись FAT32 |
| `mountfile/tc_window.py` | Основное двухпанельное окно |
| `tests/` | Автоматические тесты |

## Ограничения

- Запись поддерживается только для FAT32. Остальные файловые системы доступны для просмотра и извлечения, если их поддерживает установленная сборка `libtsk`.
- Данные OOB/ECC в сырых NAND-дампах не обрабатываются. Для таких устройств нужен образ пользовательской области без этих данных.
- Приложение предоставляется «как есть». Всегда сохраняйте резервную копию образа, в который записываете.

## Лицензия

[MIT](LICENSE).
