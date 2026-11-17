<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->

[![CI](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml)
[![Quality](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml)
[![Security](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml)
[![Docs](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml)
[![Docker](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml)
[![Latest release](https://img.shields.io/github/v/release/Alexkkkkk/mount-file?sort=semver&label=release)](https://github.com/Alexkkkkk/mount-file/releases/latest)
[![License: MIT](https://img.shields.io/github/license/Alexkkkkk/mount-file?label=license&color=yellow)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

# Mount File

**Mount File** — десктопный файловый менеджер и DFIR-инструмент для работы с
raw-образами дисков, дампами NAND/eMMC и анализа файловых систем. Программа
читает таблицы разделов, показывает и извлекает файлы, выполняет ограниченный
read-only анализ и умеет ограниченно писать — только для явно проверенных
профилей файловых систем.

Инструмент работает с файлами-образами: он **не** записывает данные обратно на
физические носители и не заявляет полного соответствия спецификациям файловых
систем или возможностям специализированных forensic-продуктов.

> **English:** Mount File is a cross-platform desktop application for browsing,
> extracting and analyzing raw disk images and NAND/eMMC dumps. Native readers
> and writers are deliberately bounded and fail closed when a profile is dirty,
> malformed, unsupported or ambiguous.

| | |
|---|---|
| Версия пакета | `1.96.0` — синхронизирована в `pyproject.toml` и `mountfile/__init__.py`; готовые сборки в [Releases](https://github.com/Alexkkkkk/mount-file/releases) |
| Документация | [alexkkkkk.github.io/mount-file](https://alexkkkkk.github.io/mount-file/) |
| Репозиторий | [github.com/Alexkkkkk/mount-file](https://github.com/Alexkkkkk/mount-file) |
| Лицензия | [MIT](LICENSE) |
| Python | 3.10, 3.11, 3.12 |
| Платформы | Windows 10/11, Linux, macOS |

## Содержание

- [Генерируемый статус проекта](#генерируемый-статус-проекта)
- [Что поддерживается сейчас](#что-поддерживается-сейчас)
- [Установка](#установка)
- [Графический интерфейс](#графический-интерфейс)
- [Командная строка](#командная-строка)
- [Переменные окружения](#переменные-окружения)
- [Отдельный помощник монтирования](#отдельный-помощник-монтирования)
- [Опциональные AI-, API- и layer-компоненты](#опциональные-ai-api--и-layer-компоненты)
- [Границы безопасности](#границы-безопасности)
- [Разработка](#разработка)
- [Структура репозитория](#структура-репозитория)
- [Участие и безопасность](#участие-и-безопасность)
- [Поддержка проекта](#поддержка-проекта)
- [Лицензия](#лицензия)
- [Заметки для мейнтейнера: MCP-серверы для Copilot](#заметки-для-мейнтейнера-mcp-серверы-для-copilot)

<!-- MOUNTFILE-AUTO-STATUS:START -->
## Generated project status

This block is generated from the canonical status data in this repository. Package version: `1.96.1`.

Validated automation: checker acceptance, recovery acceptance, benchmark artifact validation.

Canonical status data: `docs/generated/capability-matrix.json`, `checker-matrix.json`, `recovery-matrix.json` and `product-status.json`.

Capability profiles currently declared: capability matrix available.

This generated summary is informational: unsupported, dirty, ambiguous and recovery-required profiles remain fail-closed; benchmark and fixture evidence do not claim full filesystem parity or power-loss safety.
<!-- MOUNTFILE-AUTO-STATUS:END -->

## Что поддерживается сейчас

В проекте два взаимосвязанных слоя:

- Qt-приложение в `main.py`;
- CLI пакета — `python -m mountfile`, после установки также команда `mountfile`.

Нативный слой файловых систем поддерживает **проверенные ограниченные
профили**, а не любые раскладки, допустимые спецификацией:

| Семейство | Нативное чтение | Нативная запись | Область применения |
|---|---:|---:|---|
| FAT12/16/32 | Да | Ограниченно | Ограниченная геометрия и метаданные; для FAT12/16 — фиксированный корневой каталог |
| exFAT | Да | Ограниченно | Профиль с одной FAT, проверенные метаданные и состояние тома |
| ext2 | Да | Ограниченно | Метаданные из allow-list, ограниченная прямая/косвенная ёмкость |
| ext3/ext4 | Read-only | No | Продуктовая запись не поддерживается; bounded JBD2 replay экспериментальный, требует явного opt-in и не прошёл полный acceptance |
| NTFS | Read-only | No | Продуктовая запись не поддерживается; bounded replay включается явно, проверен на synthetic fixtures, parity с Windows/chkdsk отсутствует |

> Ячейки `Read-only` / `No` намеренно оставлены на английском: это машинно
> проверяемые токены гейта статуса (`tests/test_readme_status_gate.py`,
> `scripts/product_status_contract.py`). Смысл по-русски — «только чтение» и
> «запись не поддерживается».

Нативная запись в ext3/ext4 и NTFS не поддерживается, устойчивость к потере
питания не заявляется (канонические машинно-проверяемые формулировки:
*native ext3/ext4 and NTFS writes are unsupported*; *power-loss safety is
unsupported*). Ограниченная запись доступна только в перечисленных
профилях, и включать её нужно осознанно. Резервные копии, откаты и
бенчмарк-отчёты не означают ни устойчивости к сбоям питания, ни полного
соответствия спецификациям.

> **Предупреждение о guarded recovery:** `MOUNTFILE_ENABLE_NATIVE_WRITE=1` включает
> только проверочное восстановление и копирование для ограниченных нативных
> профилей. Он сам по себе **не** включает экспериментальные пути воспроизведения
> журнала ext4/JBD2 и NTFS `$LogFile` — для них нужен явный параметр писателя
> `recover="native"`, и они остаются вне заявленной поддержки продукта.
> Устойчивость к потере питания по-прежнему не поддерживается.

Версионируемая машиночитаемая матрица — источник истины по операциям, условиям
состояния и кодам отказа:

```bash
python -m mountfile capabilities
```

Выбранный образ можно проверить без изменения:

```bash
python -m mountfile status dump.img
python -m mountfile preflight dump.img
```

### Экспериментальные интеграции с журналами

В репозитории есть изолированное экспериментальное пространство
`mountfile.experimental`. Его опциональный адаптер `dfir_ntfs` умеет читать
записи из внешне установленного парсера, но не импортируется в продакшн-путь и
не имеет методов записи или воспроизведения. Внешний проект распространяется
под GPL-3.0 и поэтому не поставляется вместе с этим MIT-проектом. Отсутствующие
или несовместимые опциональные зависимости дают статус `unavailable` и никогда
не включают резервный писатель. Слой предназначен для поэтапной приёмки и не
входит в поддерживаемую матрицу возможностей.

Подробный статус и оставшаяся работа по приёмке —
[`docs/issue-182-status.md`](docs/issue-182-status.md) и
[issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).
Про управление самообучающимися слоями, обратную связь, границы приватности и
гейт починки кода OLMA — [`docs/self-learning-layers.md`](docs/self-learning-layers.md).

## Установка

### Из исходников

Нужен Python 3.10 или новее. Базовая установка (единственная зависимость —
`PyQt6`) даёт Qt-приложение и нативные читатели с ограничениями:

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install .
python main.py
```

Открыть образ сразу:

```bash
python main.py path/to/image.img
```

Установщик-помощник подготовит окружение Python и опциональные системные
компоненты:

```bash
python install_mountfile.py                # полная интерактивная установка
python install_mountfile.py --minimal      # минимальный нативный профиль
python install_mountfile.py --dry-run      # показать план, ничего не меняя
python install_mountfile.py --dev          # добавить инструменты разработчика
python install_mountfile.py --system-only  # только системные пакеты
python install_mountfile.py --no-sys       # пропустить установку системных пакетов
```

Дополнительные ключи: `--legacy-tsk`, `--skip-ext`, `--skip-imdisk`, `--upgrade`.
Установщик не ставит AI-модели, API-ключи и внешние forensic-базы. Полное
описание: [`docs/install.md`](docs/install.md).

### Опциональные наборы зависимостей

Наборы объявлены в `pyproject.toml`. Ставьте только нужное:

| Набор | Что добавляет |
|---|---|
| `.[legacy-tsk]` | `pytsk3` — явный устаревший совместимый fallback |
| `.[dfir]` | `python-dateutil`, `yara-python` |
| `.[api]` | FastAPI, uvicorn, httpx для REST API |
| `.[ai]` | MCP, pydantic, OpenAI-совместимые бэкенды, regex |
| `.[images]` | проверки изображений на Pillow |
| `.[rw]`, `.[rw-ext]`, `.[rw-ntfs]` | опциональные нативные RW-бэкенды |
| `.[fuzz]` | инструменты фаззинга для Linux |
| `.[dev]` | pytest, coverage, hypothesis, ruff, black, isort, pre-commit, PyInstaller |

```bash
python -m pip install '.[dfir]'
python -m pip install -r requirements-all.txt   # всё сразу, для контрибьюторов
```

`pytsk3` не является автоматическим fallback для распознанного, но
неподдерживаемого или «грязного» нативного профиля. Устаревшую совместимость
включают явно — когда это уместно для конкретного исследования.

### Готовые сборки

Артефакты для Windows, Linux и macOS публикуются в
[GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases) вместе с
SHA-256. Проверяйте контрольную сумму и работайте с копией образа-доказательства.

### Docker

Точка входа образа — `python -m mountfile`; команда по умолчанию — безопасная
проверка конфигурации.

```bash
docker build -t mountfile .
docker run --rm -v "$PWD/evidence:/evidence:ro" mountfile info /evidence/dump.img
docker compose run --rm mountfile info /evidence/dump.img
```

Держите примонтированные каталоги с доказательствами в режиме read-only. Для
команды записи работайте с копией и осознанно переводите в `read_only: false`
только этот монтированный каталог.

## Графический интерфейс

```bash
python main.py                 # запустить десктопное приложение
python main.py dump.img        # сразу открыть образ
python main.py --version       # вывести версию
python main.py --diag          # диагностика локального окружения
python main.py --check-updates # одна проверка обновлений на GitHub Releases и выход
```

В GUI есть двухпанельный файловый менеджер, выбор раздела и тома, просмотр
каталогов, извлечение файлов, HEX-редактор, сводка по образу, autopick для
неоднозначных дампов NAND/eMMC, опциональный AI-анализ и ограниченные операции
записи там, где это разрешает выбранный профиль.

Запись изменяет файл образа. Перед **Import**, **New folder**, **Rename**,
**Delete** или сохранением из HEX:

1. сделайте независимую копию образа;
2. проверьте выбранный том и смещение;
3. убедитесь, что места хватает для настроенной резервной копии;
4. подтвердите, что операция входит в активный нативный профиль.

Перед первым изменением за сессию GUI создаёт резервную копию. Это страховка,
а не журнал файловой системы и не гарантия сохранности при сбое питания.

Горячие клавиши: `Tab` — переключение панелей, `Space` — выделить,
`F5`/`F6` — копировать/переместить, `F7` — создать каталог, `F8` — удалить,
`Ctrl+O` — открыть образ, `Ctrl+R` — обновить, `Ctrl+Shift+X` — командный центр.
Полное руководство: [`docs/gui.md`](docs/gui.md).

## Командная строка

CLI пакета не требует Qt:

```bash
python -m mountfile --help
python -m mountfile volumes dump.img
python -m mountfile info dump.img
python -m mountfile capabilities
python -m mountfile status dump.img
python -m mountfile ls dump.img /
python -m mountfile stat dump.img /etc/hosts
python -m mountfile get dump.img /photos -o ./evidence/photos
```

После `pip install .` доступны команды `mountfile` (тот же CLI), а также
`mountfile-ai`, `mountfile-rw` и `mountfile-rw-v2`. Вариант
`python main.py <команда> ...` — поддерживаемый мост к тому же CLI.

### Общие параметры выбора цели

Любая команда, работающая с образом (`ls`, `stat`, `read-range`, `get`, `put`,
`write-range`, `append`, `truncate`, `mkdir`, `mv`, `ln`, `rm`, `preflight`,
`status`, `volumes`, `info`, `autopick`), принимает:

| Параметр | Смысл |
|---|---|
| `-V`, `--volume N` | номер тома из вывода `volumes` (нумерация с 1) |
| `-O`, `--offset 0x400000` | сырое байтовое смещение файловой системы |
| `--scan-limit N` | сколько байт сканировать в поисках сигнатур ФС |
| `--auto` | автоматический выбор рабочего тома |
| `-u`, `--udev FILE` | файл раскладки разделов с авторитетными экстентами для `--auto` |
| `--backup` | скопировать образ в `<image>.bak` перед записью |
| `--allow-libguestfs` | явно включить опциональный писатель libguestfs |

### Команды чтения и работы с доказательствами

| Команда | Назначение | Меняет исходный образ |
|---|---|---:|
| `volumes` | список найденных разделов и томов | Нет |
| `info` | сводка по образу и томам | Нет |
| `capabilities` | матрица нативных профилей с версией | Нет |
| `status` | выбранный бэкенд, профиль и типизированный статус отказа | Нет |
| `preflight` | проверка профиля и состояния в режиме read-only с отчётом об отказах | Нет |
| `ls`, `stat` | список или метаданные пути внутри ФС | Нет |
| `read-range` | чтение ограниченного диапазона байт из обычного файла (не более 16 MiB) | Нет |
| `get` | извлечь файл или каталог | Нет |
| `autopick` | автоматически выбрать проверенный том | Нет |
| `hash` | read-only SHA-256 идентичность | Нет |
| `evidence-manifest` | отдельный JSON-манифест для одного файла-доказательства | Нет |
| `grep`, `search` | поиск по сырым байтам образа (`grep`) или по файлам ФС (`search`) | Нет |
| `timeline`, `timestomp` | просмотр или поиск аномалий в переданном CSV-таймлайне | Нет |
| `timeline-export` | приведение отметок времени из CSV к UTC | Только вывод |
| `timeline-build`, `timeline-query` | построение и запросы к таймлайну с учётом ФС (SQLite/CSV) | Только вывод |
| `yara`, `yara-scan` | проверка разрешённых целей правилами YARA | Нет |
| `unallocated-scan` | сканирование явно указанных нераспределённых диапазонов | Нет |
| `slack`, `slack-extract` | просмотр или извлечение явно определённого file slack | Только вывод |
| `live-diff` | сравнение образов с отчётом в Markdown | Только вывод |

DFIR-командам, работающим через ФС, могут понадобиться `pytsk3`/`libtsk`,
командам YARA — `yara-python` (набор `.[dfir]`). Они не заменяют проверенную
карту размещения, карвинг и систему цепочки сохранности доказательств.

Правила в `rules/`: `anti_forensics.yar`, `pii.yar`.

### Команды ограниченной записи

Команды записи требуют `--yes` и отказываются работать с неподдерживаемым
профилем вместо тихого отката на другой бэкенд:

```bash
python -m mountfile put dump.img ./hello.txt --dest /docs --backup --yes
python -m mountfile write-range dump.img /config/settings.ini --input ./new.ini --file-offset 0 --backup --yes
python -m mountfile append dump.img /log/app.log --input ./tail.log --backup --yes
python -m mountfile truncate dump.img /log/app.log --size 4096 --backup --yes
python -m mountfile mkdir dump.img /docs/new --backup --yes
python -m mountfile mv dump.img /hello.txt renamed.txt --backup --yes
python -m mountfile ln dump.img /hello.txt /docs/hello-link.txt --yes
python -m mountfile rm dump.img /docs/new --recursive --backup --yes
```

Жёсткая ссылка `ln` доступна только в проверенном нативном профиле записи ext2;
для остальных файловых систем операция отклоняется гейтом возможностей.

`--backup` создаёт соседний файл `<image>.bak`; это не транзакция.

Коды возврата: `0` — успех, `1` — ошибка, `2` — нет подтверждения,
`130` — прерывание.

### Помощники для RT809H и NAND/eMMC

```bash
python -m mountfile decode dump.bin
python -m mountfile split dump.bin udev.txt -o ./parts
python -m mountfile autopick dump.bin --json > candidates.json
python -m mountfile autopick dump.bin --hint system --extract ./rootfs
```

`decode` работает с зашифрованными RT809H дампами eMMC MST/MTK (`--table-dir`
подменяет встроенные XOR-таблицы). `autopick` объединяет MBR/GPT, метаданные
`udev` и сигнатуры файловых систем, а затем проверяет кандидатов открытием;
`--no-verify` быстрее, но менее безопасно. Чередование OOB/ECC для raw NAND —
вне области задач этого инструмента.

Полный справочник параметров: [`docs/cli.md`](docs/cli.md).

### Консультативные самообучающиеся слои

```bash
python -m mountfile layers status --json
# слои: volume, preflight, cache, advisor, code_repair, operations
python -m mountfile layers disable volume
python -m mountfile layers decide ./dump.img --operation inspect --fs fat32 --json
python -m mountfile layers feedback <decision-id> --success --latency-ms 12
```

Вывод слоёв носит рекомендательный характер и никогда не отменяет отказ,
выданный по матрице возможностей. `MOUNTFILE_DISABLE_LAYERS=1` полностью
отключает слои.

## Переменные окружения

Переключатели, которые действительно читает код. Незаданная переменная
оставляет безопасное значение по умолчанию.

| Переменная | Действие |
|---|---|
| `MOUNTFILE_ENABLE_NATIVE_WRITE=1` | включить проверочную нативную запись и копирование — только для ограниченных профилей |
| `MOUNTFILE_ALLOW_LEGACY_TSK=1` | явно разрешить совместимость через `pytsk3` |
| `MOUNTFILE_NO_UPDATE=1` | отключить проверку обновлений при старте |
| `MOUNTFILE_ENABLE_UPDATE_CHECK=1` | разрешить сетевую проверку обновлений при старте |
| `MOUNTFILE_AUTO_UPDATE=1` | устанавливать обновление без вопроса и перезапускаться |
| `MOUNTFILE_UPDATE_REPO=owner/repo` | другой репозиторий как источник релизов |
| `MOUNTFILE_UPDATE_URL=…` | прямой URL на JSON последнего релиза (внутренний сервер) |
| `MOUNTFILE_GIT_PULL=1` | в git-чекауте разрешить `git pull` при обновлении |
| `MOUNTFILE_LAYERS_DIR=…` | корневой каталог хранилища обучающихся слоёв |
| `MOUNTFILE_DISABLE_LAYERS=1` | отключить консультативные слои |
| `MOUNTFILE_AI_BACKEND`, `MOUNTFILE_AI_MODEL` | выбор опционального AI-бэкенда и модели |
| `MOUNTFILE_AI_URL`, `MOUNTFILE_AI_URL_OPENAI`, `MOUNTFILE_AI_KEY` | эндпоинт и ключ AI (`.[ai]`) |
| `MOUNTFILE_AI_MAX_BYTES=N` | ограничение размера отправляемых в AI данных |
| `MOUNTFILE_API_TOKEN=…` | bearer-токен для опционального REST API (`.[api]`) |
| `MOUNTFILE_EDITOR=…` | внешний текстовый редактор для GUI (также учитываются `$VISUAL`/`$EDITOR`) |
| `MOUNTFILE_OLMA_TOKEN`, `MOUNTFILE_OLMA_REPO`, `MOUNTFILE_OLMA_WATCH` | настройки самоанализа OLMA |

Сеть используется только по явному включению: проверка обновлений, AI-бэкенды
и API-интеграции. Нативные читатели и ограниченные писатели работают локально с
выбранным образом.

## Отдельный помощник монтирования

`mountfile.py` — отдельная утилита в один файл, которая монтирует контейнеры
образов средствами ОС. Это не тот же путь, что нативные читатели и писатели
выше:

```bash
python mountfile.py                  # GUI
python mountfile.py dump.img --to /mnt/x
python mountfile.py dump.img --open
python mountfile.py --list
python mountfile.py --unmount /mnt/x
python mountfile.py --version
```

Прочие ключи: `--read-only`, `--verbose`, `--help`. Утилита опирается на
системные инструменты: `udisksctl`/`mount` в Linux, `hdiutil` в macOS,
команды PowerShell для дисковых образов в Windows. Доступность и права зависят
от хоста.

## Опциональные AI-, API- и layer-компоненты

Всё перечисленное включается по желанию и не требуется для офлайн-просмотра
образов:

- `.[api]` — REST API с аутентификацией; эндпоинт возможностей описан в [`docs/api.md`](docs/api.md).
- `.[ai]` — MCP- и AI-помощники, локальные и OpenAI-совместимые бэкенды, плюс команды `ai-guard`, `ai-schemas`, `ai-analyze`. См. [`docs/ai.md`](docs/ai.md).
- OLMA — самоанализ проекта и отчёты; это инженерный инструмент, а не гарантия восстановления данных.
- `mountfile_all.py` — широкий локальный smoke-прогон; полезен для разработки и CI, но не заменяет независимые проверки файловых систем.

## Границы безопасности

Mount File намеренно отказывается работать (fail closed) в случаях, когда:

- выбранные экстенты или метаданные выходят за границы образа;
- распознанная файловая система «грязная», повреждена, неоднозначна или выходит за allow-list возможностей;
- запрошенной операции нет в матрице возможностей выбранного профиля;
- устаревший fallback не был включён явно;
- резервную копию или промежуточное изменение не удаётся выполнить безопасно.

Проект **не** заявляет:

- полного соответствия спецификациям файловых систем;
- нативной записи в ext3/ext4 и NTFS;
- полного восстановления журналов JBD2 и NTFS;
- универсальной атомарности при сбоях и потере питания;
- полной кросс-платформенной матрицы внешних чекеров;
- выполнения форензик-изъятия, цепочки сохранности или юридической допустимости.

Статус приёмки и план:
[`docs/issue-182-status.md`](docs/issue-182-status.md),
[`docs/issue-182-full-filesystem-parity-task.md`](docs/issue-182-full-filesystem-parity-task.md),
[`docs/issue-510-acceptance-status.md`](docs/issue-510-acceptance-status.md),
[issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).

## Разработка

```bash
python -m pip install -e '.[dev]'
pre-commit install
make test
make lint
make lint-correctness
make fmt
```

Полезные цели `make`:

| Команда | Назначение |
|---|---|
| `make help` | список доступных целей |
| `make install` | установить пакет в активное окружение |
| `make test` | запустить набор pytest |
| `make test-fast` | быстрый поднабор тестов |
| `make test-ai` | тесты AI-слоя |
| `make test-standalone` | тесты отдельного помощника монтирования |
| `make lint` | синтаксический/ошибочный lint-гейт CI |
| `make lint-correctness` | правила корректности по всему репозиторию |
| `make fmt` | применить Ruff, isort и Black |
| `make sample` | собрать тестовые образы, если есть `dosfstools`/`mtools` |
| `make smoke-images` | сквозные проверки CLI и GUI на свежих образах, верифицированные `mtools`, `fsck.vfat`, `fsck.exfat`, `e2fsck` |
| `make all-in-one` | широкий smoke-прогон без GUI |
| `make exe` | собрать основной бинарник PyInstaller |
| `make mountfile-exe` | собрать бинарник отдельного помощника |
| `make tools`, `make tools-check` | показать и проверить опциональные системные инструменты |
| `make sync*` | помощники синхронизации репозитория — см. [`docs/sync.md`](docs/sync.md) |
| `make clean` | удалить артефакты сборки |

CI покрывает Linux, Windows и macOS на Python 3.10–3.12, плюс отдельные
проверки безопасности, зависимостей, документации и сборки. Опциональные тесты
на реальных образах могут пропускаться локально, если нет `pytsk3`,
`dosfstools`, `exfatprogs`, `ntfs-3g`, YARA или PowerShell.

Перед пул-реквестом прочитайте [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`docs/structure.md`](docs/structure.md), [`docs/quality.md`](docs/quality.md) и
[`docs/sync.md`](docs/sync.md).

Проверка исправленных пользовательских сценариев, команды повторного
тестирования и оставшиеся ограничения описаны в
[`docs/functional-validation.md`](docs/functional-validation.md).

## Структура репозитория

```text
main.py                  Qt-приложение и мост к CLI
mountfile.py             отдельный помощник монтирования средствами ОС
auto_mountfile.py        помощник autopick
mountfile/cli.py         CLI пакета: чтение, ограниченная запись, DFIR, слои
mountfile/capabilities.py версионируемая матрица возможностей и отказов
mountfile/vfs.py         маршрутизация нативного/совместимого VFS
mountfile/*_reader.py    читатели ФС с ограничениями (FAT, exFAT, ext2/3/4, NTFS)
mountfile/*_writer.py    реализации нативных писателей с ограничениями
mountfile/extract.py     безопасное извлечение с контролем путей
mountfile/dfir*.py       DFIR-команды (таймлайн, slack, yara, unallocated)
mountfile/api.py         опциональный REST API
mountfile/ai/            опциональный слой AI/MCP
mountfile/layers/        консультативные самообучающиеся слои
mountfile/olma/          опциональный инженерный самоанализ
mountfile/android/       помощники по Android
mountfile/experimental/  изолированные неподдерживаемые эксперименты
mountfile/data/          встроенные данные и таблицы
scripts/                 сборка, генерация образов, релизные помощники
tools/                   утилиты обслуживания
benchmarks/              стенд и артефакты бенчмарков
examples/                примеры входных данных
rules/                   правила YARA (anti_forensics, pii)
archive/legacy/          исторический код, сохранённый для справки
tests/                   набор pytest (180 модулей тестов)
docs/                    пользовательская документация, API, качество, роадмап
```

## Участие и безопасность

Форкните репозиторий, создайте локальную ветку под конкретную задачу, добавьте
тесты на изменение поведения и откройте пул-реквест в стиле
[Conventional Commits](https://www.conventionalcommits.org/). Не публикуйте в
issue и пул-реквестах реальные образы-доказательства, учётные данные, токены и
приватные дампы.

Об уязвимостях сообщайте приватно по [SECURITY.md](SECURITY.md), не через
публичный issue. Правила сообщества — [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
История изменений — [CHANGELOG.md](CHANGELOG.md).

## Поддержка проекта

Если Mount File полезен, приветствуются звезда на GitHub, воспроизводимый
баг-репорт, правка документации или код. Информация о добровольных
пожертвованиях — в [`docs/donations.md`](docs/donations.md).

Кошелёк проекта в сети **TON**: `UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv`.
Проверьте тот же адрес в [TON Viewer](https://tonviewer.com/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv)
или используйте [ссылку на перевод](ton://transfer/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv?text=MountFile%20donation).
Никогда не передавайте seed-фразу или приватный ключ; полное предупреждение — в
[`docs/donations.md`](docs/donations.md).

## Лицензия

Mount File распространяется по [лицензии MIT](LICENSE).
Copyright (c) 2026 Alexkkkkk.

## Заметки для мейнтейнера: MCP-серверы для Copilot

Бесплатные MCP-серверы без API-ключей для
[GitHub Copilot coding agent](https://docs.github.com/en/enterprise-cloud@latest/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers)
преднастроены в [`.github/copilot-mcp-config.json`](.github/copilot-mcp-config.json)
(серверы GitHub и Playwright встроены и не дублируются).

Настройка: **Settings → Copilot → MCP servers** в этом репозитории, вставьте
JSON из файла в поле «MCP configuration» и нажмите **Save MCP configuration**.
Секреты не нужны — все серверы в файле работают без API-ключа.

Файл добавляет вспомогательные серверы рассуждений без ключей
(`sequential-thinking`, `memory`), сервер документации Context7 и использует
актуальный Streamable HTTP эндпоинт для `cloudflare_docs`
(`https://docs.mcp.cloudflare.com/mcp`; устаревший `/sse` больше не работает).
