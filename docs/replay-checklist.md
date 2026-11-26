# Чеклист оставшихся задач по #510: владельцы и сроки

> Трекер: [#510 — Replay completion checklist: ext4/JBD2 + NTFS $LogFile](https://github.com/Alexkkkkk/mount-file/issues/510).
> Состояние на 2026-11-26. Данные (assignees, milestone, labels, статусы манифеста) взяты из GitHub API и `docs/replay-acceptance.json` на `main` (`c3bc327`).
>
> **Ответственность и дедлайн (обновлено 2026-11-26):** все блоки ведёт `Alexkkkkk` — единственный коллаборатор репозитория с правом push, назначен assignee на #510, [#386](https://github.com/Alexkkkkk/mount-file/issues/386), [#387](https://github.com/Alexkkkkk/mount-file/issues/387), [#389](https://github.com/Alexkkkkk/mount-file/issues/389). Milestone «Replay acceptance: ext4/JBD2 + NTFS $LogFile» — дедлайн **2026-12-31** (подтверждено API: `due_on: 2026-12-31T00:00:00Z`).

| № | Задача | Статус | Владелец | Срок | Issue / PR |
|---|--------|--------|----------|------|------------|
| 1 | Слить PR #673 (синхронизация 2.0.0; README-фикс `120e30a` запушен, гейты зелёные) | 🟡 открыт, не слит | Alexkkkkk | ≤ 2026-12-05 | [#673](https://github.com/Alexkkkkk/mount-file/pull/673) |
| 2 | Обновить срез `docs/replay-acceptance.json` после мержей (сейчас `abadf8c`, `main` на `c3bc327`; после #673 — версия `2.0.0`) | 🟡 дрейф на 1+ коммит | Alexkkkkk | ≤ 2026-12-05 | [manifest](https://github.com/Alexkkkkk/mount-file/blob/main/docs/replay-acceptance.json) |
| 3 | Второй последовательный зелёный scheduled-прогон Writer Failure Matrix на актуальном HEAD (первый — №15 от 26.11, `success`) | 🟡 1 из 2 | Alexkkkkk | ≤ 2026-12-12 | [Actions](https://github.com/Alexkkkkk/mount-file/actions) |
| 4 | Синхронизировать текст issue #510 с новым срезом манифеста | 🔴 не начато | Alexkkkkk (assignee #510) | ≤ 2026-12-05 | [#510](https://github.com/Alexkkkkk/mount-file/issues/510) |
| 5 | Согласовать сводку машинных гейтов (`scripts/replay_acceptance_status.py` ↔ `scripts/acceptance_blocks.py`) | 🔴 не начато | Alexkkkkk | ≤ 2026-12-12 | [manifest](https://github.com/Alexkkkkk/mount-file/blob/main/docs/replay-acceptance.json) |
| 6 | **C3** — независимое подтверждение redo/undo для `$MFT`, `$BITMAP`, индексов | 🟡 partial (манифест) | Alexkkkkk (assignee #386, блок C) | ≤ 2026-12-19 | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 7 | **C5** — доказательства выделения/переиспользования MFT-записей и согласованности `$MFTMirr` | 🟡 partial (манифест) | Alexkkkkk (assignee #386, блок C) | ≤ 2026-12-19 | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 8 | **C6** — Windows crash-фикстуры: disposable VHDX + реальный `chkdsk`, побайтное сравнение; фактический запуск на `windows-latest` | 🟡 partial (манифест) | Alexkkkkk (assignee #386, блок C) | ≤ 2026-12-26 | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 9 | **C4** (`$ATTRIBUTE_LIST`) — сохранить явный typed refusal до мутации, поддержку не заявлять | ⛔ blocked by design — действий нет | Alexkkkkk (assignee #386, блок C) | — | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 10 | **D1** — полный охват failure injection по всем фазам (data, bitmap, directory, inode, journal, commit) | 🟡 partial (манифест) | Alexkkkkk (assignee #387, блок D) | ≤ 2026-12-19 | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 11 | **D3** — hash preservation оригинала на каждом пути отказа | 🟡 partial (манифест) | Alexkkkkk (assignee #387, блок D) | ≤ 2026-12-19 | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 12 | **D5** — независимые checker + read-back после каждой поддержанной операции | 🟡 partial (манифест) | Alexkkkkk (assignee #387, блок D) | ≤ 2026-12-26 | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 13 | **D6** — валидные JUnit/scheduled-артефакты матрицы profile × operation × state × checker | 🟡 partial (манифест) | Alexkkkkk (assignee #387, блок D) | ≤ 2026-12-26 | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 14 | **E1** — воспроизводимые Windows/macOS evidence; macOS-контракт `macos_read_only_adapter_contract` сейчас `not_collected`; недоступность среды = typed `unavailable`, не pass | 🟡 partial (манифест) | Alexkkkkk (assignee #389, блок E) | ≤ 2026-12-26 | [#389](https://github.com/Alexkkkkk/mount-file/issues/389) |
| 15 | Закрыть #385 — нативный ext3/ext4 writer (JBD2 transaction/recovery) | 🔴 open, label `blocked` | Alexkkkkk | ≤ 2026-12-31 | [#385](https://github.com/Alexkkkkk/mount-file/issues/385) |
| 16 | Закрыть #386 — нативный NTFS writer (LogFile transaction recovery) | 🔴 open, label `blocked` | Alexkkkkk | ≤ 2026-12-31 | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| 17 | Закрыть #387 — полная acceptance-матрица (failure injection, process kill, corruption) | 🔴 open | Alexkkkkk | ≤ 2026-12-31 | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| 18 | Закрыть #389 — кросс-платформенные фикстуры и независимые checker CI | 🔴 open | Alexkkkkk | ≤ 2026-12-31 | [#389](https://github.com/Alexkkkkk/mount-file/issues/389) |
| 19 | Закрыть #390 — benchmark-матрица + синхронизация статуса продукта (вкл. README-таблицу) | 🔴 open | Alexkkkkk | ≤ 2026-12-31 | [#390](https://github.com/Alexkkkkk/mount-file/issues/390) |
| 20 | Ограничение: не менять product-status на writable, не заявлять production parity / power-loss guarantees до закрытия всех пробелов | ⛔ действует постоянно | Alexkkkkk (assignee #510) | до закрытия #510 | [#510](https://github.com/Alexkkkkk/mount-file/issues/510), `closure_rule` манифеста |

## Рекомендация — выполнена 2026-11-26

- Ответственные по блокам назначены: C → [#386](https://github.com/Alexkkkkk/mount-file/issues/386), D → [#387](https://github.com/Alexkkkkk/mount-file/issues/387), E → [#389](https://github.com/Alexkkkkk/mount-file/issues/389); `Alexkkkkk` — assignee всех трёх (единственный коллаборатор с push-доступом, подтверждено `GET /collaborators`).
- Дедлайн milestone выставлен: **2026-12-31** (`PATCH /milestones/1` → `due_on: 2026-12-31T00:00:00Z`).
- Промежуточные сроки в таблице (≤ 05.12 / ≤ 12.12 / ≤ 19.12 / ≤ 26.12) — рабочая разбивка до дедлайна milestone; они зафиксированы в этом документе, а не в GitHub milestones (GitHub не поддерживает по-фазные due dates внутри одного milestone).

## Измеримый критерий закрытия #510

Issue закрывается, когда одновременно:

1. `docs/replay-acceptance.json` указывает срез актуального `main` (commit + версия) и `scripts/replay_acceptance_status.py --check` / `scripts/acceptance_blocks.py` дают согласованные числа без расхождений (exit 0);
2. в манифесте нет пунктов со статусом `partial` (C3, C5, C6, D1, D3, D5, D6, E1 закрыты как `verified`; C4 остаётся `blocked` как намеренный отказ);
3. два последовательных scheduled-прогона Writer Failure Matrix на этом HEAD зелёные, артефакты `schedule_eligible=true`;
4. Windows/macOS evidence воспроизводимы: macOS-контракт собран (или явно typed `unavailable`), Windows parity подтверждена запуском на `windows-latest`;
5. блокеры #385, #386, #387, #389, #390 закрыты.

Дедлайн: **2026-12-31**.

## Ограничения (из `closure_rule` манифеста и критерия закрытия issue)

- Product-level ext3/ext4 и NTFS write/replay остаются `read-only` / `unsupported`, пока не закрыты пробелы выше.
- Не заявлять production parity или power-loss guarantees.
- C4 остаётся явным отказом (fail-closed), а не задачей «на доработку».
