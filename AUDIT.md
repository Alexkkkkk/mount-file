# Аудит состояния репозитория mount-file

**Дата:** 2026-11-21 · **Срез:** `main` @ `96bda60` · **Версия:** `1.100.1`
**Источник:** GitHub API (`gh issue list`, `gh pr list`, `gh api .../milestones`) + локальный клон `origin/main`.

## 1. Сводка

| Показатель | Значение |
|---|---|
| Открытых issue | **8** |
| Закрытых issue | 81 |
| Открытых PR | **1** (#593) |
| Закрытых / влитых PR | 501 |
| Milestones | 1 (#1 «Replay acceptance: ext4/JBD2 + NTFS $LogFile») |
| Коммитов после v1.95.0 (`d31b4dc`) | 74 |
| Собрано тестов в этом окружении | 2836 (13 ошибок коллекции — нет части зависимостей) |

**Главный вывод: ни один из 8 открытых issue не готов к закрытию.** Продуктовый статус остаётся консервативным: native write — только ограниченные профили FAT12/16, FAT32, ext2 и one-FAT exFAT; ext3/ext4 и NTFS — read-only by design.

## 2. Готовность по открытым issue

| Issue | Тема | Готовность | Что уже сделано | Что блокирует |
|---|---|---|---|---|
| #384 | [EPIC] native filesystem до production-ready | 🔴 не готов | Определён контракт: native write только FAT12/16, FAT32, ext2, one-FAT exFAT; ext3/ext4 и NTFS — read-only по дизайну | Дочерние #385, #386, #387, #389, #390, #530 открыты |
| #385 | ext3/ext4 writer + JBD2 | 🟡 частично | `ext4_journal_gate.py`, `ext4_native_writer.py`, `jbd2_checkpoint.py`; тесты `test_jbd2_writer.py`, `test_jbd2_csum_replay.py`, `test_jbd2_checkpoint.py`, `test_ext3_jbd2_sequence_rollover.py`, `test_jbd2_malformed_journal_refusal.py` (последний уже в `main`) | Checkpoint-план только планируется, образ не мутирует; checksummed-журналы через `allow_checksums=True` не включены |
| #386 | NTFS writer + `$LogFile` replay | 🟡 частично | `ntfs_logfile.py`, `ntfs_replay.py`, `ntfs_replay_executor.py`, `ntfs_native_writer.py`, `quarantine_ntfs_image()`; `tests/ntfs_crash_fixture.py` и `test_ntfs_replay*.py` | Нет независимого оракула: сравнение только с собственным парсером; `$ATTRIBUTE_LIST` — отказ по дизайну |
| #387 | Матрица приёмки: failure injection, process-kill, quarantine | 🔴 не готов | Fault seam в коде; ext4 process-kill тесты | #530 требует два ночных прогона без расхождений; матрица в nightly не вынесена в полном объёме |
| #389 | Кросс-платформенные фикстуры и checker CI | 🔴 не готов | `checker_matrix.py`, `cross_platform_evidence.py`, `native_read_evidence.py`; Windows-раннеры (`ci.yml:482`, `ci-nightly.yml:18`, `release.yml:92`) | macOS-раннера нет ни в одном workflow (`grep -rniE 'runs-on: macos' .github/` → пусто); `macos_read_only_adapter_contract` → `not_collected` |
| #390 | Benchmark matrix и синхронизация статуса | 🟡 частично | `product_status_contract.py` → `verdict: pass`; `verify_generated_status.py` → артефакты совпадают с генератором, exit 0; `benchmark_artifact.py`, провенанс-тесты | Job `Verify repository status` отсутствует в required checks `main`, поэтому рассинхронизация не блокирует мерж |
| #530 | Чек-лист P1–P4 (production readiness) | 🔴 не готов | Составлен на baseline v1.100.1; 19 пунктов с конкретными файлами и командами | **0 из 19** чекбоксов закрыто |
| #87 | [EPIC] roadmap DFIR-инструмента | 🔴 не готов | Актуализирован на `96bda60` / v1.100.1 | Все 7 связанных дочерних issue открыты (0/7) |

## 3. Детализация

### #384 — [EPIC] native filesystem до production-ready профилей
`labels: core, epic, priority:high, pinned`

Эпик-родитель для #385, #386, #387, #389, #390, #530. Подтверждённое состояние по телу issue: основной suite `2258 passed, 98 skipped`; после установки независимых инструментов real-image acceptance — filesystem `64 passed`, NTFS follow-up `62 passed`, DFIR/build `123 passed`. Native write — только ограниченные профили. Закрывать нечего: все дочерние задачи открыты.

### #385 — ext3/ext4 writer + JBD2
`labels: enhancement, core, priority:high, blocked`

Acceptance по телу: «Writer остаётся disabled до positive/negative fixtures, rollback, process-kill recovery, malformed-journal refusal и independent-checker tests. До этого ext3/ext4 остаются read-only.»

Готово: journal gate на `RECOVER`, независимый чекер `e2fsck -fn` на disposable copy, descriptor/data/commit/revoke-записи, sequence rollover, checkpointing-планировщик, malformed-journal refusal (`tests/test_jbd2_malformed_journal_refusal.py` уже присутствует в `main`).
Не готово: применение checkpoint-плана (сейчас `mountfile/jbd2_checkpoint.py` только планирует), включение checksummed-журналов через `allow_checksums=True`, полная negative-fixture матрица.

### #386 — NTFS writer + `$LogFile`
`labels: enhancement, core, priority:high, blocked`

Acceptance: «NTFS mutation нельзя включать до transaction/replay tests и image-hash preservation на каждом отказе.»

Готово: парсер `$LogFile` (`LogClient`, RCRD, USA-валидация), bounded replay executor, карантин образа по SHA-256, read-only MIT-совместимый адаптер `dfir_ntfs`.
Не готово: независимый оракул — сравнение идёт с собственным парсером, а не с эталоном после штатного `chkdsk`; `$ATTRIBUTE_LIST`, MFT record reuse, `$MFTMirr` — за рамками.

### #387 — матрица приёмки: failure injection, process-kill, quarantine
`labels: enhancement, tests, core, priority:high`

Acceptance: «`power_loss` или `process_kill` нельзя публиковать как supported без воспроизводимого теста. Unsupported cells должны быть typed refusals, а не скрытыми skips.»

Готово: точки инъекции отказов в писателях, process-kill тесты для ext4.
Не готово: систематическая матрица по всем включённым писателям и фазам, публикация артефакта `failure-matrix` из nightly, условие закрытия — два ночных прогона подряд без расхождений.

### #389 — кросс-платформенные фикстуры и checker CI
`labels: enhancement, ci, tests, priority:medium`

Acceptance: «Missing checker должен быть `unavailable` и не может превращать parity в pass. Каждый supported profile должен иметь independent oracle или явно документированное ограничение.»

Готово: typed-вердикты чекеров (`CLEAN`, `NOT_VERIFIED_CLEAN`, `INCONCLUSIVE`, `UNAVAILABLE`, `TIMED_OUT`), матрица чекеров, Windows-раннеры, машинно-читаемые артефакты провенанса.
Не готово: **macOS-раннера нет ни в одном workflow**; `macos_read_only_adapter_contract` → `not_collected` / `blocked`. Ключевое условие приёмки не выполнено.

### #390 — benchmark matrix и синхронизация статуса
`labels: documentation, enhancement, ci, priority:medium`

Acceptance: «Benchmark output должен запускаться из clean checkout. Status synchronization должен ломать CI, если docs или UI заявляют unsupported native writes или crash guarantees.»

Готово: контракт статуса проходит (`verdict: pass`, claims `native_ext3_ext4_write=unsupported`, `native_ntfs_write=unsupported`, `power_loss=unsupported`, `parity=not_claimed`); генератор статуса сверяется с закоммиченными артефактами и совпадает (exit 0).
Не готово: job `Verify repository status` отсутствует в required status checks ветки `main`, то есть рассинхронизация не блокирует мерж. Полный benchmark не подтверждён прогоном из clean checkout.

### #530 — чек-лист P1–P4
`labels: priority:high, checklist, blocked`

Закрыто **0 из 19** пунктов:

- [ ] `tests/ntfs_crash_fixture.py` — зафиксировать набор dirty-фикстур (N ≥ 5): обрыв записи в $MFT, $BITMAP, $LogFile, составные сценарии (commit без checkpoint и т.п.)
- [ ] Прогнать `publish_ntfs_replay` (`mountfile/ntfs_replay_executor.py`) на каждой фикстуре
- [ ] Сверить результат не с собственным парсером, а с внешним оракулом: `ntfsfix -n` + побайтовое сравнение `$MFT`/`$BITMAP` с эталоном, полученным штатным chkdsk-восстановлением
- [ ] Расхождение — ноль байт на всех фикстурах; расхождения фиксировать как баги replay- executor'а
- [ ] Зафиксировать evidence в `checker-matrix.json` (значение `pass`, а не `unavailable`)
- [ ] Закрыть checker-leg C6 в чек-листе #510
- [ ] Сгенерировать отказ после **каждого** шага транзакции: data → FAT/bitmap → directory/inode → journal → commit
- [ ] Process-kill (SIGKILL) в каждой фазе транзакции для каждого включённого писателя (FAT32, exFAT, ext2; далее ext4/NTFS по мере готовности)
- [ ] Ассерты на каждом отказе: sha256 оригинала неизменен; образ либо консистентен (checker pass), либо в карантине; частичных состояний нет
- [ ] Вынести матрицу в nightly workflow (`.github/workflows/ci-nightly.yml` уже есть) с публикацией артефакта failure-matrix (расширение D6, коммит bb85658)
- [ ] Закрыть #387 после двух ночных прогонов подряд без расхождений
- [ ] Реализовать применение checkpoint-плана (`mountfile/jbd2_checkpoint.py` — сейчас только планирует, образ не мутирует) в `mountfile/ext4_native_writer.py` за fail-closed гейтом (`MOUNTFILE_ENABLE_NATIVE_WRITE`)
- [ ] Тест на обрыв после commit-блока: расширение `tests/test_jbd2_malformed_journal_refusal.py`
- [ ] Включить checksummed-журналы через явный путь `allow_checksums=True` с тестами на descriptor/commit/revoke и sequence wrap (база — `tests/test_ext3_jbd2_sequence_rollover.py`)
- [ ] Верифицировать `e2fsck -fn` pass после recovery на копии + пост-запись (уже описано в `ext4_native_writer.py:15-26`) на checksummed-профиле
- [ ] Опубликовать macOS/Windows evidence для нативных read-путей (CI-легы macos-latest × 3.10/3.11/3.12 уже есть в `.github/workflows/ci.yml:40`)
- [ ] Различать `unavailable` / `pass` / `not_claimed` во всех матрицах (`scripts/sync_repository_status.py`)
- [ ] Обновить README.md:90-91 (ext3/ext4, NTFS → актуальный статус) **в том же PR**, который меняет поведение — гейт status-sync уже работает (E2, коммит 75d7e34)
- [ ] Включить обновление статуса в PR #528 «synchronize 1.96.0»

### #87 — [EPIC] roadmap DFIR-инструмента
`labels: epic, roadmap, v2.0, pinned`

Roadmap-эпик, актуализирован на `96bda60` / v1.100.1. Все 7 дочерних issue открыты — прогресса по чекбоксам нет (0/7).

## 4. Закрытые issue: характер

| Категория | Кол-во |
|---|---|
| Всего закрытых | 81 |
| `[automation]` (сгенерированы CI) | 71 |
| — из них `CI failed on main` | 39 |
| — из них `Quality failed on main` | 18 |
| — из них `mfsync failed on main` | 4 |
| Содержательные (не automation) | 10 |

Доля automation-шума — 88% закрытых issue. Отдельно: **#510 закрыт 2026-11-19**, но его чеклист унаследован в #530, где 0/19 — закрытие трекера не означает завершения replay.

## 5. Проверенные артефакты кода

| Файл | Наличие |
|---|---|
| `tests/ntfs_crash_fixture.py` | присутствует |
| `tests/test_jbd2_malformed_journal_refusal.py` | присутствует |
| `mountfile/ntfs_replay_executor.py` | присутствует |
| `mountfile/ntfs_native_writer.py` | присутствует |
| `mountfile/ext4_native_writer.py` | присутствует |
| `scripts/checker_matrix.py` | присутствует |
| `scripts/cross_platform_evidence.py` | присутствует |
| `scripts/product_status_contract.py` | присутствует |
| `scripts/verify_generated_status.py` | присутствует |
| `docs/generated/recovery-matrix.json` | присутствует |
| macOS-раннер в `.github/` | **отсутствует** |

## 6. Риски

1. **macOS-ветка не доказана.** Для #389 это прямое нарушение acceptance: у macOS-адаптера нет ни оракула, ни собранных данных.
2. **Незакрытый гейт статуса.** Без `Verify repository status` в required checks регрессия README/UI пройдёт молча.
3. **Оракул NTFS остаётся собственным.** Пока сравнение идёт с тем же парсером, что и replay, доказательство круговое.
4. **Шум автоматизации.** 71 из 81 закрытых issue — сгенерированные CI-инциденты; они маскируют содержательную историю.

## 7. Что делать по порядку

1. #389 — добавить macOS-раннер в evidence-job (список платформ в `cross_platform_evidence.py` уже включает `macos`).
2. #390 — вынести `Verify repository status` в required checks, переведя триггер на `pull_request`, чтобы чек отчитывался в head PR.
3. #530 — закрыть P1: `tests/ntfs_crash_fixture.py` (≥5 dirty-фикстур) + сравнение с эталоном после `chkdsk`.
4. #385 — реализовать применение checkpoint-плана, затем включать `allow_checksums=True`.
5. #387 — вынести матрицу в nightly и закрыть после двух чистых прогонов.
6. #384, #87 — закрывать только после #385, #386, #387, #389, #390, #530.

---
Собрано автоматически; ссылки на issue — в разделе 2. Все утверждения о наличии файлов проверены в клоне `origin/main` @ `96bda60`.
