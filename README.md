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

# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows Command Prompt
.venv\Scripts\activate.bat

Установите зависимости и запустите программу:

python -m pip install -r requirements.txt
python main.py

Чтобы сразу открыть образ, передайте путь к нему:

python main.py path/to/image.img

В окне выберите образ и раздел (или весь образ). Двойной щелчок открывает папку или файл; для FAT32 доступны **Import file…** и **New folder**.

> **Важно:** запись изменяет файл образа. Перед импортом и созданием папок сделайте резервную копию; безопаснее работать с копией образа.

## Командная строка

Команды доступны и через `python main.py`, и через собранный бинарник;
для CLI установка Qt не требуется. Все пути ниже — примеры.

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
python -m pip install -r requirements-win7.txt
python main.py path\to\image.img

Чтобы собрать автономный `dist\MountFile.exe`, оставив это окружение активным, запустите:

build_exe.bat

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

python main.py --version
python main.py --diag
python main.py --check-updates

## Разработка и тесты

Для тестов и проверки стиля установите инструменты разработчика:

python -m pip install pytest ruff
python -m pytest tests/ -v
python -m ruff check . --select E9,F63,F7,F82

Тесты используют синтетические образы и проверяют FAT32, таблицы разделов, пользовательский интерфейс и автообновление.

Основные части проекта:

| Путь | Назначение |
| `main.py` | Запуск приложения и диагностика |
| `mountfile/partition.py` | Чтение таблиц MBR/GPT |
| `mountfile/vfs.py` | Просмотр и извлечение через The Sleuth Kit |
| `mountfile/fat32.py` | Чтение и запись FAT32 |
| `mountfile/tc_window.py` | Основное двухпанельное окно |
| `mountfile.py` | Отдельный одностраничный инструмент: монтирование образов (GUI + CLI) |
| `tests/` | Автоматические тесты |


### Единый запуск всего функционала

`mountfile_all.py` прогоняет весь функционал проекта за один запуск: окружение и
версию, CLI-команды, Python API и тесты, а по флагу `--build` — ещё и сборку
бинарника.

python mountfile_all.py                     # CLI + API + тесты + GUI
python mountfile_all.py --no-gui --no-tests # без интерфейса и тестов
python mountfile_all.py --cli-only          # только CLI-команды
python mountfile_all.py --api-only          # только Python API
python mountfile_all.py --tests-only        # только тесты
python mountfile_all.py --diag              # диагностика main.py --diag
python mountfile_all.py --check-updates     # 10 = есть обновление, 0 = нет
python mountfile_all.py --build             # + сборка бинарника
python mountfile_all.py --cheatsheet        # карта команд и переменных окружения

Скрипт принимает `--img`, `--dump` и `--udev` (по умолчанию `dump.img`,
`dump.bin`, `udev.txt`). Если файла нет, соответствующий раздел пропускается с
предупреждением, а не завершается ошибкой.

Через `make`:

make all-in-one                 # python mountfile_all.py --no-gui
make all-in-one ARGS=--build    # то же плюс сборка бинарника
make sample                     # тестовые образы для полного прогона

Тестовые образы (нужны пакеты `dosfstools` и `mtools`) собирает
`scripts/make_sample_image.sh`: образ FAT32 через `mkfs.vfat` + `mcopy`, дамп с
маркером `MBOT` и флагом `JC` по смещению `0x200090` и таблицу `[PARTITIONS]`
для команд `decode` и `split`.

make sample          # → dump.img, dump.bin, udev.txt
python mountfile_all.py --no-gui   # теперь ни один раздел не пропускается
## Отдельный инструмент: `mountfile.py`

`mountfile.py` — самостоятельный одностраничный вариант Mount File, решающий
одну задачу: смонтировать образ диска (`.img`, `.iso`, `.bin`, `.raw`, `.vhd`,
`.vhdx`) и отдать содержимое системе. Qt для CLI не нужен, интернет не
используется, телеметрии нет.

python mountfile.py                      # графический интерфейс
python mountfile.py dump.img --to /mnt/x # монтирование без интерфейса
python mountfile.py dump.img --open      # смонтировать и открыть в файловом менеджере
python mountfile.py --list               # что смонтировано сейчас
python mountfile.py --unmount /mnt/x     # отмонтировать
python mountfile.py --version

