# Ускорение цикла разработки MountFile

Документ описывает, что уже есть в репозитории для быстрого цикла «правка → зелёный
гейт», что добавлено в этом изменении и что сознательно **не** сделано (с причиной).
Ни один пункт не меняет продуктовый статус: ext3/ext4 и NTFS остаются read-only,
acceptance-пункты трекера #510 закрываются своим порядком.

## 0. Что уже было

| Что | Где |
|---|---|
| Параллельный быстрый прогон (`-n auto --dist=worksteal`, без gui/e2e) | `make test-fast` |
| Локальное окружение из lockfile без резолвера | `.github/actions/python-test-env` (`uv sync --frozen --extra ci`) |
| uv-кэш в CI | `astral-sh/setup-uv` с `cache-dependency-glob: uv.lock, pyproject.toml` |
| Fast tier на PR, полная матрица на `main` | `ci.yml`, job `setup-matrix` |
| Отмена устаревших PR-прогонов | `concurrency.cancel-in-progress` |
| Кэш системных пакетов Linux | `./.github/actions/setup-system-deps` |
| Манифест трекера и его проверка | `docs/replay-acceptance.json`, `scripts/replay_acceptance_status.py` |
| Инструкции для агентов по безопасности | `.github/copilot-instructions.md` |

Узкое место было не в отсутствии инструментов, а в трёх параллельных способах делать
одно и то же (ruff/black/isort, pip/uv, быстрый/полный прогон) и в ручной бухгалтерии
вокруг трекера.

## 1. Что добавлено

### Makefile — одна команда на каждый шаг

