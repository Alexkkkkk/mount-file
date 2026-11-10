# MountFile readiness audit — 2026-11-10

Аудит выполнен по `origin/main` на commit `7ab919d` (`feat(layers): самообучающиеся AI-слои...`). Цель аудита — отделить реально работающие bounded-профили от заявлений, которые пока нельзя считать production-ready.

## Итог

Репозиторий находится в рабочем промежуточном состоянии, но не готов объявлять полную native read/write поддержку всех заявленных файловых систем. Это не дефект safety-контракта: ограничения в основном выставлены намеренно и fail-closed. Неготовыми остаются прежде всего ext3/ext4 и NTFS native writers, power-loss evidence для всех writable profiles, а также два открытых status PR.

На `main` уже присутствуют JBD2 superblock validation, checkpoint planner, committed-transaction decoder/reducer и durable checkpoint executor в составе squash-коммита PR #449. Отдельно восстанавливать исторические follow-up SHA не нужно: содержимое этих функций уже находится в актуальном `main`.

## Что реально работает

- Native bounded read/write profiles опубликованы для FAT12/16, FAT32, ext2 и exFAT.
- Native read-only profiles опубликованы для ext3, ext4 и NTFS.
- Transactional snapshots, hash-linked journals, disposable-copy checker verification и explicit refusal paths присутствуют.
- External checker bootstrap присутствует в `tools/system-packages-debian.txt`; CI на `main` читает этот manifest.
- JBD2 foundation присутствует, но это не ext3/ext4 writer: он валидирует и планирует checkpoint, не authorizes filesystem mutation.

## Blockers, подтверждённые на main

### 1. ext3/ext4 native write отсутствует — issue #385

Capability matrix не содержит writable ext3/ext4 profiles. `fs_preflight.py` прямо оставляет journaled ext profiles read-only, а `universal_fs_rw.py` отказывает debugfs bypass для ext3/ext4.

Решение должно идти поэтапно:

1. ограничить первый writable profile classic 32-bit/no-checksum JBD2;
2. закончить journal inode mapping, ring scan, descriptor/data/revoke/commit validation и sequence wrap;
3. связать reducer с metadata mutation для inode, bitmap, group descriptor и directory blocks;
4. добавить ext3 fixtures и ext4 classic/extent fixtures с валидными metadata checksums;
5. выполнять `data -> fsync -> clean journal marker -> fsync` только после independent `e2fsck -f -n` на disposable copy;
6. включать capability profile только после process-kill, malformed-journal и quarantine acceptance.

До выполнения всех шести шагов read-only refusal является правильным поведением и его нельзя снимать ради зелёного status.

### 2. Native NTFS writer отсутствует — issue #386

Репозиторий имеет bounded NTFS reader/probe и compatibility mount paths, но не native pure-Python writer. `ntfsfix` не является authoritative NTFS checker и не может заменить `$LogFile` replay.

Практическое решение:

1. начать с clean, uncompressed, non-sparse, non-resident-data bounded profile;
2. реализовать USA fixups, `$MFT` record allocation, `$Bitmap`, directory index updates и `$UpCase`-consistent name lookup;
3. добавить `$LogFile` restart-page parser и transaction/restart-area validation;
4. публиковать writes только при clean volume state и known supported LogFile profile;
5. использовать Windows/Linux reference mounting only as differential oracle, не как hidden fallback;
6. оставить NTFS read-only, пока independent checker и interrupted-commit recovery не пройдут.

### 3. Recovery acceptance не production-ready — issue #387

Canonical `recovery-matrix.json` сообщает:

- FAT12/16: rollback tested, process-kill not proven, power-loss unsupported, quarantine not proven;
- FAT32: rollback tested, process-kill not proven, power-loss unsupported, quarantine not proven;
- ext2: rollback and quarantine tested, process-kill tested with recovery sidecar, power-loss unsupported;
- exFAT: rollback tested, process-kill not proven, power-loss unsupported, quarantine not proven.