В графическом режиме доступны перетаскивание файла в окно, значок в системном
лотке, палитра команд по `Ctrl+K`, темы system/light/dark и журнал операций.
Состояние, список последних образов и журнал хранятся в каталоге настроек
пользователя (`%APPDATA%`, `~/Library/Application Support`, `~/.config`);
запись атомарная, поэтому обрыв питания не портит файл состояния.

Платформенные механизмы монтирования:

| Система | Механизм | Права |
|---|---|---|
| Linux | `udisksctl` (loop-setup + mount) или `mount -o loop,ro` | без root при наличии udisks2 |
| macOS | `hdiutil attach` / `detach` | без sudo |
| Windows | PowerShell `Mount-DiskImage` (ISO/VHD/VHDX) | без прав администратора |

Сборка одного исполняемого файла:

make mountfile-exe            # то же, через Makefile
pyinstaller --onefile --windowed --name mountfile mountfile.py

Тесты этого модуля запускаются отдельно и не требуют ни `pytsk3`, ни дисплея:

make test-standalone
python -m pytest tests/test_mountfile_standalone.py -v

## Ограничения

- Запись поддерживается только для FAT32. Остальные файловые системы доступны для просмотра и извлечения, если их поддерживает установленная сборка `libtsk`.
- Данные OOB/ECC в сырых NAND-дампах не обрабатываются. Для таких устройств нужен образ пользовательской области без этих данных.
- Приложение предоставляется «как есть». Всегда сохраняйте резервную копию образа, в который записываете.

## Лицензия

[MIT](LICENSE).

Файловый менеджер для raw-образов дисков и дампов NAND/eMMC. Позволяет просматривать образы, извлекать файлы и изменять файлы в разделах FAT32 — без записи образа обратно на устройство.

A file manager for raw disk images and NAND/eMMC dumps. Browse images, extract files, and modify files in FAT32 partitions without flashing the image back to the device.

> ⚠️ Запись изменяет файл образа на диске. Перед импортом файлов сделайте резервную копию.

## Содержание

