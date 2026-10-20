# Синхронизация кода с GitHub

`tools/mfsync.py` — один локальный инструмент, который закрывает всю рутину
вокруг синхронизации рабочей копии с удалённым репозиторием:

```bash
python tools/mfsync.py status      # ветка, ahead/behind, изменения, версия
python tools/mfsync.py sync        # fetch + rebase + commit + push одной командой
python tools/mfsync.py sync --pr   # то же, но через pull request
python tools/mfsync.py watch --push   # авто-синхронизация при сохранении файла
python tools/mfsync.py scan --all  # проверка на утёкшие секреты
```

Требований к окружению три: Python 3.8+, установленный `git` и — только для
режима PR — токен в переменной окружения. Сторонних зависимостей нет
(только stdlib), поэтому инструмент работает и на Windows 7 из
`requirements-win7.txt`, и в CI.

## Зачем это, а не `git pull && git push`

| Проблема | Что делает `mfsync` |
|---|---|
| Забыли `git pull` перед работой | `sync` всегда начинает с `fetch --prune` + `rebase @{u}` |
| Незакоммиченные правки мешают rebase | автоматический `stash --include-untracked` и восстановление после rebase |
| Конфликт оставил репозиторий в `rebase` | конфликт откатывается (`rebase --abort`), stash возвращается, список файлов выводится |
| Некрасивая история `update`, `fix2`, `asdf` | сообщение генерируется из diff в формате Conventional Commits |
| Случайно закоммичен токен/`.env`/`*.pem` | сканер секретов блокирует commit до записи в историю |
| Правила `main` запрещают push (GH013) | push автоматически уходит в ветку `sync/*` и открывает PR |
| Версия в `pyproject.toml` и `__init__.py` разошлась | `status`/`check` показывают расхождение и сверяют с последним тегом |
| Два наблюдателя синхронизируют один репозиторий | файловый lock `.git/mfsync.lock` |

## Команды

### `status`

Показывает ветку и upstream, `ahead/behind`, изменённые файлы, совпадение
версий (`pyproject.toml` ↔ `mountfile/__init__.py` ↔ последний тег) и наличие
токена в окружении. `--json` — для машинной обработки и для CI.

### `sync`

```bash
python tools/mfsync.py sync                       # авто-сообщение коммита
python tools/mfsync.py sync -m "fix(rw): cluster chain"
python tools/mfsync.py sync --type fix --scope fat32
python tools/mfsync.py sync --no-push             # только локальный коммит
python tools/mfsync.py sync --pr --base main      # PR вместо push в main
```

Порядок шагов неизменен: `fetch` → `rebase` (со stash) → сканер секретов →
`commit` → `push`. Если push отклонён защитой ветки, инструмент повторяет
публикацию в ветку `sync/<branch>-<timestamp>` и открывает pull request.

### `watch`

```bash
python tools/mfsync.py watch                 # коммит после паузы в 2 с
python tools/mfsync.py watch --push          # коммит + push
python tools/mfsync.py watch --pr            # коммит + PR
python tools/mfsync.py watch --debounce 5    # ждать 5 с тишины
```

Наблюдатель считает снимок дерева (mtime + размер) и срабатывает только когда
файлы перестали меняться — серия сохранений из IDE даёт один коммит, а не
десять. Игнорируются `build/`, `dist/`, `__pycache__/`, `*.img`, `*.bin`,
`.venv/` и прочие артефакты из `.gitignore`.

### `pull`, `push`, `commit`, `pr`, `scan`, `check`

Отдельные команды для тех же операций — удобно встраивать в свои скрипты:

```bash
python tools/mfsync.py pull --merge      # merge вместо rebase
python tools/mfsync.py push --force-with-lease
python tools/mfsync.py commit -m "docs: sync guide"
python tools/mfsync.py pr --base main
python tools/mfsync.py scan --all --json
python tools/mfsync.py check --strict     # exit 1 при расхождении версий
```

## Секреты

Токен читается из `GH_TOKEN`, `GITHUB_TOKEN` или `MFSYNC_TOKEN` в момент
вызова. Он не пишется ни в `.git/config`, ни в отчёты, ни в вывод: любая
строка перед печатью проходит через `_redact()`, а URL вида
`https://x-access-token:...@github.com/...` маскируется.