Нужно добавить отдельный subprocess harness, который убивает writer на каждой boundary: before data, after each data block, after first fsync, before marker, after marker and after final fsync. После каждого сценария harness обязан проверить byte/hash preservation, journal state, repeated recovery idempotence и quarantine marker. Power-loss нельзя объявлять proven симуляцией process kill: нужен отдельный documented durability test на supported filesystem/image runner.

### 4. Checker evidence неполна на разработческой машине — issue #389

На audit sandbox доступны `mke2fs` и `e2fsck`, но отсутствуют `fsck.fat`, `fsck.exfat`, `ntfsfix`, `pytsk3` и соответствующие mkfs utilities. Canonical checker matrix корректно маркирует эти entries как `unavailable`; это не означает pass.

Решение — запускать full acceptance только в Linux integration job после `tools/system-packages-debian.txt`, сохранять tool versions и JUnit/artifact evidence, а locally использовать `make tools` / `make tools-check`. Missing checker должен fail the acceptance job, но не должен превращаться в false product success.

### 5. Open status PRs блокируют clean documentation state

- [PR #467](https://github.com/Alexkkkkk/mount-file/pull/467) обновляет README status block и roadmap после release metadata changes.
- [PR #468](https://github.com/Alexkkkkk/mount-file/pull/468) делает stale generated README status failing gate вместо warning.

Чистое решение: сначала merge #467, затем rebase/merge #468 и убедиться, что `generate_readme.py`, generated JSON artifacts и `README.md` совпадают на одном commit. Не редактировать generated block вручную.

## Наблюдения по качеству

Полный локальный `ruff check .` сейчас сообщает около 1823 нарушений, но CI применяет более узкие correctness/format gates. Это следует разделить на два backlog-а: не менять поведение массовым autofix; сначала зафиксировать lint scope для runtime (`mountfile`, `tests`, `main.py`), а затем отдельными PR очищать `tools` и архивные scripts.

В текущем audit sandbox отсутствуют Linux checker packages, поэтому полная acceptance suite локально не может быть честно объявлена зелёной. Это environment limitation, а не доказательство поломки writer-ов.

## Приоритет реализации

1. Merge #467 и #468 и зафиксировать generated-status gate.
2. Закрыть #387 для уже существующих FAT12/16, FAT32 и exFAT writers. Это уменьшит production-risk без расширения filesystem scope.
3. Расширить checker/fixture CI по #389 и сохранить artifacts с версиями tools.
4. Завершить bounded ext3 writer по #385; capability remains read-only until acceptance evidence is complete.
5. Затем разрабатывать NTFS writer по #386; не использовать `ntfs-3g` или `ntfsfix` как скрытую native implementation.
6. После каждого профиля обновлять capability matrix, recovery matrix, README generated block и issue #182 progress sheet одним проверяемым commit.

## Решение по текущему состоянию

Позиция `main` сейчас корректна как conservative release candidate для поддержанных bounded profiles, но некорректна как заявление о полной filesystem read/write реализации. Главная ближайшая инженерная цель — не снять refusal codes, а закрыть recovery evidence для существующих writers и только затем публиковать новые ext3/ext4 и NTFS write profiles.

## Источники

- [Repository](https://github.com/Alexkkkkk/mount-file)
- [Issue #384 — native filesystem production-ready epic](https://github.com/Alexkkkkk/mount-file/issues/384)
- [Issue #385 — native ext3/ext4 writer](https://github.com/Alexkkkkk/mount-file/issues/385)
- [Issue #386 — native NTFS writer](https://github.com/Alexkkkkk/mount-file/issues/386)
- [Issue #387 — writer acceptance matrix](https://github.com/Alexkkkkk/mount-file/issues/387)
- [Issue #389 — cross-platform fixtures/checker CI](https://github.com/Alexkkkkk/mount-file/issues/389)
- [Issue #390 — benchmark/product status synchronization](https://github.com/Alexkkkkk/mount-file/issues/390)
- [PR #449 — bounded JBD2 checkpoint planning](https://github.com/Alexkkkkk/mount-file/pull/449)