- [Скачать](#скачать)
- [Возможности](#возможности)
- [Быстрый старт](#быстрый-старт)
- [Использование](#использование)
- [Командная строка](#командная-строка)
- [Windows 7](#windows-7)
- [Автообновление](#автообновление)
- [Интерфейс и клавиши](#интерфейс-и-клавиши)
- [Разработка и тесты](#разработка-и-тесты)
- [Ограничения](#ограничения)
- [Лицензия](#лицензия)
Список версий и файлов: [GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases).

Ссылки ниже ведут на сборки **последнего** релиза, поэтому их не нужно
править при выпуске новой версии. К каждому `.exe` приложен файл `.sha256`
с контрольной суммой.

| Платформа | Сборка |
| Windows 10/11 x64 | [MountFile-win10-x64.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-win10-x64.exe) |
| Windows 7 x64 | [MountFile-win7-x64.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-win7-x64.exe) |
| Windows, универсальная сборка | [MountFile.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile.exe) |
| Linux x86-64 | [MountFile-linux-x86-64](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-linux-x86-64) |

Универсальная сборка `MountFile.exe` собрана на Python 3.8 + PyQt5 и
работает на Windows 7 SP1 и новее (x64). Именно её выбирает автообновление.
- **Разметка диска:** автоматическое определение MBR и GPT, выбор раздела или всего образа.
- **Просмотр файлов:** через The Sleuth Kit (`pytsk3`) для FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+ и ISO9660.
- **Извлечение:** копирование файлов из образа на диск.
- **Запись в FAT32:** создание и перезапись файлов, длинные имена (LFN) и имена 8.3, создание каталогов, выделение и освобождение кластеров.
- **HEX-просмотр:** первые 1 МБ выбранного файла.
- **Интерфейс в стиле Total Commander:** две панели, вкладки каталогов, светлая и тёмная темы, массовые операции с файлами.
  Колонки Имя / Тип / Размер / Дата / Атрибуты: «Имя» занимает свободное место, у папок тип — «Папка»,
  размер — `<Папка>`, атрибуты — `----c`; длинные имена обрезаются по центру, полное имя видно в подсказке.
- **Windows 7:** поддерживается через слой совместимости с PyQt5.

## Быстрый старт
pip install -r requirements.txt
Чтобы сразу открыть образ:

python main.py dump.img

Для Windows 10/11 и Linux/macOS используется PyQt6 из `requirements.txt`. Инструкции для Windows 7 приведены ниже.

## Использование

1. Откройте образ через **File → Open image…**.
2. В списке **Volume** выберите раздел или **Whole image**.
3. Дважды щёлкните файл, чтобы открыть его в HEX-просмотрщике; для извлечения используйте **Extract…**.
4. Для тома FAT32 доступны **Import file…** и **New folder**.

Импорт изменяет исходный файл образа. Перед записью сохраните его резервную копию.
Для Windows 7 используется та же кодовая база через `mountfile/qt_compat.py`: PyQt6 не поддерживает Windows 7, поэтому нужны PyQt5 и Python 3.8.10.
py -3.8 -m pip install -r requirements-win7.txt
py -3.8 main.py dump.img
Последняя команда собирает автономный `dist\MountFile.exe`.

На Python 3.8 `pytsk3` не устанавливается: PyPI публикует её колёса для
Windows только начиная с Python 3.10, поэтому `requirements-win7.txt`
пропускает эту зависимость на 3.8. Сборка для Windows 7 работает на
встроенном движке FAT32 — просмотр, извлечение и запись FAT32 доступны,
чтение NTFS/EXT требует `pytsk3` и на 3.8 недоступно.

При запуске программа в фоновом потоке проверяет GitHub Releases. Если доступна более новая версия, приложение предлагает обновление. В собранном `.exe` обновление скачивается, проверяется по SHA-256, устанавливается и запускает программу заново. Если сеть недоступна или URL некорректен, программа продолжает работу.

Проверка из командной строки:


Код возврата: `10` — найдено обновление, `0` — обновлений нет.

| Переменная окружения | Действие |
| `MOUNTFILE_UPDATE_REPO=owner/repo` | Использовать другой репозиторий с релизами |
| `MOUNTFILE_AUTO_UPDATE=1` | Устанавливать обновление и перезапускаться без подтверждения |
| `MOUNTFILE_GIT_PULL=1` | Разрешить `git pull --ff-only` при запуске из исходников |
| `MOUNTFILE_UPDATE_URL=…` | Использовать прямой URL JSON последнего релиза |

Проверка из Python:

```python
from mountfile import __version__, updater
updater.check(__version__)

## Интерфейс и клавиши

| Клавиши | Действие |
| `Enter` / двойной щелчок | Открыть каталог или просмотреть файл в HEX |
| `Backspace` | Перейти на уровень вверх |
| `Space` / `Insert` | Отметить файл |
| `Ctrl+A` | Выделить всё |
| `Tab` / `Ctrl+U` | Переключить панель / поменять панели местами |
| `Ctrl+T` / `Ctrl+W` | Создать / закрыть вкладку |
| `F3` / `F4` | Просмотр / редактирование (редактирование файлов внутри образа недоступно) |
| `F5` / `F6` | Копировать / переместить между панелями |
| `F7` / `F8` | Создать каталог / удалить локальные файлы |
| `F9` / `Ctrl+L` | Показать или скрыть меню / журнал окна |
| `Ctrl+R` / `Ctrl+O` | Обновить панель / открыть образ |

Отмеченные файлы можно копировать, перемещать и удалять группами. Каталоги копируются целиком; запись в образ выполняется движком FAT32. Настройки темы, размера шрифта и журнала доступны в **Конфигурация → Настройки**.
Запуск полного набора тестов:


<<<<<<< HEAD
Тесты проверяют FAT32, пользовательский интерфейс, запуск приложения и обновление; для проверки FAT32 без `pytest`:
=======
>>>>>>> origin/main

python tests/test_fat32.py

Основные модули:

```text
mount-file/
├── main.py
├── mountfile/
│   ├── qt_compat.py
│   ├── partition.py
│   ├── fat32.py
│   ├── vfs.py
│   ├── hexview.py
│   ├── updater.py
│   ├── mainwindow.py
│   ├── tc_window.py
│   └── tc_style.py
└── tests/
    ├── smoke_startup.py
    ├── test_fat32.py
    ├── test_tc_ui.py
    └── test_updater.py

- Запись поддерживается только для **FAT32**. FAT12/16, exFAT и другие перечисленные файловые системы доступны для чтения через The Sleuth Kit.
- Области OOB/ECC в дампах eMMC/NAND не обрабатываются. Используйте образ пользовательского раздела без OOB.
MIT. См. файл [LICENSE](LICENSE).

## Отказ от ответственности

Программа предоставляется «как есть». Всегда храните резервную копию образа, в который записываете данные.