| Цель | Что делает |
|---|---|
| `make test-fast` | параллельный прогон + `--durations=25`, чтобы медленные тесты были видны сразу |
| `make test-changed` | только тестовые файлы, изменённые относительно `origin/main` (+ их зависимости по имени модуля) |
| `make lint-fast` | синтаксис, неопределённые имена и правила корректности (#182) — то, что реально валит CI |
| `make fmt-check` | `black --check` + `isort --check-only` без изменения файлов — паритет с `quality.yml` |
| `make tracker-status` | готовый Markdown-блок для #510 из манифеста |
| `make dev-sync` | `uv sync --frozen --extra ci` локально, без резолвера |

### `scripts/tracker_status.py` — сводка трекера генерируется

Комментарий в #510 больше не набирается руками:

```bash
make tracker-status                                   # блок для issue
python scripts/tracker_status.py --check              # exit 1 при расхождении с деревом
python scripts/tracker_status.py --json               # машинный вывод
python scripts/tracker_status.py --markdown \
    --similarity last-comment.md                      # exit 3, если это повтор (>90%)
```

`--check` проверяет то, что может проверить дерево: статус существует, `verified`/
`partial`/`blocked` ссылаются на существующие файлы, `not_started` не ссылается ни на
что, ни один id не объявлен дважды. `--similarity` — это ровно та операция, которую
в треде 2026-11-18 делали вручную (удаление зеркальных комментариев со сходством
0.999 и 0.900).

`--check` дополнительно сравнивает `main` с `origin/main` и предупреждает, что срез
манифеста отстаёт, если ветка ушла вперёд.

### `scripts/new_acceptance_item.py` — генератор acceptance-пункта

32 пункта трекера — это 32 почти одинаковых «файл теста + запись в манифесте».
Вместо копирования предыдущего:

```bash
python scripts/new_acceptance_item.py --id C6 --block C \
    --title "NTFS: diff classifier на синтетическом \$LogFile" \
    --kind integration --test-tests tests/test_ntfs_diff_classifier.py --dry-run

# затем без --dry-run: создаёт tests/test_<slug>.py (skipped-заготовка)
# и вставляет запись в docs/replay-acceptance.json
```

Генератор отказывается работать, если id уже объявлен, если файл доказательства
отсутствует, если блок неизвестен, и если сам манифест не проходит проверку после
вставки.

### `AGENTS.md` — правила для AI-агентов и ревьюеров

Один файл, который читают Copilot, Claude Code, Codex и Cursor: как проверять правку,
какие гейты нельзя ослаблять, какие имена проверок защищены branch protection, что
`uv.lock` нельзя править руками, что синтетический NTFS-фикстур и `ntfsfix -n` —
не оракул, и что комментарий в #510 генерируется, а не пишется.

### CI: покрытие только на одной ноге

`pytest-cov` замедляет прогон на 20-30 %, а `coverage.xml` нужен один. Матрица теперь
несёт флаг `coverage`: `ubuntu-latest`/3.12 собирает покрытие и держит
`--cov-fail-under=20`, остальные ноги гоняют те же тесты без инструментирования.
`--junitxml` остаётся на всех ногах — на нём построены `failure_matrix_artifact.py`,
`platform_benchmark_provenance.py` и `native_read_evidence.py`. Имена проверок не
менялись.

## 2. Что сознательно не сделано

| Пункт | Почему нет | Что вместо |
|---|---|---|
| Замена `black`+`isort` на `ruff format` | `pyproject.toml` содержит `[tool.ruff.lint] select = E,F,W,I,UP,B,SIM`; на этом наборе в дереве **2017** находок, `ruff format` даст массовый дифф по 5.3 МБ кода. Такое изменение нельзя прятать в PR про ускорение | `make fmt-check` даёт паритет с CI без риска; миграция — отдельный PR |
| Расширение `--select` в pre-commit | То же: pre-commit начнёт блокировать коммиты на существующем коде | `make lint-fast` локально повторяет ровно те правила, что валят CI |
| `paths-ignore` для docs-only PR | `ci.yml`/`quality.yml` публикуют **защищённые** контексты (`test (3.10/3.11/3.12)`, `Tests (ubuntu-latest, py3.12)`, `CI-Gate`, `quality-gate`). Пропущенный по `paths-ignore` workflow не отчитается никогда, и PR останется заблокированным навсегда | Дешёвый путь — не пропускать, а сокращать: покрытие на одной ноге, полная матрица только на `main` |
| Кэш `~/.cache/pre-commit` в `quality.yml` | Этот job не запускает pre-commit: он ставит `ruff==0.4.10 black==24.4.2 isort==5.13.2` напрямую. Кэшировать нечего | Если `pre-commit run --all-files` когда-нибудь заменит ручные шаги — кэш добавляется строкой `actions/cache` |
| Кэш собранных дисковых образов (fixtures) | Для этого нужно вынести сборку образов в отдельный скрипт с ключом от хэша, а сейчас `mke2fs`/`mkfs.ntfs` вызываются внутри тестов. Переделка тестовой инфраструктуры — отдельная работа с риском для replay-изоляции | Оставлено как задача; ниже — как её делать безопасно |
| `pytest-testmon` / `pytest-watch` | Новые зависимости требуют `uv lock`, а lockfile-правки не должны ехать в PR про инструменты | `make test-changed` покрывает 80 % сценария без новой зависимости |
| Сплит аудита `docs/audits/issue-510-audit-2026-11-21.md` (42.9 КБ) | Разрезание существующего документа меняет ссылки в треде #510 | Новые аудиты складывать в `docs/audits/YYYY-MM-DD/` по разделам |

## 3. Порядок внедрения (ROI)

| # | Шаг | Трудозатраты | Что ускоряет | Состояние |
|---|---|---|---|---|
| 1 | `make test-fast` с `--durations`, `make test-changed`, `make lint-fast`, `make fmt-check` | 15 мин | каждый прогон | внедрено |
| 2 | `scripts/tracker_status.py` + `make tracker-status` | полдня | мета-работу вокруг #510 | внедрено |
| 3 | `AGENTS.md` | полдня | новые тесты и AI-правки | внедрено |
| 4 | `scripts/new_acceptance_item.py` | полдня | новые acceptance-пункты | внедрено |
| 5 | `--cov` только на одной ноге | 10 мин | −20-30 % на 7 из 8 ног | внедрено |
| 6 | uv-кэш и `uv sync --frozen` локально (`make dev-sync`) | 1 час | установку окружения | внедрено |
| 7 | Кэш дисковых образов-фикстур | 1 день | все replay-тесты | отложено |
| 8 | `ruff format` вместо black+isort | 1 PR | правка → коммит | отложено |

## 4. Параллельная работа

Один worktree на задачу: переключение ветки не должно пересобирать окружение.

```bash
git worktree add ../mount-file-c6 -b feat/c6-oracle
```

## 5. Источники

- uv в GitHub Actions — <https://docs.astral.sh/uv/guides/integration/github/>
- Ускорение CI через uv — <https://hugovk.dev/blog/2024/speed-up-ci-with-uv/>
- uv — <https://github.com/astral-sh/uv>
- Современный тулинг Python (uv/ruff/mypy) — <https://softaims.com/blog/modern-python-tooling-uv-ruff-mypy-2026>
- ruff-pre-commit — <https://github.com/astral-sh/ruff-pre-commit>
- Приёмы ускорения pytest — <https://github.com/zupo/awesome-pytest-speedup>
- PyPI suite на 81 % быстрее (xdist) — <https://blog.trailofbits.com/2025/05/01/making-pypis-test-suite-81-faster/>
- Copier (шаблоны с обновлением) — <https://copier.readthedocs.io/en/stable/comparisons/>
- AI-ассистенты 2026: скорость и риск — <https://www.kusari.dev/blog/ai-coding-assistants-in-2026-4x-faster-10x-riskier-the-hidden-security-cost/>
