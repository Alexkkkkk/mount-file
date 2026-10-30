# MountFile automation contract

Этот документ описывает безопасную автоматизацию репозитория. Она должна ускорять диагностику и исправление, но не превращать filesystem-код в неконтролируемый auto-merge поток.

## Контуры

| Контур | Запуск | Действие | Может менять source? | Может merge? |
|---|---|---|---:|---:|
| CI | push/PR/nightly | Python 3.10–3.12, Linux/Windows/macOS, integration, GUI, build | нет | нет |
| Quality | push/PR/manual | ruff, black, isort, compileall, actionlint | нет | нет |
| Security | push/PR/weekly/manual | CodeQL, dependency review, pip-audit, unified `security-gate` | нет | нет |
| Autofix | main/manual/ChatOps | только isort + black, затем тесты | PR | только через обычные gates |
| Self-heal | failure trusted main + opt-in | анализ логов и предложение patch PR | PR | нет |
| CI Guardian | workflow completion | deduplicated incident issue, recovery close | issues only | нет |
| mfsync | daily/manual/PR | secret scan, version parity, sync status, optional explicit sync | только manual write mode | нет |
| Release | successful tested main | version PR/tag/build/release assets | PR/tag | только ruleset |

## Автоисправления

Автоисправления разделены на два безопасных уровня:

1. `autofix.yml` применяет только воспроизводимые форматтеры. Он не запускает semantic `ruff --fix`, не удаляет re-export imports и не пушит изменения напрямую в `main`; результатом является PR.
2. `self-heal.yml` может анализировать падение CI и создать patch PR только после включения repository variable `ENABLE_AI_AUTOMATION=true` и настройки `OPENAI_API_KEY`. `AUTO_APPROVE_PATCHES=false`: AI не имеет права самостоятельно approve или merge исправление.

Любой созданный PR проходит CI, Quality, Security, mfsync и branch protection. Исправления parser/writer нельзя включать через обход этих gates.

## CI Guardian

`ci-guardian.yml` слушает завершение trusted workflow для `main`. При `failure`, `cancelled` или `timed_out` он создаёт одну deduplicated issue с labels `automation` и `ci-failure`, добавляет ссылку на run и commit, а повторные падения добавляет комментариями. После успешного запуска того же workflow открытая incident issue закрывается с указанием recovery commit.

Guardian не редактирует исходники и не закрывает PR. Это операционный контур обнаружения, а не скрытая самопочинка.

## Security policy

`pip-audit` является настоящим blocking gate: ошибка vulnerability scan больше не подавляется `|| true`. `security-gate` агрегирует CodeQL, dependency review и pip-audit; на PR все применимые проверки обязаны завершиться успешно, а manual/push режимы корректно пропускают только неприменимый dependency review. Auto-merge ждёт именно этот единый gate. Если dependency audit требует временного исключения, оно должно быть оформлено явно в repository policy и отдельном reviewed PR, а не скрыто в workflow.

Все third-party Actions закреплены полными commit SHA. Workflow permissions минимальны по умолчанию. Maintenance workflows используют общий concurrency lock и не применяют force-push.

## Автосинхронизация

GitHub Actions не видит несохранённые файлы на компьютере разработчика. Поэтому синхронизация разделена:

- локальный `python tools/mfsync.py watch --pr --debounce 5` синхронизирует рабочую копию при сохранении;
- scheduled GitHub `mfsync` проверяет состояние, версии и секреты;
- запись в remote через GitHub Actions возможна только явным `workflow_dispatch` после успешного Verify;
- при divergence выполняется PR, а не force-push.

Это предотвращает ложное обещание «GitHub сам увидит любой файл на компьютере» и защищает forensic-данные от случайного попадания в Git.

## Включение unattended режимов

Безопасный базовый режим работает без секретов. Для полностью unattended PR потребуется отдельный fine-grained `AUTOMATION_TOKEN` с минимальными правами `Contents: write`, `Pull requests: write`, `Actions: write` при необходимости. Личные токены нельзя помещать в код, URL, issue или логи.

AI self-heal включается отдельно и явно:

```text
Repository variable: ENABLE_AI_AUTOMATION=true
Repository secret:   OPENAI_API_KEY=<configured directly in GitHub>
Optional secret:     AUTOMATION_TOKEN=<fine-grained repo token>
```

Включение AI не даёт ему права обходить review, required checks или branch protection.

## Операционная проверка

```bash
python -m pytest tests/test_automation_workflows.py tests/test_mfsync.py tests/test_release_version.py -q
python tools/mfsync.py scan --all
python tools/mfsync.py check --strict
python -m compileall -q mountfile tests main.py
```

Для workflow syntax используется checksum-pinned `actionlint` в `quality.yml`; локальный результат без GitHub runner не заменяет реальный matrix run.
