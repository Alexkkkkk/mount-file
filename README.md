<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
[![CI](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml)
[![Quality](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml)
[![Docs](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml)
[![Security](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml)
[![Docker](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml)
[![Latest release](https://img.shields.io/github/v/release/Alexkkkkk/mount-file?sort=semver&label=release)](https://github.com/Alexkkkkk/mount-file/releases/latest)
[![License: MIT](https://img.shields.io/github/license/Alexkkkkk/mount-file?label=license&color=yellow)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
[![Install](https://img.shields.io/badge/install-one--command-brightgreen)](#%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D0%B0)

# Mount File

**Файловый менеджер для raw-образов дисков и дампов NAND/eMMC.** Просмотр и
извлечение файлов, чтение таблиц разделов MBR/GPT, а в образах FAT32 — создание,
перезапись и удаление файлов и папок. Вся работа идёт с файлом образа: проект
никогда не пишет данные обратно на устройство.

> **English:** Mount File is a desktop file manager for raw disk images and
> NAND/eMMC dumps — browse and extract files, read MBR/GPT partition tables,
> and create/replace/delete files and folders in FAT32 images. Everything
> happens inside the image file; nothing is flashed back to the device.

| | |
|---|---|
| **Актуальный релиз** | [GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases/latest) (к каждому бинарнику приложен `.sha256`) |
| **Документация** | [alexkkkkk.github.io/mount-file](https://alexkkkkk.github.io/mount-file/) |
| **Версия в файлах репозитория** | `mountfile/__init__.py` и `pyproject.toml` согласованы; проверка: `python tools/mfsync.py check --strict` |
| **Лицензия** | [MIT](LICENSE) |
| **Платформы** | Windows 10/11, Linux, macOS |

> **Про версию.** Релизный процесс сначала обновляет обе версии через PR,
> затем после успешного CI в `main` создаёт тег проверенного коммита.
> `scripts/stamp_version.py <tag> --check` сверяет файлы с конкретным тегом.
> Сборки Windows, Linux и macOS получают одинаковую версию из тега.
> Подробности и безопасная локальная синхронизация — [в руководстве](docs/sync.md).

> **Статус проекта.** Issue [#87](https://github.com/Alexkkkkk/mount-file/issues/87) —
> это roadmap, а не заявление о готовой forensic-платформе. Штатно
> поддерживается запись **только в FAT32**; DFIR-команды, AI-слой, YARA,
> carving и универсальный RW-слой имеют экспериментальные или частичные
> реализации. Подробности — в разделе [Ограничения](#ограничения).

## Содержание

- [Состав проекта](#состав-проекта)
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
- [Документация, Docker и demos](#документация-docker-и-demos)
- [Синхронизация кода](#синхронизация-кода)
- [Тесты и разработка](#тесты-и-разработка)
- [Структура проекта](#структура-проекта)
- [Ограничения](#ограничения)
- [Участие в разработке](#участие-в-разработке)
- [Лицензия](#лицензия)

## Состав проекта

Проект состоит из трёх независимых частей:

| Часть | Файлы | Назначение |
|---|---|---|
| **Основное приложение** | `main.py` + пакет `mountfile/` | GUI в стиле Total Commander, командная строка, авто-подбор тома, работа с дампами RT809H, DFIR-команды, AI-слой, OLMA |
| **Автономный инструмент** | `mountfile.py` | Одностраничный скрипт, который монтирует образ в систему (GUI + CLI), без `pytsk3` и без сетевых запросов |
| **Подсистемы пакета** | `mountfile/rw/`, `mountfile/olma/`, `mountfile/ai/`, `mountfile/turbo.py` | Универсальный RW-слой (ext4/debugfs, NTFS/ntfs-3g), самоанализ и автоисправление кода, анализ файлов через LLM, внутренние утилиты (см. [Ограничения](#ограничения)) |

## Возможности

### Работа с образом

- **Разметка диска:** определение MBR и GPT, выбор раздела или всего образа,
  чтение смещений из файла `udev`.
- **Просмотр и извлечение:** через The Sleuth Kit (`pytsk3`) — FAT12/16/32,
  exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 и другие файловые системы,
  поддерживаемые установленной сборкой `libtsk`.
- **Запись в FAT32:** создание и перезапись файлов, длинные имена (LFN) и
  имена 8.3, создание каталогов, переименование и удаление, выделение и
  освобождение кластеров — собственным движком `mountfile/fat32.py`.
- **Авто-подбор рабочего тома:** поиск реальной файловой системы в дампах, где
  таблица разделов отсутствует или указывает на контейнер (см.
  [авто-подбор](#авто-подбор-рабочего-тома)).
- **Сводка образа и томов:** меню **Инструменты → Сводка образа и тома…**
  показывает размер файла, найденные MBR/GPT-разделы, смещения и режим записи
  открытых панелей.
- **Дампы RT809H:** расшифровка зашифрованных дампов eMMC MStar (MST) и
  MediaTek (MTK) по встроенным XOR-таблицам (`mountfile/data/XOR_MST_00.bin`,
  `XOR_MTK_00.bin`) и разбиение дампа на партиции по файлу `udev`.
- **HEX-просмотр** первых 1 МБ выбранного файла.

### Интерфейс

- **Двухпанельный интерфейс** в стиле Total Commander: вкладки каталогов,
  отметка файлов, массовые операции, светлая и тёмная темы, журнал окна.
- Правая кнопка мыши открывает контекстное меню строки: открыть/просмотреть,
  AI-анализ, копировать, переместить, переименовать, удалить, создать папку,
  обновить список и скопировать путь. Правый клик также делает панель активной.
  Операции копирования, перемещения и удаления применяются к группе строк,
  отмеченных `Space`; переименование действует на строку под курсором.
- Колонки Имя / Тип / Размер / Дата / Атрибуты: «Имя» занимает свободное место,
  у папок тип — «Папка», размер — `<Папка>`, атрибуты — `----c`; длинные имена
  обрезаются по центру и видны целиком в подсказке.
- Перед записью в FAT32-образ GUI предлагает создать резервную копию
  `<image>.bak`; при ошибке копирования операция записи отменяется.

### DFIR, AI и служебное

- **Read-only DFIR CLI:** `grep`, `hash`, `timeline`, `timeline-export`,
  `timestomp`, `yara`, `unallocated-scan` и `slack` доступны через
  `python -m mountfile`. Timeline-команды принимают CSV внешнего коллектора;
  YARA использует правила, переданные пользователем (необязательный
  `yara-python`), а unallocated scan — только явно указанные диапазоны.
  `slack --slack-only` принимает смещение последнего кластера, логический
  размер файла и размер кластера от вызывающей стороны: сам CLI не разрешает
  путь файла и не строит цепочку кластеров. Выходной файл создаётся только при
  указанном `--output` и не перезаписывает существующий.
- **Экспериментальные library API:** отдельные модули `mountfile/` включают
  SQLite-индексатор заданных unallocated ranges, read-only
  crypto/Android/macOS/NAND/Registry анализаторы, plugin hooks, REST API и
  MCP/AI helpers. Это библиотечные интерфейсы, а не команды GUI; индексатор не
  определяет allocation map и сам по себе не выполняет carving, а распознавание
  контейнера не расшифровывает его.
- **Метаданные copilot:** `mountfile.ai.copilot` строит ограниченную summary по
  уже извлечённым fingerprint/timeline/anomaly данным; он не открывает образ,
  не читает байты файла и не обращается к сети/LLM.
- **AI-анализ файла** через локальную Ollama или любой OpenAI-совместимый
  эндпоинт — кнопка «AI» на панели инструментов.
- **Автообновление** из GitHub Releases: проверка при запуске, проверка
  SHA-256, индикатор хода в строке состояния.
- **GUI** на PyQt6 для Windows 10/11, Linux и macOS.

## Требования

| Компонент | Версия |
|---|---|
| Python | 3.10+ (`requires-python = ">=3.10"`; CI проверяет 3.10–3.12) |
| Qt | PyQt6 ≥ 6.11 для основного приложения |
| `pytsk3` | обязательная Python-зависимость; чтение NTFS/EXT и прочих ФС |
| REST API (необязательно) | `fastapi`, `uvicorn`, `httpx` |
| AI-слой (необязательно) | `mcp[cli]`, `pydantic`, `openai` |
| Системные пакеты (Linux) | `libgl1`, `libegl1`, `libxkbcommon-x11-0`, `libdbus-1-3`, `libxcb-cursor0` |
| Системные инструменты (по желанию) | `debugfs` для ext (на Windows — `debugfs.exe` или WSL с `e2fsprogs`); `ntfs-3g` для NTFS на Linux; ImDisk Virtual Disk Driver для NTFS/exFAT/FAT12/16 на Windows (нужен запуск от администратора) |

`requirements.txt` — полный набор зависимостей для Python 3.10+. Обычная
установка через `pip install .` включает PyQt6 и `pytsk3`; последние версии
этих зависимостей указаны в `pyproject.toml`. Файлы и сценарии Win7/Python 3.8 сохранены как
legacy-артефакты, но текущий пакет требует Python 3.10+, поэтому этот релиз не
заявляет поддержку Windows 7.

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

Установка как Python-пакета с консольной командой `mountfile`:

```bash
python -m pip install .
mountfile --help
```

### Автоустановка одним скриптом

На Windows распакуйте исходники и дважды щёлкните **`install.bat`**. Скрипт
найдёт Python 3.10+ (при отсутствии установит Python 3.12 через WinGet), создаст
`.venv`, установит Python-зависимости и системные компоненты. Затем запускайте
**`run.bat`**: он использует именно `.venv`, а при первом запуске также умеет
вызвать установку. Если отсутствуют одновременно Python и WinGet, понадобится
установить App Installer или Python вручную; скрипт объяснит причину остановки.

Для готового `.exe` Python не нужен: откройте **Конфигурация → Установить
компоненты…** либо запустите `install-components.bat` из комплекта установщика.
Установщик проверит `debugfs`, при необходимости включит WSL2 и импортирует
отдельный Ubuntu-дистрибутив `MountFileTools` с `e2fsprogs`. Он также установит
ImDisk для записи NTFS/exFAT/FAT12/16. В уже работающем Debian/Ubuntu WSL
достаточно установки `e2fsprogs` в существующий дистрибутив. Загрузка нового
Ubuntu составляет около 325 МБ;
нужны интернет, свободное место и подтверждение UAC. Если Windows попросит
перезагрузку, выполните её самостоятельно и повторите установку. Существующий
WSL-дистрибутив по умолчанию не переключается, образы и диски не изменяются.
После установки переоткройте образ; для ImDisk запустите MountFile от имени
администратора. Само наличие компонентов не разрешает запись без подтверждения
и резервной копии.

`install_mountfile.py` в корне репозитория делает всё сразу: создаёт `.venv`,
ставит системные пакеты, pip-зависимости и сам проект. Работает на Windows,
Linux и macOS — одна команда вместо ручных шагов выше.

```bash
python install_mountfile.py
python install_mountfile.py --minimal     # без AI/API/DFIR
python install_mountfile.py --win7        # завершится сообщением о неподдерживаемом legacy-профиле
python install_mountfile.py --no-sys      # не ставить системные пакеты
python install_mountfile.py --dry-run     # показать команды без запуска
python install_mountfile.py --system-only # только debugfs/WSL/ImDisk/системные пакеты
python install_mountfile.py --skip-imdisk # не устанавливать Windows-драйвер
python install_mountfile.py --skip-ext    # не устанавливать debugfs/WSL
python install_mountfile.py --dev         # дополнительно инструменты разработки
```

| Флаг | Что делает |
|---|---|
| *(без флагов)* | системные пакеты (`apt`/`brew`/WSL/ImDisk), обязательные Qt/pytsk3 и extras `api,ai,dfir` |
| `--minimal` | только обязательные Qt/pytsk3 и `pip install -e .` |
| `--win7` | завершится понятной ошибкой: текущая версия требует Python 3.10+ |
| `--no-sys` | пропустить системные пакеты, WSL и ImDisk |
| `--dry-run` | напечатать команды и ничего не выполнять |

Скрипт идемпотентен: готовый `.venv` переиспользуется, повторный запуск ничего
не ломает. `pytsk3` обязателен; если для платформы нет подходящего wheel или
сборка зависимости не удалась, установка завершится ошибкой.
Проверяются `pip check` и импорты выбранного профиля; ошибка не маскируется
сообщением «Готово». Visual Studio C++ Build Tools не устанавливаются безусловно:
если Windows-зависимость требует сборки из исходников, потребуется совместимый
wheel либо отдельно установленный C++ toolchain. AI-серверы, модели и ключи API
не входят в установку Python-библиотек и настраиваются пользователем отдельно.
В конце печатается команда запуска: `.venv/bin/python main.py` на Linux/macOS
и `.venv\Scripts\python.exe main.py` на Windows.

Для повторного предварительного просмотра из корня исходного репозитория:

```bash
python install_mountfile.py --dry-run
```

Wheel-установка предоставляет команду запуска приложения `mountfile`; отдельная
команда `mountfile-install` в установленном пакете не публикуется, поскольку
системный установщик должен запускаться из исходного checkout.

### Необязательные extras

| Extra | Что добавляет |
|---|---|
| `.[api]` | authenticated REST API и его тестовый клиент (`fastapi`, `uvicorn`, `httpx`) |
| `.[ai]` | MCP/AI helpers (`mcp[cli]`, `pydantic`, `openai`) |
| `.[dfir]` | дополнительные DFIR-модули (`python-dateutil`, `yara-python`) |
| `.[dev]` | `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `black`, `isort`, `pyinstaller` |
| `.[rw]`, `.[rw-ext]`, `.[rw-ntfs]` | универсальный RW-слой; чтение через MountFileBridge, writers для записи устанавливаются отдельно (`debugfs`, WSL/e2fsprogs, `ntfs-3g` или Windows ImDisk) |
| `.[fuzz]` | `atheris` (только Linux) |

```bash
python -m pip install '.[api]'
```

Чтобы сразу открыть образ, передайте путь к нему:

```bash
python main.py path/to/image.img
```

Готовые сборки последнего стабильного релиза лежат на странице
[GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases); к каждому
`.exe` приложен файл `.sha256` с контрольной суммой.

### Windows 7 (legacy, unsupported)

`requirements-win7.txt` и старые build artifacts сохранены для исторической
совместимости, но текущий код требует Python 3.10+ и не поддерживает Windows 7.
См. актуальные требования и ограничения в [руководстве установки](docs/install.md).

### Системные пакеты Qt (Linux)

Для запуска Qt могут понадобиться `libgl1`, `libegl1`, `libxkbcommon-x11-0`,
`libdbus-1-3` и `libxcb-cursor0`. Скрипт `scripts/setup-gui.sh` устанавливает их
вместе с Xvfb/x11vnc/noVNC для окружения Codespaces.

## Запуск

```bash
python main.py                 # графический интерфейс
python main.py dump.img        # открыть образ в окне
python main.py --version       # версия
python main.py --diag          # пошаговая проверка окружения
python main.py --check-updates # разовый запрос к GitHub Releases (код 10 = есть обновление)
python main.py --olma <команда># слой самоанализа OLMA (см. ниже)
```

В окне выберите образ (**File → Open image…**), затем том в списке **Volume**
(раздел или **Whole image**). Двойной щелчок открывает папку или файл в
HEX-просмотрщике, кнопка **Extract…** извлекает файл на диск. Для тома FAT32
доступны **Import file…** и **New folder**.

> ⚠️ **Импорт, создание, переименование и удаление изменяют сам файл образа.**
> Работайте с копией образа или сделайте резервную копию заранее. `--backup`
> создаёт соседний файл `<image>.bak`, но не заменяет независимую резервную
> копию и проверку свободного места.

## Командная строка

CLI живёт в `mountfile/cli.py` и доступен и через `python main.py`, и через
`python -m mountfile`; Qt для него не требуется.

```text
python -m mountfile {volumes,info,ls,get,put,mkdir,mv,rm,decode,split,autopick,
                     grep,hash,timeline,timeline-export,timestomp,yara,
                     unallocated-scan,slack,timeline-build,timeline-query,
                     search,yara-scan,timestomp-scan,slack-extract,live-diff} …
```

### Команды изменения образа (FAT32)

| Команда | Назначение | Запись в образ |
|---|---|---:|
| `put` | записать локальные файлы в образ | **да, FAT32** |
| `mkdir` | создать каталог | **да, FAT32** |
| `mv` | переименовать запись в том же каталоге | **да, FAT32** |
| `rm` | удалить файл или каталог | **да, FAT32** |

Команды записи без `--yes` ничего не делают, показывают предупреждение и
возвращают код **2**. Код возврата: `0` — успех, `1` — ошибка, `2` — нужно
подтверждение, `130` — `Ctrl+C`.

### Чтение, разбор дампов и диагностика

| Команда | Назначение | Запись в образ |
|---|---|---:|
| `volumes` | список найденных томов и смещений | нет |
| `info` | размер, сигнатура и сводка томов | нет |
| `ls` | содержимое каталога внутри образа | нет |
| `get` | извлечь файл или каталог во внешний каталог | нет |
| `autopick` | автоматически подобрать рабочий том | нет |
| `decode` | расшифровать дамп RT809H (MST/MTK) | создаёт новый файл |
| `split` | разбить дамп на партиции по файлу `udev` | создаёт файлы в каталоге вывода |

### Read-only DFIR-команды

| Команда | Назначение |
|---|---|
| `grep` | поиск по сырым байтам образа (`--encoding utf-8\|utf-16le\|latin-1\|raw`) |
| `hash` | расчёт SHA-256 (идентичность образа) |
| `timeline` | печать записей из timeline-CSV |
| `timeline-export` | нормализация меток времени CSV в UTC |
| `timeline-build` | сбор filesystem-aware timeline в SQLite/CSV |
| `timeline-query` | запрос к базе timeline по окну времени и/или типу события |
| `timestomp` | консервативные аномалии меток времени из CSV |
| `timestomp-scan` | аномалии меток времени напрямую из образа |
| `search` | поиск по содержимому файлов или сырым байтам образа |
| `yara` | сканирование файла/образа YARA-правилами из файла |
| `yara-scan` | сканирование файлов и/или unallocated-пространства YARA-правилами |
| `unallocated-scan` | поиск сигнатур файлов в явно указанных диапазонах |
| `slack` | slack последнего кластера по явно заданному extent |
| `slack-extract` | извлечение slack-пространства файлов в каталог |
| `live-diff` | сравнение двух образов (или образа до/после) в Markdown |

`grep` возвращает ограниченные byte offsets и контекст, `hash` вычисляет
SHA-256, а `timeline` и `timestomp` работают с CSV, полученным внешним
коллектором. Эти команды не изменяют исходный образ. Полноценный
filesystem-aware collector, YARA pipeline, carving и slack discovery не
являются частью package CLI. `unallocated-scan` читает только явно переданные
диапазоны. `slack --slack-only` принимает уже подтверждённые смещение последнего
кластера, логический размер файла и размер кластера и не разрешает путь файла и
cluster chain — это должен сделать вызывающий код. Выходной файл создаётся
только при указанном `--output` и не перезаписывает существующий.

Отдельные `mountfile_dfir.py` и `mountfile_grep.py` сохраняются как
экспериментальные standalone-инструменты с расширенными сценариями. Их наличие
не означает полноценного forensic recovery или гарантии chain of custody.

### Общие аргументы целевых команд

Применимы к `volumes`, `info`, `ls`, `get`, `put`, `mkdir`, `mv`, `rm`,
`autopick`:

| Аргумент | Действие |
|---|---|
| `-V`, `--volume N` | номер тома в нумерации `volumes` (с 1) |
| `-O`, `--offset 0x400000` | прямой байтовый адрес файловой системы |
| `--scan-limit` | сколько байт сканировать в поиске сигнатур ФС |
| `--auto` | подобрать том автоматически (как команда `autopick`) |
| `--backup` | скопировать образ в `<image>.bak` перед записью |

### Примеры

```bash
# чтение
python main.py volumes dump.img
python main.py info dump.img
python main.py ls dump.img /
python main.py get dump.img /photos -o ./out
python main.py ls dump.bin /system --auto

# запись (FAT32, обязателен --yes)
python main.py put dump.img ./hello.txt --dest / --backup --yes
python main.py mkdir dump.img /new --yes
python main.py mv dump.img /hello.txt renamed.txt --yes
python main.py rm dump.img /new --recursive --yes

# дампы и DFIR
python main.py decode dump.bin                  # → dump.bin_decode.bin
python main.py split dump.bin udev.txt -o ./parts
python -m mountfile grep dump.img "password=" --encoding utf-8
python -m mountfile hash dump.img
python -m mountfile timeline dump.img --csv timeline.csv
python -m mountfile yara dump.img rules/pii.yar
python -m mountfile unallocated-scan dump.img --ranges ranges.json
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

`--no-verify` быстрее, но может выбрать нерабочее смещение. OOB/ECC сырых
NAND-дампов не обрабатываются. В GUI авто-подбор запускается сам, если том не
открылся, и вручную — через меню **Инструменты → Подобрать том автоматически…**.

## Горячие клавиши

| Клавиши | Действие |
|---|---|
| `Enter` / двойной щелчок | открыть каталог, текстовый файл — в системном редакторе, остальное — в HEX-просмотре |
| `Backspace` | на уровень вверх |
| `Space` / `Insert` | отметить файл |
| `Ctrl+A` | выделить всё |
| `Tab` | другая панель |
| `Ctrl+U` | поменять панели местами |
| `Ctrl+T` / `Ctrl+W` | создать / закрыть вкладку |
| `F3` / `F4` | просмотр / правка: текстовые файлы открываются в системном редакторе, остальное — HEX-просмотр |
| `F5` / `F6` | копирование / перемещение между панелями |
| `F7` / `F8` | создать каталог / удаление |
| `F9` | показать или скрыть меню |
| `F11` | полноэкранный режим |
| `Ctrl+O` / `Ctrl+R` | открыть образ / обновить панель |
| `Ctrl+L` | журнал окна |
| `Ctrl+I` | AI: добавить файл для анализа |

`F4` редактирует текстовые файлы до 2 MiB. Сохранение в образ требует writer:
FAT32 имеет встроенный writer; ext2/3/4 требует `debugfs` (на Windows —
`debugfs.exe` в `PATH` либо WSL с установленным `e2fsprogs`); NTFS использует
`ntfs-3g` на Linux или ImDisk и системный драйвер Windows при запуске от
администратора. На Windows ImDisk также даёт запись в exFAT и FAT12/16.
Перед изменением образа создаётся отдельная резервная копия; если её нельзя
создать, запись отменяется. Файловые системы без доступного writer остаются
только для чтения.

Тема, размер шрифта и показ журнала настраиваются в **Конфигурация → Настройки**.

## Автономный инструмент `mountfile.py`

`mountfile.py` — самостоятельный одностраничный вариант Mount File, решающий
одну задачу: смонтировать образ (`.img`, `.iso`, `.bin`, `.raw`, `.vhd`, `.vhdx`)
и отдать содержимое системе. `pytsk3` не нужен, интернет не используется,
телеметрии нет.

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
| `GH_TOKEN` / `GITHUB_TOKEN` / `MFSYNC_TOKEN` | токен для `tools/mfsync.py` (читается в момент вызова, не хранится) |

## AI-анализ файлов

Кнопка **AI** на панели инструментов (или `Ctrl+I`) открывает диалог анализа:
файл берётся из активной панели — в том числе **внутри образа** — или с диска.
Ответ модели дублируется строкой в журнал окна. Слой `mountfile/ai/` не тянет Qt
и не делает сетевых запросов при импорте: обращение к бэкенду происходит только
по явному действию пользователя. Файл читается чанками, лимит (`ai_max_bytes`,
по умолчанию 262 144 байта) защищает от загрузки всего образа в память.

Поддерживаются `OllamaBackend` (локальный сервер), `OpenAiCompatBackend`
(любой `/v1/chat/completions`) и `NullBackend`, который отвечает понятным
сообщением, если бэкенд не настроен. Настройки — `ai_backend`, `ai_base_url`
(по умолчанию `http://127.0.0.1:11434`), `ai_model`, `ai_api_key`,
`ai_max_bytes`; ключ вводится как пароль и в журнал не пишется. MCP-сервер
лежит в `mountfile/ai/mcp_server.py`, отдельная обвязка — в `mountfile_ai.py`.

## OLMA

`mountfile/olma/` — слой самоанализа: статический анализ проекта, трассировка,
автоисправления, отчёты, публикация issue/PR.

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
| `rules` | список правил (`rules/*.yar`) |
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
```

Если `pytsk3` не установлена, сборка не падает: бинарник собирается с встроенным
движком FAT32. Релизные сборки собирает workflow `release.yml` (Windows x64 / Python 3.10 и 3.12,
Linux x86-64, macOS arm64), а версию в `mountfile/__init__.py`
проставляет `scripts/stamp_version.py` из git-тега.

## Документация, Docker и demos

Структурированные docs доступны на [GitHub Pages](https://alexkkkkk.github.io/mount-file/);
CI собирает их через `mkdocs build --strict`. Для локального просмотра:

```bash
python -m pip install -r requirements-docs.txt
mkdocs serve
```

Короткие записи терминала можно воспроизвести локально через asciinema:

```bash
asciinema play examples/demos/timeline.cast
asciinema play examples/demos/recovery.cast
asciinema play examples/demos/ai-copilot.cast
```

В исходниках есть runners для timeline, synthetic deleted-entry recovery и
metadata-only copilot; prerequisites и границы сценариев описаны в
[`docs/demos.md`](docs/demos.md). Recovery demo не делает file carving и
использует только временный синтетический образ.

Docker build/smoke выполняется в CI. Чтобы опубликовать image в
`ghcr.io/alexkkkkk/mount-file`, владелец запускает **Actions → Docker → Run
workflow** с `publish=true`; push не происходит автоматически на каждом commit.
Публичный pull возможен только после изменения visibility package в GitHub
Packages. До этого можно собрать локально: `docker build -t mountfile:local .`.

## Синхронизация кода

`tools/mfsync.py` — синхронизация рабочей копии с GitHub одной командой: без
сторонних зависимостей (только stdlib + `git`), с autostash-rebase, сканером
секретов, генерацией Conventional Commits и автоматическим переходом на pull
request, если `main` защищён правилами.

```bash
python tools/mfsync.py status        # ветка, ahead/behind, изменения, версии
python tools/mfsync.py sync          # fetch + rebase + commit + push
python tools/mfsync.py sync --pr     # то же через pull request
python tools/mfsync.py watch --push  # авто-синхронизация при сохранении
```

| Что закрывает | Как |
|---|---|
| Забытый `pull` перед работой | `fetch --prune` + `rebase @{u}` в начале `sync` |
| Незакоммиченные правки | `stash --include-untracked`, восстановление после rebase |
| Конфликт в rebase | `rebase --abort`, stash возвращается, список файлов печатается |
| Мусорные сообщения коммитов | тип/скоуп выводятся из diff в формате Conventional Commits |
| Случайный токен или `.env` в коммите | сканер блокирует commit (exit 4) |
| `GH013` от ruleset на `main` | публикация в ветку `sync/*` + авто-PR через REST API |
| Расхождение версий | `status` / `check --strict` сверяют `pyproject.toml`, `__init__.py` и тег |
| Параллельные прогоны | lock-файл `.git/mfsync.lock` |

Токен берётся из `GH_TOKEN` / `GITHUB_TOKEN` / `MFSYNC_TOKEN` в момент вызова и
не попадает ни в `.git/config`, ни в вывод (всё проходит через редакцию
`x-access-token:...@github.com` и `github_pat_*`). Полное описание и рецепты —
[docs/sync.md](docs/sync.md); в CI это проверяет workflow
[mfsync Verify](.github/workflows/mfsync.yml).

```bash
make sync          # pull + commit + push
make sync-status   # статус
make sync-check    # проверка версий для CI
```

## Стиль кода и автоматическое форматирование

Единый стиль поддерживается тремя слоями: локально через `pre-commit`,
автоматически через workflow [autofix](.github/workflows/autofix.yml) и как
гейт в CI. Конфигурация живёт в `pyproject.toml` (`[tool.black]`,
`[tool.isort]` с `profile = "black"`), версии совпадают с rev в
`.pre-commit-config.yaml`.

```bash
make fmt          # ruff --fix, затем isort, затем black (именно такой порядок)
pre-commit run -a # то же локально, перед коммитом
```

| Слой | Что делает | Когда |
|---|---|---|
| `pre-commit` | ruff + isort + black + чистка пробелов/концов строк | при каждом `git commit` |
| `make fmt` | тот же набор одной командой | вручную |
| [autofix.yml](.github/workflows/autofix.yml) | форматирует push в `main` и открывает PR с меткой `auto-merge`, [auto-merge](.github/workflows/auto-merge.yml) сам мержит его после зелёных проверок | включается переменной репозитория `ENABLE_AUTOFIX=true` |
| [Quality](.github/workflows/quality.yml) | `black --check`, `isort --check-only`, ruff, compileall | на каждый push/PR в `main` |

> Порядок `isort → black` важен: с `profile = "black"` оба форматтера
> сходятся, а black всегда оставляет финальный вид файла.

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

`mountfile_all.py` прогоняет доступный функционал за один запуск. Команды над
образом по умолчанию работают без записи; демонстрационные операции записи
включаются только с `--allow-write`:

```bash
python mountfile_all.py                     # CLI + API + тесты + GUI
python mountfile_all.py --no-gui            # без интерфейса (работает по SSH и в CI)
python mountfile_all.py --cli-only          # только CLI-команды
python mountfile_all.py --api-only          # только Python API
python mountfile_all.py --allow-write       # разрешить демонстрационную запись в образ
python mountfile_all.py --tests-only        # только тесты
python mountfile_all.py --cheatsheet        # карта команд и переменных окружения
python mountfile_all.py --build             # + сборка бинарника
```

Скрипт принимает `--img`, `--dump` и `--udev` (по умолчанию `dump.img`,
`dump.bin`, `udev.txt`); если файла нет, соответствующий раздел пропускается с
предупреждением. Тестовые образы собирает `scripts/make_sample_image.sh` (нужны
`dosfstools` и `mtools`): FAT32-образ через `mkfs.vfat` + `mcopy` и дамп с
маркером `MBOT` и флагом `JC` по смещению `0x200090` для `decode` и `split`.

CI (`.github/workflows/ci.yml`) прогоняет тесты на ubuntu, windows, macos с Python 3.10/3.11/3.12; отдельное интеграционное задание — с установленной `pytsk3` и `mkfs.vfat`. Перед коммитом полезно прогнать `make lint && make test`; локально те же проверки делает `.pre-commit-config.yaml`.

## Структура проекта

Полная карта каталогов, правила размещения файлов и таблица всех GitHub Actions
— в [docs/structure.md](docs/structure.md). Ниже — краткий обзор.

```text
mount-file/
├── main.py                     точка входа GUI/CLI + диагностика
├── mountfile.py                автономный инструмент монтирования (GUI + CLI)
├── mountfile_all.py            прогон всего функционала одной командой
├── mountfile_ai.py             отдельный MCP-сервер с агентной обвязкой
├── mountfile_fix.py            патчер/обслуживание дерева пакета
├── mountfile_dfir.py           standalone DFIR-утилиты (grep, timeline, timestomp, SHA-256)
├── mountfile_dfir_tools.py     расширенные standalone DFIR-инструменты
├── mountfile_grep.py           автономный grep по образу
├── mountfile_universal_rw.py   тонкие шимы (≈10 строк) на mountfile/rw/*
├── mountfile_rw_complete.py    └─ имена сохранены ради [project.scripts]
├── mountfile_rw_v2.py          └─ и внешних импортов
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
│   ├── appicon.py              иконка приложения (поиск в data/ и в _MEIPASS)
│   ├── qt_compat.py            слой совместимости Qt6/Qt5
│   ├── updater.py, update_dialog.py   проверка, установка обновлений и UI
│   ├── text_editor.py          правка текстовых файлов внутри образа
│   ├── dfir*.py                DFIR-подсистема (timeline, yara, slack, unallocated…)
│   │                           crypto.py, android/, macos.py, nand.py, registry.py
│   │                           — read-only анализаторы
│   ├── rw/                     универсальный RW-слой: universal.py, complete.py, v2.py
│   ├── turbo.py                вспомогательные утилиты (кэш, CDC, журнал)
│   ├── olma_watch.py           слежение за падениями
│   ├── ai/                     бэкенды AI-анализа (backend, agents, cli, copilot, mcp_server, rag)
│   ├── olma/                   самоанализ, автофикс, отчёты, публикация
│   ├── plugins/                plugin hooks
│   └── data/                   XOR-таблицы MST/MTK и иконки icon.png/.ico/.icns
├── scripts/                    сборка образов, GUI в Codespaces, штамп версии, ручной smoke
├── tests/                      автотесты (pytest)
├── tools/mfsync.py             синхронизация рабочей копии с GitHub
├── tools/tidy_repo.py          утилита уборки репозитория
├── examples/                   демо (asciinema .cast) и примеры плагинов
├── rules/                      YARA-правила (anti_forensics.yar, pii.yar)
├── archive/legacy/             исторические материалы (вне сборки, тестов и линта)
├── Makefile                    основные задачи разработки
├── requirements.txt            зависимости Windows 10/11, Linux, macOS
├── requirements-win7.txt       сохранённый legacy manifest (текущий код не поддерживает Win7)
├── requirements-docs.txt       зависимости сборки документации (mkdocs)
└── pyproject.toml              метаданные, ruff/mypy/pytest, semantic-release
```

## Ограничения

- **Запись зависит от файловой системы и системного backend.** FAT32 имеет
  встроенный writer; ext2/3/4 требует `debugfs` (на Windows доступен через WSL
  с `e2fsprogs`); NTFS — `ntfs-3g` на Linux или
  ImDisk и права администратора на Windows. На Windows ImDisk поддерживает
  NTFS, exFAT и FAT12/16 через системный драйвер. Остальные ФС доступны только
  для просмотра/извлечения, если их поддерживает установленная `libtsk`.
- **Без `pytsk3`** доступен только встроенный движок FAT32; чтение NTFS/EXT
  недоступно.
- RW-слой требует внешних утилит (`debugfs`, `ntfs-3g`), WSL/e2fsprogs для ext
  на Windows или ImDisk для NTFS/exFAT/FAT12/16.
  Если требуемый backend отсутствует, приложение явно отказывает в записи, а
  не сообщает об успехе после открытия тома только для чтения.
- Для неатомарных writer-ов `debugfs` и `ntfs-3g` перед каждой операцией записи
  создаётся временный снимок всего образа. Это повышает надёжность восстановления,
  но требует свободного места примерно под размер образа и может заметно увеличить
  время операции; снимок удаляется после успешного завершения. FAT32 использует
  внутренний откат метаданных. Исходная полная резервная копия остаётся отдельной.
- Данные OOB/ECC в сырых NAND-дампах не обрабатываются — нужен образ
  пользовательской области без этих данных.
- `mountfile/turbo.py` (`CachedFS`, CDC-подписи, delta-sync, sqlite-журнал)
  сейчас не подключён к интерфейсу: это библиотека для будущей интеграции.
- Приложение предоставляется «как есть». Всегда сохраняйте резервную копию
  образа, в который записываете.

## Участие в разработке

1. Форк → ветка `feat/my-feature`.
2. `pip install -e ".[dev]"` + `pre-commit install`.
3. Тесты в `tests/`, коммиты в формате [Conventional Commits](https://www.conventionalcommits.org/).
4. PR.

Подробнее — [CONTRIBUTING.md](CONTRIBUTING.md). Вопросы по уязвимостям — по
правилам [SECURITY.md](SECURITY.md), без публичных issue. Правила общения —
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) (Contributor Covenant v2.1).

## Лицензия

[![License: MIT](https://img.shields.io/github/license/Alexkkkkk/mount-file?label=license&color=yellow)](LICENSE)

Проект распространяется по лицензии **MIT** — полный текст лежит в файле
[`LICENSE`](LICENSE) в корне репозитория
(`SPDX-License-Identifier: MIT`). GitHub определяет её автоматически, поэтому
лицензия видна и в шапке репозитория, и в бейдже выше.

Что это значит на практике:

| Разрешено | Условия | Гарантии |
|---|---|---|
| использование, копирование, изменение, слияние, публикация, распространение, сублицензирование и продажа копий | сохранить уведомление об авторских правах и текст лицензии в копиях или существенных частях ПО | ПО поставляется «как есть», без явных и подразумеваемых гарантий; автор не отвечает за убытки |

```text
MIT License

Copyright (c) 2026 Alexkkkkk

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

[LICENSE](LICENSE) © 2026 Alexkkkkk.
