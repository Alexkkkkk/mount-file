# Чеклист оставшихся задач по #510: владельцы и сроки

> Трекер: [#510 — Replay completion checklist: ext4/JBD2 + NTFS $LogFile](https://github.com/Alexkkkkk/mount-file/issues/510).
> Состояние на 2026-11-26. Данные (assignees, milestone, labels, статусы манифеста) взяты из GitHub API и `docs/replay-acceptance.json` на `main` (`c3bc327`).
>
> **Оговорка о полноте данных:** в репозитории фактически назначен только один владелец — `Alexkkkkk` (assignee трекера #510). Milestone «Replay acceptance: ext4/JBD2 + NTFS $LogFile» не имеет due date (`due_on: null`), в манифесте нет полей owner/deadline. Поэтому прочерки в колонках «Владелец»/«Срок» означают «данные отсутствуют в репозитории», а не «владелец не нужен».

| № | Задача | Статус | Владелец | Срок | Issue / PR |
|---|--------|--------|----------|------|------------|
| 1 | Слить PR #673 (синхронизация 2.0.0; README-фикс `120e30a` запушен, гейты зелёные) | 🟡 открыт, не слит | не назначен | — | [#673](https://github.com/Alexkkkkk/mount-file/pull/673) |
| 2 | Обновить срез `docs/replay-acceptance.json` после мержей (сейчас `abadf8c`, `main` на `c3bc327`; после #673 — версия `2.0.0`) | 🟡 дрейф на 1+ коммит | не назначен | — | [manifest](https://github.com/Alexkkkkk/mount-file/blob/main/docs/replay-acceptance.json) |
| 3 | Второй последовательный зелёный scheduled-прогон Writer Failure Matrix на актуальном HEAD (первый — №15 от 26.11, `success`) | 🟡 1 из 2 | не назначен | — | [Actions](https://github.com/Alexkkkkk/mount-file/actions) |
| 4 | Синхронизировать текст issue #510 с новым срезом манифеста | 🔴 не начато | Alexkkkkk (assignee #510) | — | [#510](https://github.com/Alexkkkkk/mount-file/issues/510) |
| 5 | Согласовать сводку машинных гейтов (`scripts/replay_acceptance_status.py` ↔ `scripts/acceptance_blocks.py`) | 🔴 не начато | не назначен | — | [manifest](https://github.com/Alexkkkkk/mount-file/blob/main/docs/replay-acceptance.json) |
| 6 | **C3** — независимое подтверждение redo/undo для `$MFT`, `$BITMAP`, индексов | 🟡 partial (манифест) | не назначен | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 7 | **C5** — доказательства выделения/переиспользования MFT-записей и согласованности `$MFTMirr` | 🟡 partial (манифест) | не назначен | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 8 | **C6** — Windows crash-фикстуры: disposable VHDX + реальный `chkdsk`, побайтное сравнение; фактический запуск на `windows-latest` | 🟡 partial (манифест) | не назначен | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 9 | **C4** (`$ATTRIBUTE_LIST`) — сохранить явный typed refusal до мутации, поддержку не заявлять | ⛔ blocked by design — действий нет | не назначен | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 10 | **D1** — полный охват failure injection по всем фазам (data, bitmap, directory, inode, journal, commit) | 🟡 partial (манифест) | не назначен | — | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 11 | **D3** — hash preservation оригинала на каждом пути отказа | 🟡 partial (манифест) | не назначен | — | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 12 | **D5** — независимые checker + read-back после каждой поддержанной операции | 🟡 partial (манифест) | не назначен | — | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 13 | **D6** — валидные JUnit/scheduled-артефакты матрицы profile × operation × state × checker | 🟡 partial (манифест) | не назначен | — | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 14 | **E1** — воспроизводимые Windows/macOS evidence; macOS-контракт `macos_read_only_adapter_contract` сейчас `not_collected`; недоступность среды = typed `unavailable`, не pass | 🟡 partial (манифест) | не назначен | — | [#389](https://github.com/Alexkkkkk/mount-file/issues/389) |
| 15 | Закрыть #385 — нативный ext3/ext4 writer (JBD2 transaction/recovery) | 🔴 open, label `blocked` | не назначен | — | [#385](https://github.com/Alexkkkkk/mount-file/issues/385) |
| 16 | Закрыть #386 — нативный NTFS writer (LogFile transaction recovery) | 🔴 open, label `blocked` | не назначен | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 17 | Закрыть #387 — полная acceptance-матрица (failure injection, process kill, corruption) | 🔴 open | не назначен | — | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 18 | Закрыть #389 — кросс-платформенные фикстуры и независимые checker CI | 🔴 open | не назначен | — | [#389](https://github.com/Alexkkkkk/mount-file/issues/389) |
| 19 | Закрыть #390 — benchmark-матрица + синхронизация статуса продукта (вкл. README-таблицу) | 🔴 open | не назначен | — | [#390](https://github.com/Alexkkkkk/mount-file/issues/390) |
| 20 | Ограничение: не менять product-status на writable, не заявлять production parity / power-loss guarantees до закрытия всех пробелов | ⛔ действует постоянно | Alexkkkkk (assignee #510) | — | [#510](https://github.com/Alexkkkkk/mount-file/issues/510), `closure_rule` манифеста |

## Рекомендация

Назначить ответственных по блокам (C → #386, D → #387, E → #389) и выставить due date у milestone — иначе критерий закрытия #510 не имеет измеримого дедлайна.

## Ограничения (из `closure_rule` манифеста и критерия закрытия issue)

- Product-level ext3/ext4 и NTFS write/replay остаются `read-only` / `unsupported`, пока не закрыты пробелы выше.
- Не заявлять production parity или power-loss guarantees.
- C4 остаётся явным отказом (fail-closed), а не задачей «на доработку».
