# Production readiness: issues #385–#390

## Что готово в этой ветке

- Общий `TransactionalWriter` теперь повторно проверяет SHA-256 образа после получения lock, после snapshot и перед публикацией. Внешняя замена или изменение исходного образа приводит к typed `REJECTED_PRECONDITION`, а staged-копия не публикуется.
- Ошибки получения lock и отсутствующий исходный образ возвращаются как receipt, а не теряются в исключении.
- Audit JSONL выдерживает короткие `os.write`; ошибка записи audit-журнала не маскирует уже завершённую публикацию образа.
- ext4 semantic read-back повторно открывает изменённый inode через libext2fs и сравнивает фактическое содержимое до atomic replace.
- ext4 profile validation отклоняет отрицательные и выходящие за пределы образа offsets.
- Для ext4 recovery используется только `e2fsck -fy` на disposable-копии; исходный образ не ремонтируется напрямую.
- NTFS `$LogFile` replay остаётся явно отключённым: dirty/unknown состояния fail closed, а `recover="copy"` сохраняет content-addressed quarantine-копию с JSON-маркером и затем отвергает запись через `NtfsReplayQuarantined`.

## Что намеренно не объявляется готовым

Этот патч не объявляет полной crash-consistent реализацией:

- собственный JBD2 transaction coordinator с descriptor/data/revoke/commit, checksum, sequence rollover, checkpoint и durable ordering;
- NTFS `$LogFile` redo/undo replay, обновление `$MFT`/`$MFTMirr`/bitmap/index и differential validation с Windows;
- power-loss durability или доказательство old-or-new состояния после физического отключения питания;
- создание, удаление, rename, resize и сложные NTFS/ext4 metadata операции без отдельных реальных fixtures.

До появления process-kill/failure-injection fixtures и независимого checker для каждой заявленной операции native mutation остаётся opt-in экспериментальным режимом. Черновики replay сохранены в документах как материалы для ревью и не подключаются к runtime.