```bash
# bash / zsh
export GH_TOKEN="$(cat ~/.config/gh/token)"
# PowerShell
$env:GH_TOKEN = Get-Content "$HOME\.config\gh\token"
```

Сканер перед коммитом проверяет **только те файлы, которые входят в коммит**
(не всю историю), и распознаёт: `github_pat_*`, `ghp_*`/`gho_*`, ключи AWS
`AKIA*`, токены Slack `xox[baprs]-*`, `sk-*`, заголовки приватных ключей и
присваивания `password = "..."`/`api_key: "..."`.

Ложные срабатывания снимаются двумя способами:

* строка-документация с плейсхолдером (`your_token_here`, `${API_KEY}`,
  `<token>`, `changeme`) не считается секретом;
* явный маркер `# pragma: allowlist secret` в конце строки.

Файлы `.env`, `*.pem`, `*.key`, `*.p12`, `id_rsa`, `.netrc` и подобные
блокируются по имени.

## Ветки и защита `main`

Если в репозитории включён ruleset «Changes must be made through a pull
request», прямой push вернёт `GH013`. `mfsync` распознаёт это и переключается
на PR-режим — либо сразу, если указан `--pr`:

```bash
python tools/mfsync.py sync --pr
```

PR создаётся через REST API (`POST /repos/{owner}/{repo}/pulls`) с заголовком и
телом, собранными из `git log` относительно базовой ветки.

## CI

`.github/workflows/sync-guard.yml` запускает на каждый push и PR:

* `mfsync check --strict` — расхождение версий ломает сборку;
* `mfsync scan --all` — утёкший секрет в отслеживаемом файле ломает сборку;
* `mfsync status --json` — отчёт в лог job'а.

Секреты в workflow не нужны: используются только `GITHUB_TOKEN` от Actions
для чтения репозитория.

## Автозапуск в GitHub Actions

`.github/workflows/mfsync.yml` запускает `mfsync` в CI на встроенном
`secrets.GITHUB_TOKEN` — личный PAT в workflow не используется ни на одной
строке.

| Событие | Что выполняется |
|---|---|
| `push` в `main`, `feat/**`, `sync/**` | job **Verify**: `status`, `scan --all`, `check --strict`, self-tests, JSON-артефакт, summary. Только чтение |
| `pull_request` в `main` | то же самое — это required check для PR |
| `schedule` (03:17 UTC) | **Verify** + **Autosync** с `commit` |
| `workflow_dispatch` с `mode=verify` | только **Verify** |
| `workflow_dispatch` с `mode=commit` / `mode=pr` | **Verify** + **Autosync** (push или PR) |

Локальные режимы (`watch`, интерактивный `sync`) в CI не запускаются: у job'а
нет терминала, а watcher завершился бы по таймауту. Поэтому workflow вызывает
только конечные команды `status`, `scan`, `check`, `sync`.

Защита от циклов — четыре независимых слоя:

1. write-job недостижим по `push`: его `if` требует `workflow_dispatch` или
   `schedule`, а `mode=verify` его пропускает;
2. коммиты бота содержат `[skip ci]`;
3. `concurrency: mfsync-write` сериализует писателей, `cancel-in-progress:
   false`;
4. job наследует права только `contents: write` + `pull-requests: write`.

Если `main` защищён ruleset'ом, `mfsync` сам уходит в ветку `sync/*` и открывает
PR — для этого в job'е и запрошено `pull-requests: write`.

## Make-цели

```bash
make sync          # pull + commit + push
make sync-status   # статус
make sync-check    # проверка версий
make sync-pr       # синхронизация через PR
make sync-watch    # наблюдатель с push
```

## Тесты

```bash
python -m pytest tests/test_mfsync.py -v
```

29 тестов гоняют настоящий `git` во временном репозитории с локальным bare
remote: rebase с autostash, конфликт и чистый откат, блокировку секретов,
генерацию сообщений, fallback на PR, lock и наблюдатель. Сети не требуется.
