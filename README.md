<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
[![CI](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml)
[![Quality](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml)
[![Docs](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml)
[![CodeQL](https://github.com/Alexkkkkk/mount-file/actions/workflows/codeql.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/codeql.yml)
[![Security](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml)
[![Docker](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml)
[![Nightly](https://github.com/Alexkkkkk/mount-file/actions/workflows/nightly.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/nightly.yml)
[![Latest release](https://img.shields.io/github/v/release/Alexkkkkk/mount-file?sort=semver&label=release)](https://github.com/Alexkkkkk/mount-file/releases/latest)
[![License: MIT](https://img.shields.io/github/license/Alexkkkkk/mount-file?label=license&color=yellow)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

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
| **Актуальный релиз** | [v1.48.1](https://github.com/Alexkkkkk/mount-file/releases/latest) (к каждому `.exe` приложен `.sha256`) |
| **Документация** | [alexkkkkk.github.io/mount-file](https://alexkkkkk.github.io/mount-file/) |
| **Версия в файлах репозитория** | `1.26.0` (`mountfile/__init__.py`, `pyproject.toml`) — не совпадает с последним тегом |
| **Лицензия** | [MIT](LICENSE) |
| **Платформы** | Windows 10/11, Windows 7 (Python 3.8 + PyQt5), Linux, macOS |

> **Про версию.** В файлах репозитория указано `1.26.0` (`mountfile/__init__.py`,
> `pyproject.toml`), а последний релизный тег — `v1.48.1`; workflow
> `version-bump.yml` при этом завершается успешно. Расхождение не скрывается:
> ориентируйтесь на тег релиза, а не на строку версии в файле.

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
- **Windows 7** через Python 3.8.10 + PyQt5 (`mountfile/qt_compat.py`);
  Windows 10/11, Linux и macOS — через PyQt6.

## Требования

| Компонент | Версия |
|---|---|
| Python | 3.10–3.12 для основной установки (`requires-python = ">=3.10"`); 3.8.10 — только для Windows 7 |
| Qt | PyQt6 ≥ 6.11 (`requirements.txt`) или PyQt5 5.15 (Windows 7) |
| `pytsk3` | необязательно; нужен для чтения NTFS/EXT и прочих ФС |
| REST API (необязательно) | `fastapi`, `uvicorn`, `httpx` |
| AI-слой (необязательно) | `mcp[cli]`, `pydantic`, `openai` |
| Системные пакеты (Linux) | `libgl1`, `libegl1`, `libxkbcommon-x11-0`, `libdbus-1-3`, `libxcb-cursor0` |
| Системные инструменты (по желанию) | `dosfstools`, `mtools` — только для сборки тестовых образов; `debugfs` (e2fsprogs) и `ntfs-3g` — для записи в ext/NTFS |

Основные зависимости (`requirements.txt`) рассчитаны на Python 3.10–3.12. Для
Windows 7 используйте отдельный `requirements-win7.txt` с Python 3.8.10 и PyQt5.
На Python 3.8 `pytsk3` обычно недоступен как готовое колесо, поэтому зависимость
там пропускается; приложение продолжает работать со встроенным движком FAT32.

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

### Необязательные extras

| Extra | Что добавляет |
|---|---|
| `.[api]` | authenticated REST API и его тестовый клиент (`fastapi`, `uvicorn`, `httpx`) |
| `.[ai]` | MCP/AI helpers (`mcp[cli]`, `pydantic`, `openai`) |
| `.[dev]` | `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `black`, `isort`, `pyinstaller` |
| `.[win7]` | PyQt5 5.15 для Windows 7 |
| `.[rw]`, `.[rw-ext]`, `.[rw-ntfs]` | универсальный RW-слой: чтение через `MountFileBridge`, запись через `debugfs` / `ntfs-3g` |
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

### Windows 7

```bat
py -3.8 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements-win7.txt
python main.py path\to\image.img
```

Сборка автономного `dist\MountFile.exe` при активном окружении Win7:

```bat
build_exe.bat
py -3.8 build_exe.bat        REM принудительно Python 3.8
```

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
`python -m mountfile`; Qt для него не требуется. Для сборки Windows 7
используйте `python main.py`.

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
FAT32 встроен, ext2/3/4 поддерживается только при наличии `debugfs` в `PATH`,
NTFS — при наличии `ntfs-3g`. Перед изменением образа создаётся отдельная
резервная копия; если её нельзя создать, запись отменяется. Файловые системы
без writer остаются доступны только для чтения.

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
py -3.8 build_exe.bat          REM Windows 7: принудительно Python 3.8
```

Если `pytsk3` не установлена, сборка не падает: бинарник собирается с встроенным
движком FAT32. Релизные сборки собирает workflow `release.yml` (Windows x64,
Windows 7, Linux x86-64, macOS arm64), а версию в `mountfile/__init__.py`
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
[Sync Guard](.github/workflows/sync-guard.yml).

```bash
make sync          # pull + commit + push
make sync-status   # статус
make sync-check    # проверка версий для CI
```

## Тесты и разработка

```bash
python -m pip install -e ".[dev]"     # pytest, ruff, black, isort, pyinstaller
pytest tests/ -v
```

| Действие | Команда |
|---|---|
| все тесты (54 файла в `tests/`) | `make test` — `pytest tests/ -v` |
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
├── requirements-win7.txt       зависимости Windows 7 (Python 3.8 + PyQt5)
├── requirements-docs.txt       зависимости сборки документации (mkdocs)
└── pyproject.toml              метаданные, ruff/mypy/pytest, semantic-release
```

## Ограничения

- **Запись поддерживается только для FAT32.** Переименование и удаление также
  работают только на FAT32. Остальные файловые системы доступны для просмотра и
  извлечения, если их поддерживает установленная сборка `libtsk`.
- **Без `pytsk3`** (в том числе на Windows 7 с Python 3.8) доступен только
  встроенный движок FAT32; чтение NTFS/EXT недоступно.
- Универсальный RW-слой (`mountfile/rw/`) требует внешних утилит (`debugfs` для
  ext2/3/4, `ntfs-3g` для NTFS) и остаётся экспериментальным: отсутствие
  утилиты — это явный отказ записи, а не «тихая» деградация.
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
