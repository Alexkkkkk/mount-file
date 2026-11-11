# Автоматика веток

Workflow `Branch hygiene` запускается ежедневно и вручную через GitHub Actions. Он получает список remote-веток и открытых pull request, фиксирует commit SHA, дату последнего commit, возраст ветки и связь с PR.

По умолчанию workflow только читает состояние репозитория и публикует два артефакта: `branch-hygiene.json` для машинной обработки и `branch-hygiene.md` для просмотра. За stale-кандидат считается ветка старше заданного порога без открытого PR. `main`, `master`, default branch и `release/*` всегда защищены.

Ни одна ветка не удаляется, не force-push-ится и не перебазируется автоматически. При ручном запуске можно включить `fail_on_stale=true`; тогда workflow завершится ошибкой, если найден stale-кандидат, но всё равно ничего не изменит. Это позволяет использовать аудит как осознанный gate перед ручной очисткой.

Локальный запуск требует только Python standard library и GitHub token:

```bash
GITHUB_REPOSITORY=Alexkkkkk/mount-file \
GITHUB_TOKEN="$GH_TOKEN" \
python scripts/branch_hygiene.py \
  --json-out /tmp/branch-hygiene.json \
  --markdown-out /tmp/branch-hygiene.md
```

Локальная синхронизация рабочих файлов по-прежнему принадлежит `mfsync` (`make sync`, `make sync-pr`, `make sync-watch`). Branch hygiene не пытается видеть незакоммиченные файлы на компьютере пользователя и не заменяет `mfsync`.
