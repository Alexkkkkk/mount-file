# Аудит готовности issue #182

**Дата аудита:** 2026-11-03  
**Проверенный ref:** `origin/main`  
**Проверенный commit:** `82f86aa`  
**Issue:** [#182 — Встроенные pure-Python файловые системы: чтение, запись и VFS](https://github.com/Alexkkkkk/mount-file/issues/182)

## Итог

Issue #182 **не готова к закрытию как full filesystem parity / production-ready**. В `main` есть существенная bounded native-инфраструктура: ограниченные readers для FAT12/16/32, exFAT, ext2/ext3/ext4 и NTFS; общий VFS routing; typed refusal/safety primitives; ограниченные writers для FAT и ext2; transactional one-FAT exFAT writer; capability matrix и тесты отказов.

Это не равно полной реализации файловых систем. В текущем `main` отсутствуют native writers для ext3, ext4 и NTFS. Для journaled profiles нет доказанной полной комбинации ordering, checkpoint, replay, revoke/checksum semantics, process-kill/power-loss recovery и independent checker acceptance. Поэтому публичный статус должен оставаться **частично готово / bounded profiles only**, а не «все заявленные файловые системы поддержаны».

## Что подтверждено в текущем `main`

### Native readers и fail-closed routing

Репозиторий содержит отдельные bounded readers/probes для FAT, exFAT, ext и NTFS. В коде присутствуют проверки размеров, смещений, geometry, cluster/block/run bounds, malformed metadata и отказов для неподдержанных feature flags. В VFS есть тесты маршрутизации и отказа до legacy fallback для распознанных, но неподдержанных профилей.

Текущая реализация остаётся профильной: она поддерживает только явно проверенные layouts и операции. Нельзя трактовать наличие класса reader или успешное распознавание signature как поддержку всего формата. Это соответствует исходным требованиям issue: неизвестное, dirty, corrupt или recovery-required состояние должно завершаться отказом без скрытой подмены backend. [1] [2]

### Ограниченные writers

В `main` присутствуют ограниченные write-профили для FAT12/16, FAT32, чистого ext2 и one-FAT exFAT. Для ext2 есть bounded directory growth и durable sidecar recovery flow; для exFAT есть staging, identity/concurrent-change checks, `fsync`, read-back и ограниченная metadata consistency validation.

Эти writers не дают полной parity. Операции, layouts, большие размеры, directory indexing, multi-FAT/TexFAT, отдельные inode flags и другие features остаются ограниченными или явно отвергаются. В частности, exFAT two-FAT/TexFAT profile не должен считаться включённым только потому, что формат распознан. [2] [3]

### Capability и refusal status

Есть machine-readable capability matrix и typed refusal vocabulary, а также VFS/API тесты, которые проверяют часть статусов. Однако единая матрица `filesystem × profile × feature × state × operation` ещё не доведена до уровня, при котором GUI, CLI, VFS, API и release documentation гарантированно показывают одинаковое состояние каждого профиля.

## Что не готово

### 1. Полная native write parity

Не готово:

- ext3 native writer;
- ext4 native writer;
- NTFS native writer;
- полный набор операций для каждого заявленного профиля: create, replace, mkdir, rename внутри и между каталогами, delete, links, timestamps, attributes, sparse/large-file boundaries и rollback;
- полная поддержка Unicode/LFN/case-folding и collision handling во всех профилях;
- production-ready capability declaration для операций, которые сейчас существуют только в proposal или в ограниченном экспериментальном коде.

`mountfile/jbd2_writer.py` и read-only JBD2/ext readers не являются доказательством готового ext3/ext4 writer. Наличие serializer/planner или in-memory replay не заменяет on-volume journal transaction, checkpoint и recovery semantics.

### 2. Journal и crash/power-loss acceptance

Не готово доказательство, что после остановки процесса или имитации power loss каждая фаза каждой native write-транзакции приводит к одному из двух безопасных результатов: валидному восстановленному образу или явной quarantine/read-only блокировке.

Ext2 имеет существенный sidecar recovery механизм и отдельные failure tests. Но общей acceptance-матрицы для FAT, exFAT, ext2, ext3/ext4 и NTFS нет. Также нет доказательства полноценной on-volume JBD2 recovery parity для ext3/ext4 writers. Host backup/staging не может считаться заменой файловому журналу.

### 3. Независимые filesystem checkers

CI предусматривает установку checker-инструментов на Linux, но локальный аудит выполнялся в окружении без части этих инструментов. Полная матрица `writer operation × real image × independent checker × refusal case` не подтверждена для всех профилей.

Требуется отдельно доказать на копиях образов, что `e2fsck -fn`, `fsck.fat`, `fsck.exfat` и `ntfsfix` либо эквивалентные read-only oracles дают ожидаемый результат, а исходный образ не изменяется при отказе. Repair-инструменты нельзя запускать на оригинале.

### 4. Cross-platform acceptance

В `.github/workflows/ci.yml` заявлена матрица Linux/Windows/macOS и Python 3.10–3.12. Это полезная конфигурация CI, но сам workflow не является доказательством зелёного прохождения каждого profile/operation/checker на каждой ОС.

Особенно не закрыты cross-platform доказательства для file locking, durable rename/fsync semantics, temporary/staging files, rollback, permissions и optional filesystem utilities. Для Linux есть более полный набор system-oracle шагов; Windows и macOS не имеют эквивалентной полной real-image checker matrix.

### 5. Benchmark coverage

Есть benchmark-инфраструктура и опубликованные ограниченные результаты для отдельных workload-ов. Но нет воспроизводимой полной матрицы list/stat/range-read/extract/write для всех заявленных файловых систем на Linux, Windows и macOS с одинаковыми fixture parameters, cache assumptions и trusted baseline.

Поэтому нельзя заявлять универсальное performance parity или правило «не более 20% regression» для всего issue. Допустимо говорить только о конкретных опубликованных workload-ах и средах.

### 6. Static quality gate

В локальной проверке `ruff check mountfile tests` завершился с ошибкой: найдено `1380` замечаний, из них `590` отмечены как автоматически исправимые обычным `--fix`. Это не отменяет успешный pytest, но означает, что общий lint-quality gate не закрыт в текущем состоянии.

Следует разделить существующий legacy lint backlog и новые safety-critical нарушения, затем включить обязательный scoped lint для native filesystem modules и transaction/recovery code. Нельзя объявлять issue production-ready при silently tolerated lint failures в критических путях.

## Фактические проверки аудита

В этом окружении выполнены следующие проверки:

- полный `python -m pytest -q`: **1800 passed, 18 skipped** за **308.16 секунды**;
- targeted acceptance suite (`issue_182_acceptance`, VFS refusal routing, native writer failure matrix, independent checkers): **42 passed, 3 skipped** за **3.33 секунды**;
- локальные пропуски полного набора относятся к отсутствующим optional dependencies/tools: `pytsk3`, `dosfstools/mtools`, `exfatprogs`, `ntfs-3g/ntfsfix`, `yara-python` и PowerShell;
- `ruff check mountfile tests`: **failed, 1380 findings**;
- рабочее дерево перед созданием этого отчёта было чистым; отчёт создан отдельно от proposal-кода.

`pytest` подтверждает отсутствие регрессий в доступном Linux окружении, но не доказывает пропущенные cross-platform, external-checker и power-loss критерии. Skipped tests нельзя считать passed.

## Статус по acceptance gates issue #182

| Gate | Статус | Причина |
|---|---|---|
| Bounded native reads | Частично готово | Есть принятые profiles, но не полная feature/layout parity. |
| Native writes для всех заявленных семейств | Не готово | ext3/ext4/NTFS writers отсутствуют; остальные writers ограничены профилями. |
| Fail-closed и no hidden fallback | Частично готово | Есть routing/refusal tests, но нужна полная profile/state/operation matrix. |
| Read-back и independent checker | Частично готово | Есть профильные проверки; нет полного real-image writer matrix. |
| Durable transaction/recovery | Частично готово | Наиболее развито для ext2; общей power-loss гарантии нет. |
| Capability/status во всех интерфейсах | Частично готово | Matrix есть, полная синхронизация GUI/CLI/VFS/release не доказана. |
| Linux/Windows/macOS acceptance | Частично готово | Workflow есть; полное прохождение всех внешних oracle cases не подтверждено. |
| Reproducible benchmark matrix | Не готово | Результаты ограничены отдельными workload/environment. |
| Production-ready full parity | Не готово | Несколько обязательных gates выше остаются открытыми. |

## Рекомендуемый порядок закрытия

1. Зафиксировать и публично сохранить bounded capability matrix как единственный источник заявленного статуса. Не объявлять ext3/ext4/NTFS writable.
2. Закрыть scoped lint и добавить обязательные safety-critical quality checks для native readers/writers, routing и recovery modules.
3. Довести real-image checker matrix для уже существующих FAT/ext2/exFAT writers на копиях образов, включая отказные и failure-injection cases.
4. Добавить cross-platform evidence artifacts для Linux/Windows/macOS, а не только workflow configuration.
5. Отдельным milestone реализовать ext3 writer только после доказанного JBD2 ordering/checkpoint/replay/revoke/checksum и crash acceptance.
6. Затем отдельно реализовать ext4 extent/checksum/htree profiles с явными feature gates.
7. NTFS writer начинать отдельным проектом после `$LogFile` redo/undo, MFT/MFTMirr, bitmap, directory B-tree и replay design.
8. После каждого milestone публиковать code, fixtures, negative matrix, checker report и benchmark report в одном reviewable PR.

## Решение по issue #182

Issue #182 следует оставить открытой. Текущий результат лучше описывать как **bounded native filesystem profiles with limited safe writes**, а не как полную pure-Python поддержку всех заявленных файловых систем.

Следующий merge должен закрывать конкретный acceptance gate и содержать проверяемые fixtures. Proposal-документы и snippets, которые ссылаются на отсутствующие в `main` модули, не должны считаться реализованными функциями до адаптации, тестов и прохождения CI.

## References

[1]: https://github.com/Alexkkkkk/mount-file/issues/182 "MountFile issue 182"
[2]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/docs/issue-182-status.md "Issue 182 status at audited main"
[3]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/mountfile/ext2_native.py "Bounded native ext2 writer"
[4]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/mountfile/exfat_writer.py "Bounded native exFAT writer"
[5]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/mountfile/ext3_reader.py "Conservative ext3 reader"
[6]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/mountfile/ntfs_reader.py "Bounded NTFS reader"
[7]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/.github/workflows/ci.yml "Cross-platform CI workflow"
[8]: https://github.com/Alexkkkkk/mount-file/blob/82f86aa/pyproject.toml "Project dependencies and optional extras"
