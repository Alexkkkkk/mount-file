<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
[![CI](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml)
[![Quality](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

# Mount File

Файловый менеджер для raw-образов дисков и дампов NAND/eMMC: просмотр и
извлечение файлов, чтение таблиц разделов, а в образах FAT32 — создание,
перезапись и удаление файлов и папок. Работает с файлом образа и никогда не
пишет его обратно на устройство.

> **English:** Mount File is a desktop file manager for raw disk images and
> NAND/eMMC dumps — browse and extract files, read MBR/GPT partition tables,
> and create/replace files and folders in FAT32 images. Everything happens in
> the image file; nothing is flashed back to the device.

Проект состоит из трёх частей:

- **`main.py` + пакет `mountfile/`** — основное приложение: GUI в стиле
  Total Commander, командная строка, авто-подбор тома, работа с дампами RT809H.
- **`mountfile.py`** — автономный одностраничный инструмент, который монтирует
  образ в систему (GUI + CLI, без `pytsk3` и без сети).
- **Подсистемы `mountfile/olma/`, `mountfile/ai/`, `mountfile/turbo.py`** —
  самоанализ и автоисправление кода, анализ файлов через LLM, внутренние
  утилиты (см. [Ограничения](#ограничения)).

## Содержание

- [Возможности](#возможности)
- [Требования](#требования)
- [Установка](#установка)
- [Запуск](#запуск)
- [Командная строка](#командная-строка)
- [Горячие клавиши](#горячие-клавиши)
- [Автономный инструмент `mountfile.py`](#автономный-инструмент-mountfilepy)
- [Настройки и окружение](#настройки-и-окружение)
- [AI-анализ файлов](#ai-анализ-файлов)
- [OLMA](#olma)
- [GUI в GitHub Codespaces](#gui-в-github-codespaces)
- [Сборка](#сборка)
- [Тесты и разработка](#тесты-и-разработка)
- [Структура проекта](#структура-проекта)
- [Ограничения](#ограничения)
- [Лицензия](#лицензия)

## Возможности

- **Разметка диска:** определение MBR и GPT, выбор раздела или всего образа,
  чтение смещений из файла `udev`.
- **Просмотр и извлечение:** через The Sleuth Kit (`pytsk3`) —
  FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 и другие файловые
  системы, поддерживаемые установленной сборкой `libtsk`.
- **Запись в FAT32:** создание и перезапись файлов, длинные имена (LFN) и
  имена 8.3, создание каталогов, переименование и удаление, выделение и
  освобождение кластеров — собственным движком (`mountfile/fat32.py`).
- **Авто-подбор рабочего тома:** поиск реальной файловой системы в дампах, где
  таблица разделов отсутствует или указывает на контейнер (см.
  [авто-подбор](#авто-подбор-рабочего-тома)).
- **Дампы RT809H:** расшифровка зашифрованных дампов eMMC MStar (MST) и
  MediaTek (MTK) по встроенным XOR-таблицам и разбиение дампа на партиции по
  файлу `udev`.
- **HEX-просмотр** первых 1 МБ выбранного файла.
- **Двухпанельный интерфейс** в стиле Total Commander: вкладки каталогов,
  отметка файлов, массовые операции, светлая и тёмная темы, журнал окна.
  Колонки Имя / Тип / Размер / Дата / Атрибуты: «Имя» занимает свободное место,
  у папок тип — «Папка», размер — `<Папка>`, атрибуты — `----c`; длинные имена
  обрезаются по центру и видны целиком в подсказке.
- **AI-анализ файла** через локальную Ollama или любой OpenAI-совместимый
  эндпоинт — кнопка «AI» на панели инструментов.
- **Автообновление** из GitHub Releases: проверка при запуске, SHA-256,
  индикатор хода в строке состояния.
- **Windows 7** через Python 3.8.10 + PyQt5 (`mountfile/qt_compat.py`);
  Windows 10/11, Linux и macOS — через PyQt6.

## Требования

| Компонент | Версия |
|---|---|
| Python | 3.8+ (рекомендуется 3.10–3.12) |
| Qt | PyQt6 ≥ 6.11 (Windows 10/11, Linux, macOS) или PyQt5 5.15 (Windows 7) |
| `pytsk3` | необязательно; нужен для чтения NTFS/EXT и прочих ФС |
| Системные инструменты (по желанию) | `dosfstools`, `mtools` — только для сборки тестовых образов |

На Python 3.8 `pytsk3` не устанавливается: PyPI публикует колёса для Windows
начиная с Python 3.10, поэтому в `requirements-win7.txt` зависимость помечена
маркером `python_version >= "3.10"`. Без `pytsk3` приложение работает на
встроенном движке FAT32.

## Установка

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
python -m venv .venv
```

Активируйте окружение:

```bash
source .venv/bin/activate        # Linux / macOS
```

```powershell
.venv\Scripts\Activate.ps1       # Windows PowerShell
```

```bat
.venv\Scripts\activate.bat       REM Windows Command Prompt
```

Установите зависимости и запустите приложение:

```bash
python -m pip install -r requirements.txt
python main.py
```

Чтобы сразу открыть образ, передайте путь к нему:

```bash
python main.py path/to/image.img
```

Готовые сборки лежат на странице
[GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases); к каждому
`.exe` приложен файл `.sha256` с контрольной суммой.

### Windows 7

```bat
py -3.8 -m venv .venv
python -m pip install -r requirements-win7.txt
python main.py path\to\image.img
```

Сборка автономного `dist\MountFile.exe` (при активном окружении Win7):

```bat
build_exe.bat
```

### Системные пакеты Qt (Linux)

Для запуска Qt могут понадобиться `libgl1`, `libegl1`, `libxkbcommon-x11-0`,
`libdbus-1-3` и `libxcb-cursor0`. Скрипт `scripts/setup-gui.sh` устанавливает
их вместе с Xvfb/x11vnc/noVNC для окружения Codespaces.

## Запуск

```bash
python main.py                 # графический интерфейс
python main.py dump.img        # открыть образ в окне
python main.py --version
python main.py --diag          # пошаговая проверка окружения
python main.py --check-updates # разовый запрос к GitHub Releases (код 10 = есть обновление)
```

В окне выберите образ (**File → Open image…**), затем том в списке **Volume**
(раздел или **Whole image**). Двойной щелчок открывает папку или файл в
HEX-просмотрщике, кнопка **Extract…** извлекает файл на диск. Для тома FAT32
доступны **Import file…** и **New folder**.

> ⚠️ **Импорт, создание и удаление изменяют сам файл образа.** Работайте с
> копией образа или сделайте резервную копию заранее.

## Командная строка

CLI живёт в `mountfile/cli.py` и доступен и через `python main.py`, и через
`python -m mountfile`; Qt для него не требуется. Для сборки Windows 7
используйте `python main.py`.

```text
python -m mountfile {volumes,info,ls,get,put,mkdir,mv,rm,decode,split,autopick} …
```

| Команда | Назначение |
|---|---|
| `volumes` | список найденных томов |
| `info` | сводка по образу |
| `ls` | содержимое каталога внутри образа |
| `get` | извлечь файл или каталог |
| `put` | записать локальные файлы в образ (**FAT32**) |
| `mkdir` | создать каталог (**FAT32**) |
| `mv` | переименовать запись в том же каталоге (**FAT32**) |
| `rm` | удалить файл или каталог (**FAT32**) |
| `decode` | расшифровать дамп RT809H (MST/MTK) |
| `split` | разбить дамп на партиции по файлу `udev` |
| `autopick` | автоматически подобрать рабочий том |

Общие аргументы целей (volumes/info/ls/get/put/mkdir/mv/rm/autopick):

| Аргумент | Действие |
|---|---|
| `-V`, `--volume N` | номер тома в нумерации `volumes` (с 1) |
| `-O`, `--offset 0x400000` | прямой байтовый адрес файловой системы |
| `--scan-limit` | сколько байт сканировать в поиске сигнатур ФС |
| `--auto` | подобрать том автоматически (как команда `autopick`) |
| `--backup` | скопировать образ в `<image>.bak` перед записью |

Команды записи (`put`, `mkdir`, `mv`, `rm`) без `--yes` отказываются
работать. Код возврата: `0` — успех, `1` — ошибка, `2` — нужно подтверждение.

### Примеры

```bash
python main.py volumes dump.img
python main.py ls dump.img /
python main.py get dump.img /photos -o ./out
python main.py put dump.img ./hello.txt --dest / --yes
python main.py mkdir dump.img /new --yes
python main.py mv dump.img /hello.txt renamed.txt --yes
python main.py rm dump.img /new --recursive --yes
python main.py decode dump.bin                  # → dump.bin_decode.bin
python main.py split dump.bin udev.txt -o ./parts
```

### Авто-подбор рабочего тома

В дампах NAND/eMMC таблица разделов часто отсутствует или указывает на
контейнер, а не на файловую систему — тогда появляются ошибки
`The Sleuth Kit: FS_Info_Con … FAT32: invalid bytes-per-sector (not a FAT BPB?)`
и `FAT32: image too small`. Модуль `mountfile/autopick.py` решает это без
ручного перебора смещений:

- собирает кандидатов из MBR/GPT, из файла `udev` и из сигнатур файловых систем;
- строго проверяет каждого кандидата (BPB FAT, суперблок ext, NTFS, exFAT,
  ISO9660, HFS+, squashfs, UBIFS, JFFS2) и ищет ФС **внутри** раздела, если по
  его началу ФС нет;
- подтверждает победителя реальным монтированием (pytsk3 или встроенный FAT32);
- ищет файл `udev` рядом с образом автоматически.

```bash
python main.py autopick dump.bin                # таблица кандидатов + выбор
python main.py autopick dump.bin --json         # то же в JSON
python main.py autopick dump.bin --extract ./out
python main.py autopick dump.bin --hint system  # предпочесть том с таким именем/меткой
python main.py ls dump.bin / --auto             # авто-подбор для любой команды
```

В GUI авто-подбор запускается сам, если том не открылся, и вручную — через
меню **Инструменты → Подобрать том автоматически…**.

## Горячие клавиши

| Клавиши | Действие |
|---|---|
| `Enter` / двойной щелчок | открыть каталог или файл в HEX-просмотре |
| `Backspace` | на уровень вверх |
| `Space` / `Insert` | отметить файл |
| `Ctrl+A` | выделить всё |
| `Tab` | другая панель |
| `Ctrl+U` | поменять панели местами |
| `Ctrl+T` / `Ctrl+W` | создать / закрыть вкладку |
| `F3` / `F4` | просмотр / правка |
| `F5` / `F6` | копирование / перемещение между панелями |
| `F7` / `F8` | создать каталог / удаление |
| `F9` | показать или скрыть меню |
| `F11` | полноэкранный режим |
| `Ctrl+O` / `Ctrl+R` | открыть образ / обновить панель |
| `Ctrl+L` | журнал окна |
| `Ctrl+I` | AI: добавить файл для анализа |

Тема, размер шрифта и показ журнала настраиваются в **Конфигурация →
Настройки**.

## Автономный инструмент `mountfile.py`

`mountfile.py` — самостоятельный одностраничный вариант Mount File, решающий
одну задачу: смонтировать образ (`.img`, `.iso`, `.bin`, `.raw`, `.vhd`,
`.vhdx`) и отдать содержимое системе. `pytsk3` не нужен, интернет не
используется, телеметрии нет.

```bash
python mountfile.py                      # графический интерфейс
python mountfile.py dump.img --to /mnt/x # монтирование без интерфейса
python mountfile.py dump.img --open      # смонтировать и открыть в файловом менеджере
python mountfile.py --list               # что смонтировано сейчас
python mountfile.py --unmount /mnt/x     # отмонтировать
python mountfile.py --version
```

В GUI доступны перетаскивание файла в окно, значок в системном лотке, палитра
команд по `Ctrl+K`, темы system/light/dark и журнал операций. Состояние, список
последних образов и журнал хранятся в каталоге настроек пользователя
(`%APPDATA%`, `~/Library/Application Support`, `~/.config`), запись атомарная.

| Система | Механизм монтирования | Права |
|---|---|---|
| Linux | `udisksctl` (loop-setup + mount) или `mount -o loop,ro` | без root при наличии udisks2 |
| macOS | `hdiutil attach` / `detach` | без sudo |
| Windows | PowerShell `Mount-DiskImage` (ISO/VHD/VHDX) | без прав администратора |

## Настройки и окружение

При запуске приложение проверяет GitHub Releases в фоновом потоке; отсутствие
сети не блокирует работу.

| Переменная окружения | Действие |
|---|---|
| `MOUNTFILE_NO_UPDATE=1` | отключить проверку обновлений |
| `MOUNTFILE_AUTO_UPDATE=1` | устанавливать найденное обновление без подтверждения |
| `MOUNTFILE_UPDATE_REPO=owner/repo` | проверять релизы другого репозитория |
| `MOUNTFILE_UPDATE_URL=https://…` | прямой URL на JSON последнего релиза |
| `MOUNTFILE_GIT_PULL=1` | разрешить `git pull --ff-only` при запуске из git-репозитория |
| `MOUNTFILE_OLMA_TOKEN` | токен GitHub для OLMA Watch (см. ниже) |

## AI-анализ файлов

Кнопка **AI** на панели инструментов (или `Ctrl+I`) открывает диалог анализа:
файл берётся из активной панели — в том числе **внутри образа** — или с диска.
Ответ модели дублируется строкой в журнал окна. Слой `mountfile/ai/` не тянет
Qt и не делает сетевых запросов при импорте: обращение к бэкенду происходит
только по явному действию пользователя. Файл читается чанками, лимит
(`ai_max_bytes`, по умолчанию 262 144 байта) защищает от загрузки всего образа
в память.

Поддерживаются `OllamaBackend` (локальный сервер), `OpenAiCompatBackend`
(любой `/v1/chat/completions`) и `NullBackend`, который отвечает понятным
сообщением, если бэкенд не настроен. Настройки — `ai_backend`, `ai_base_url`
(по умолчанию `http://127.0.0.1:11434`), `ai_model`, `ai_api_key`,
`ai_max_bytes`; ключ вводится как пароль и в журнал не пишется.

## OLMA

`mountfile/olma/` — слой самоанализа: статический анализ проекта, трассировка,
автоисправления, отчёты, публикация issue/PR. Запуск через `python main.py --olma`.

```text
python main.py --olma <команда> [опции]
```

| Команда | Назначение |
|---|---|
| `analyze` | статический анализ |
| `fix` | автоисправление (`--dry-run` по умолчанию, `--apply` — применить) |
| `report` | отчёт (`--format text\|json\|md\|html`, `--out FILE`) |
| `trace` | статистика runtime-трассировки |
| `watch` | слежение за ошибками |
| `rules` | список правил |
| `learn`, `train`, `suggest`, `history` | самообучение и подсказки |
| `publish-issue`, `publish-pr` | публикация результатов на GitHub |
| `install`, `uninstall`, `sync` | установка/снятие хуков, синхронизация |

`mountfile/olma_watch.py` перехватывает необработанные исключения и фатальные
сообщения Qt, очищает отчёт от имён пользователя, домашних путей и токенов,
группирует повторы по отпечатку трейсбека и отправляет их в GitHub Issues.
Токен берётся только из `MOUNTFILE_OLMA_TOKEN` (или ключа `olma_token` в
настройках) — он не хранится в репозитории и не попадает в журнал.

## GUI в GitHub Codespaces

Конфигурация `.devcontainer/` поднимает виртуальный дисплей и noVNC:

1. `scripts/setup-gui.sh` — системные пакеты, `.venv`, зависимости, тестовый `test.img`;
2. `scripts/start-gui.sh` — Xvfb + XFCE + x11vnc + noVNC на порту `6080` и запуск Mount File.

Откройте вкладку **Ports → 6080 → Open in browser** и нажмите **Connect**.

## Сборка

```bash
./build_exe.sh                 # Linux → dist/MountFile-linux-x86-64
make exe                       # PyInstaller: MountFile (main.py)
make mountfile-exe             # автономный mountfile.py → dist/mountfile
```

```bat
build_exe.bat                  REM Windows → dist\MountFile.exe
py -3.8 build_exe.bat          REM Windows 7: принудительно Python 3.8
```

Если `pytsk3` не установлена, сборка не падает: бинарник собирается с
встроенным движком FAT32. Релизные сборки собирает workflow `release.yml`
(Windows x64, Windows 7, Linux x86-64, macOS arm64), а версию в
`mountfile/__init__.py` проставляет `scripts/stamp_version.py` из git-тега.

## Тесты и разработка

```bash
python -m pip install -e ".[dev]"     # pytest, ruff, black, isort, pyinstaller
pytest tests/ -v
```

| Действие | Команда |
|---|---|
| все тесты | `make test` — `pytest tests/ -v` |
| тесты автономного `mountfile.py` (без `pytsk3` и дисплея) | `make test-standalone` |
| статическая проверка | `make lint` |
| автоформат (ruff + black + isort) | `make fmt` |
| полный прогон функционала | `make all-in-one` |
| синтетические образы для прогона | `make sample` |
| очистка артефактов | `make clean` |

`mountfile_all.py` прогоняет весь функционал за один запуск — окружение и
версию, CLI-команды, Python API и тесты:

```bash
python mountfile_all.py                     # CLI + API + тесты + GUI
python mountfile_all.py --no-gui            # без интерфейса (работает по SSH и в CI)
python mountfile_all.py --cli-only          # только CLI-команды
python mountfile_all.py --api-only          # только Python API
python mountfile_all.py --tests-only        # только тесты
python mountfile_all.py --cheatsheet        # карта команд и переменных окружения
python mountfile_all.py --build             # + сборка бинарника
```

Скрипт принимает `--img`, `--dump` и `--udev` (по умолчанию `dump.img`,
`dump.bin`, `udev.txt`); если файла нет, соответствующий раздел пропускается с
предупреждением. Тестовые образы собирает `scripts/make_sample_image.sh`
(нужны `dosfstools` и `mtools`): FAT32-образ через `mkfs.vfat` + `mcopy` и дамп
с маркером `MBOT` и флагом `JC` по смещению `0x200090` для `decode` и `split`.

CI (`.github/workflows/ci.yml`) гоняет тесты на Ubuntu и Windows с Python 3.8 и
3.12, отдельное интеграционное задание — с установленной `pytsk3` и `mkfs.vfat`.
Перед коммитом полезно прогнать `make lint && make test`; локально те же
проверки делает `.pre-commit-config.yaml`.

## Структура проекта

```text
mount-file/
├── main.py                     точка входа GUI/CLI + диагностика
├── mountfile.py                автономный инструмент монтирования (GUI + CLI)
├── mountfile_all.py            прогон всего функционала одной командой
├── mountfile_ai.py             отдельный MCP-сервер с агентной обвязкой
├── mountfile_fix.py            патчер/обслуживание дерева пакета
├── mountfile/                  основной пакет
│   ├── cli.py                  подкоманды командной строки
│   ├── partition.py            чтение MBR/GPT
│   ├── vfs.py                  чтение через The Sleuth Kit
│   ├── fat32.py                чтение и запись FAT32
│   ├── autopick.py             авто-подбор рабочего тома
│   ├── extract.py              извлечение файлов и каталогов
│   ├── firmware.py             поиск и проверка файловых систем
│   ├── rt809.py                расшифровка MST/MTK и разбиение по udev
│   ├── hexview.py              HEX-просмотр
│   ├── tc_window.py            основное двухпанельное окно
│   ├── tc_style.py             темы и настройки
│   ├── mainwindow.py           упрощённое окно (используется тестами)
│   ├── qt_compat.py            слой совместимости Qt6/Qt5
│   ├── updater.py              проверка и установка обновлений
│   ├── olma_watch.py           слежение за падениями
│   ├── turbo.py                вспомогательные утилиты (кэш, CDC, журнал)
│   ├── ai/                     бэкенды AI-анализа
│   ├── olma/                   самоанализ, автофикс, отчёты, публикация
│   └── data/                   встроенные XOR-таблицы MST/MTK
├── scripts/                    сборка образов, GUI в Codespaces, стимп версии
├── tests/                      автотесты (pytest)
├── tools/tidy_repo.py          утилита уборки репозитория
├── Makefile                    основные задачи разработки
├── requirements.txt            зависимости Windows 10/11, Linux, macOS
├── requirements-win7.txt       зависимости Windows 7 (Python 3.8 + PyQt5)
└── pyproject.toml              метаданные, ruff/mypy/pytest, semantic-release
```

## Ограничения

- **Запись поддерживается только для FAT32.** Переименование и удаление также
  работают только на FAT32. Остальные файловые системы доступны для просмотра и
  извлечения, если их поддерживает установленная сборка `libtsk`.
- **Без `pytsk3`** (в том числе на Windows 7 с Python 3.8) доступен только
  встроенный движок FAT32; чтение NTFS/EXT недоступно.
- Данные OOB/ECC в сырых NAND-дампах не обрабатываются — нужен образ
  пользовательской области без этих данных.
- `mountfile/turbo.py` (`CachedFS`, CDC-подписи, delta-sync, sqlite-журнал)
  сейчас не подключён к интерфейсу: это библиотека для будущей интеграции.
- Приложение предоставляется «как есть». Всегда сохраняйте резервную копию
  образа, в который записываете.

## Лицензия

[MIT](LICENSE) © 2026 Alexkkkkk.

Вопросы по уязвимостям — по правилам [SECURITY.md](SECURITY.md), без публичных
issue. Порядок работы над изменениями — [CONTRIBUTING.md](CONTRIBUTING.md).
