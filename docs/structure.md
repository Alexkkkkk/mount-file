# Структура репозитория

Карта каталогов, правила размещения файлов и полный список GitHub Actions.
Цель документа — чтобы любой файл имел ровно одно очевидное место.

## Верхний уровень

| Путь | Назначение | Правило |
| --- | --- | --- |
| `main.py` | точка входа GUI-приложения (Total Commander стиль) | единственный GUI-энтрипойнт |
| `mountfile/` | основной пакет: GUI, CLI, FS-слой, DFIR, AI, OLMA | любая новая логика живёт **здесь**, а не в корне |
| `mountfile.py` | автономный одностраничный инструмент (GUI+CLI, без `pytsk3`) | поставляется как отдельный бинарник, зависимостей из `mountfile/` не тянет |
| `mountfile_all.py` | лаунчер «всё сразу»: CLI + API + тесты | используется `make all-in-one` |
| `mountfile/rw/` | универсальный RW-слой: `universal.py`, `complete.py`, `v2.py` | вся логика записи живёт **здесь** |
| `mountfile_universal_rw.py`, `mountfile_rw_complete.py`, `mountfile_rw_v2.py` | тонкие шимы (≈10 строк) на `mountfile/rw/*` | имена сохранены ради `[project.scripts]` и внешних импортов; правится реализация в пакете, не шим |
| `mountfile_ai.py` | AI-слой верхнего уровня | legacy-энтрипойнт, новый код — в `mountfile/ai/` |
| `mountfile_dfir.py`, `mountfile_dfir_tools.py` | DFIR-энтрипойнты | legacy, новый код — в `mountfile/dfir*.py` |
| `mountfile_grep.py` | автономный grep по образу | standalone-утилита |
| `mountfile_fix.py` | патч-скрипт внедрения OLMA | одноразовый инструмент, не часть рантайма |
| `tests/` | pytest-набор (`testpaths` в `pyproject.toml`) | один файл = одна подсистема |
| `scripts/` | вспомогательные скрипты: сборка сэмплов, запуск GUI, штамп версии, ручной smoke | всё, что запускается руками, а не импортируется |
| `tools/` | инструменты разработчика: `mfsync.py`, `tidy_repo.py` | CLI-утилиты репозитория |
| `docs/` | mkdocs-источник (`mkdocs.yml` → `nav`) | каждая страница обязана быть в `nav` |
| `examples/` | демо и плагины | не импортируется из `mountfile/` |
| `rules/` | YARA-правила для `dfir_yara` | данные, не код |
| `archive/` | исторические материалы | вне сборки, тестов и линта |
| `.github/` | workflows, шаблоны issue/PR, dependabot | см. таблицу ниже |

## Куда что класть

- новый модуль приложения → `mountfile/<тема>.py`, тест → `tests/test_<тема>.py`;
- новая GUI-панель → `mountfile/tc_*.py` или `mountfile/*_dialog.py`;
- новый энтрипойнт CLI → `mountfile/cli.py` (не новый файл в корне);
- скрипт «сделать разово» → `scripts/`, инструмент «пользоваться постоянно» → `tools/`;
- страница документации → `docs/` **и** строка в `nav` файла `mkdocs.yml`;
- выведенный из эксплуатации, но ценный файл → `archive/legacy/` через `git mv`.

## GitHub Actions

16 workflow — у каждого своя зона ответственности. Проверки паритета версий,
секретов и automation-контрактов выполняет только `mfsync.yml`; отдельный
`sync-guard.yml` удалён как полный дубль.

| Workflow | Триггер | Роль |
| --- | --- | --- |
| `ci.yml` | push/PR в `main`, nightly cron | тесты: 3 ОС × Python 3.10/3.11/3.12, Linux integration, сборка/GUI-import; по расписанию — Windows nightly exe |
| `quality.yml` | push/PR | единственный обязательный lint/actionlint gate |
| `security.yml` | push/PR + cron | dependency review, pip-audit и CodeQL в одном workflow |
| `docs.yml` | push/PR + ручной | сборка mkdocs |
| `docker.yml` | push/PR + ручной | сборка образа |
| `deploy.yml` | push в `main`, явный dispatch из release | staging и production hooks без управления Release assets |
| `release.yml` | `release: published` | сборка и загрузка артефактов |
| `create-release.yml` | теги `v*` | создание релиза из CHANGELOG |
| `version-bump.yml` | push в `main` | semantic-release |
| `mfsync.yml` | push/PR в `main`, cron, ручной | синхронизация, version parity, secret-scan и automation tests |
| `autofix.yml` | push в `main`, `workflow_call` | ruff/black/isort автофиксы |
| `self-heal.yml` | `workflow_run: CI` | перезапуск/починка после падения CI |
| `auto-merge.yml` | `pull_request` | авто-мерж размеченных PR |
| `labeler.yml` | `pull_request_target` | лейблы по путям |
| `chatops.yml` | `issue_comment` | команды из комментариев |
| `stale.yml` | cron | закрытие залежавшихся issue |

Правило: новый workflow добавляется **только** если его зону нельзя расширить
существующим; после добавления строка появляется в этой таблице.

## Гигиена репозитория

- `.gitattributes` — `eol=lf` для текста, `binary` для `*.bin/*.ico/*.icns/*.png/*.img`,
  `linguist-*` для `docs/`, `CHANGELOG.md`, `archive/`;
- `.editorconfig` — 4 пробела (Python/Makefile-исключение), 2 для yml/json/toml,
  CRLF только для `.bat`/`.ps1`;
- `.gitignore` — образы и дампы (`*.img`, `*.bin`, `*.raw`, `*.dump`) игнорируются,
  **кроме** отгружаемых ресурсов `mountfile/data/`;
- секреты: `tools/mfsync.py` и workflow `mfsync.yml` сканируют дерево на
  `github_pat_*` / `ghp_*` / `AKIA*` / `xox*` / `sk-*`; токен читается из
  переменной окружения и никогда не попадает в `.git/config`.

## Проверка перед PR

```bash
make lint                       # ruff: E9,F63,F7,F82
python -m pytest tests/ -q      # полный набор
python -m compileall -q mountfile main.py mountfile.py
```

## Ruleset ветки `main`

Активный ruleset требует только реально существующие статусы `quality-gate` и
`dependency-review`. Имена не меняются без одновременного изменения ruleset:
иначе GitHub начнёт ждать отсутствующий status check.
