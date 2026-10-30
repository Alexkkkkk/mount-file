# Встроенные файловые системы MountFile: native VFS, ограниченные writers и критерии безопасности

## Цель

Постепенно развивать MountFile в сторону встроенных in-process readers и writers файловых систем. Для файловых образов пользователь должен иметь возможность просматривать, читать, извлекать и выполнять явно поддержанные операции без обязательного `pytsk3/libtsk`, внешних filesystem CLI и системного монтирования. Доступ к физическим устройствам по-прежнему определяется разрешениями операционной системы.

Работа выполняется только по проверяемым форматным профилям. Поддержка конкретного расширения, feature flag или типа операции должна быть явно заявлена, ограничена валидатором и покрыта тестами. Неизвестное, dirty, повреждённое, неполное или требующее recovery состояние означает отказ от операции; автоматический repair исходного образа запрещён.

## Обязательные принципы

1. **Fail closed.** Неизвестные features, повреждённые метаданные, неверные размеры и смещения, dirty/recovery-required состояние и неполный профиль не переводятся в writable режим. Причина отказа должна быть понятной и машинно различимой там, где API поддерживает refusal codes.
2. **Границы образа и раздела.** Все вычисления offset, size, sector, block, cluster, run и extent используют проверенные bounded arithmetic. Ни один read/write не выходит за границы образа или выбранного filesystem extent. Partition offsets берутся из общего проверенного слоя partition parsing.
3. **Никакой скрытой подмены backend.** Для распознанной, но отвергнутой файловой системы запрещено незаметно переключаться на `pytsk3`, FAT parser или другой backend. Legacy fallback допустим только для явно разрешённого случая и отображается вызывающему коду.
4. **Native write — только для заявленного профиля.** Поддержка операций и ограничений показывается в backend status, GUI, CLI и документации. Наличие общего интерфейса writer не является обещанием полного формата.
5. **Безопасное изменение образа.** До commit проверяются исходная идентичность образа и отсутствие конкурентного изменения. Изменяемая область ограничивается staging; выполняются read-back и структурная проверка. Ошибка rollback или неопределённое состояние переводит образ в read-only/quarantined режим и сообщает о необходимости восстановиться из backup.
6. **Не подменять on-volume recovery host snapshot-ом.** Snapshot/backup на стороне MountFile не заменяет корректный журнал файловой системы. Writers для журналируемых профилей запрещены, пока не реализованы требуемые transaction, ordering и recovery semantics.
7. **OS mount — отдельная функция.** In-app VFS не зависит от FUSE, WinFsp, macFUSE, ImDisk, `udisksctl`, `hdiutil` или `Mount-DiskImage`. OS mount adapters остаются отдельной необязательной интеграцией.

## Объём и операции

### Read-only API

Единый VFS/backend API должен предоставлять `list`, `stat`, `read`, `read-range` и `extract` с точным filesystem extent. Неизвестный или неподдерживаемый профиль возвращает ясный отказ. При необходимости допускаются дополнительные read-only backends, но их статус и fallback policy должны быть явными.

Форматы для постепенного native-read покрытия: FAT12/16/32, exFAT, ext2/3/4 и NTFS. Поддержка каждого формата ограничивается конкретной allowlist профиля и не подразумевает поддержку всех возможностей стандарта.

### Write API

Для выбранного профиля операции `put` (создание/замена), `mkdir`, `mv`/rename и `rm`/delete включаются по отдельности. Не включённые операции обязаны явно отклоняться до первой записи. В частности, ext3/ext4 и NTFS не получают native writer только на основании успешного чтения.

## Текущая база и опубликованный статус

### `main`

Текущая базовая ветка репозитория включает уже опубликованные native filesystem изменения и последующие исправления. В частности, в дереве есть ограниченные native readers ext/FAT/NTFS, bounded exFAT компоненты, FAT32 writer и ограниченный native ext2 writer. Последние изменения `main` включают проверку регистра имён exFAT по `$UpCase` и differential test native FAT reader против `pytsk3` на реальном образе.

Фактический уровень поддержки остаётся частичным:

- **ext2:** ограниченный clean, non-journaled writer с allowlist features, direct и single-indirect блоками файлов, staging и read-back. Рост каталога через indirect blocks, persistent power-loss recovery и общая crash-recovery acceptance не закрыты.
- **FAT:** native bounded readers реализованы для FAT12/16/32; отдельные write-возможности существуют для FAT32. Полная differential и write parity матрица для всех FAT вариантов не заявляется.
- **exFAT:** native reader/writer возможности ограничены поддержанными профилями и проверяемыми метаданными. Не поддерживаемые layout/операции остаются fail-closed; неизвестные структуры не должны трактоваться как поддержанные.
- **ext3/JBD2:** доступно ограниченное read-only профилирование/валидация. Журналирование и recovery требуют отдельной проверки; replay не является разрешением на запись.
- **ext4:** доступно ограниченное чтение конкретных clean профилей. Extents, feature gates и checksum/recovery ограничения применяются консервативно; native ext4 writer не заявляется.
- **NTFS:** имеются bounded metadata/read-only компоненты, но принятие профиля зависит от полной проверки затрагиваемых структур и состояния тома. Native NTFS writer и log replay не заявляются.
- **Benchmarking:** наличие тестов производительности или differential tests не заменяет опубликованный воспроизводимый benchmark report по методике ниже.

### Незлитая follow-up работа

Ветка `work/issue-182-follow-up` содержит дополнительные hardening изменения ext3/NTFS readers и regression tests. Эти коммиты не являются частью `main`, пока не будут отдельно проверены и включены через обычный review/merge процесс. Любые упоминания этой ветки в обновлениях должны сопровождаться её SHA и не смешивать статус ветки со статусом релиза.

## План работ и критерии приёмки

### Этап A — общие safety gates и ext2 writer

- Сохранить явный allowlist чистого ext2 профиля и отказывать для ext3/ext4 профилей, если соответствующая логика журналирования/структур не реализована.
- Проверять inode flags и тип/состояние изменяемых каталогов и файлов до мутации; неподдержанные flags не должны обходиться через parent path.
- Добавить рост каталогов через indirect blocks только отдельным профилем с allocation/counter/read-back тестами.
- Спроектировать durable transaction/recovery протокол для падения процесса и питания. Advisory locks не считаются crash recovery и не защищают от неучаствующих процессов.
- Проверить fsync/rename порядок, идентичность и права sidecar backup, очистку временных файлов, отсутствующий или повреждённый backup.
- Выполнить failure-injection для short write, исключения, отмены, concurrent modification, завершения процесса и каждого commit/rollback boundary.
- После восстановления тестировать временную копию независимым `e2fsck -fn`, если инструмент доступен; never repair оригинал.

**Приёмка:** каждое поддерживаемое изменение ограничено выбранным extent, подтверждено read-back, counters и структурной проверкой; соседние байты partition/image неизменны. Если восстановление нельзя однозначно подтвердить, дальнейшие записи блокируются.

### Этап B — VFS и fallback policy

- Использовать существующие `mountfile/vfs.py`, backend API и общие partition bounds; не создавать параллельный VFS.
- Подключать native providers через единый `list/stat/read/read-range/extract` контракт с корректным `close`, повторным открытием и очисткой после неудачного reopen.
- Для всех известных signatures проверить VFS, CLI, GUI, GUI image search и MCP маршруты. Dirty/unsupported распознанный профиль не должен попадать в другой parser fallback.
- Сделать `pytsk3` необязательной install/runtime dependency там, где native/explicit fallback policy это позволяет. Compatibility fallback должен быть явно обозначен и тестируем.
- Согласовать FAT12/16/32 с единым VFS контрактом и расширять differential fixtures по реальным и synthetic образам.

**Приёмка:** таблица маршрутизации/профилей проверена тестами на allowed-native, allowed-fallback и reject-before-fallback случаях; пользователю видны backend и причина отказа.

### Этап C — exFAT read-only и write profile

- Валидировать main и backup boot regions, их checksum, согласованность критической геометрии и допускаемые различия VolumeFlags/PercentInUse.
- Реализовать и проверять root/directory entry sets, checksum, строгий порядок записей и допустимые комбинации metadata entries.
- Валидировать FAT и её границы, active FAT/NumberOfFats semantics, allocation bitmap и `$UpCase` checksum/content.
- Реализовать bounded cluster-chain resolver с cycle, length, allocation и extent checks; обеспечить `list/stat/read-range/extract`.
- Оставлять multi-FAT и неизвестные metadata layouts закрытыми до явной реализации.
- Writers включать только по отдельным операциям с staging, проверкой bitmap/FAT/directory updates, read-back и независимой consistency проверкой.

**Приёмка:** valid/invalid synthetic и реальные fixtures, проверки offset/volume bounds, no-fallback тесты и operation matrix для каждой заявленной мутации. Неподдержанные операции возвращают отказ без изменения образа.

### Этап D — ext3/JBD2 и ext4

- Для JBD2 проверять superblock, UUID/features, ring geometry, sequence semantics, transaction descriptor/data/commit/revoke и неполную транзакцию. Неподдержанные checksum/revoke/layout варианты fail closed.
- Добавить clean/dirty/incomplete/corrupt journal fixtures и real-image oracle. Read-only replay, если включён, обязан работать только как bounded in-memory overlay и явно маркироваться; он не записывает журнал или filesystem обратно.
- Для ext4 проверять полный физический диапазон каждого extent, logical extent coverage относительно inode size, preallocation/unwritten policy, дерево extents, циклы, index/leaf bounds и allocation/metadata overlap.
- Поддержку 64-bit/group descriptor/layout расширять только с отдельными тестами descriptor size, high words и bounds.
- Реализовать каждый необходимый checksum validator для заявленного read/write профиля. Superblock checksum один не разрешает читать unchecked metadata_csum структуры.
- Не включать ext3/ext4 writers до доказанной реализации on-volume journaling/recovery, ordering, crash handling и независимой проверки.

**Приёмка:** профили чётко разделены; неизвестные/dirty/recovery-required состояния отклоняются до browsing/mutation там, где данные нельзя безопасно интерпретировать. Ни один ext4 extent не читается за пределами блока, размера inode, файловой системы или образа.

### Этап E — NTFS read-only

- Валидировать BPB/геометрию, полный volume extent, MFT/MFTMirr records, USA/fixups, record sequence numbers и ссылки на записи.
- Проверять resident/nonresident attributes, attribute lists и extension records по ограниченному профилю; проверять runlist arithmetic, VCN coverage, LCN bounds и размеры потоков.
- Проверять `$Volume` dirty state и `$LogFile` restart/checkpoint/recovery eligibility прежде чем разрешить native browsing.
- Реализовать bounded directory index traversal и collation policy, включая `$I30`, `INDX` VCN/USA/header bounds, cycles и duplicate-name handling.
- Добавить clean/dirty/corrupt fixtures, real-image/differential oracle и no-fallback tests.
- Оставить log replay и native writes выключенными до полноценной `$LogFile`-aware transaction/recovery реализации.

**Будущий ограниченный writer профайл, не входящий в текущую приёмку:** только overwrite уже выделенного nonresident, non-sparse, non-compressed, non-encrypted stream без изменения размера, и только после `$LogFile` transactional/recovery path. Create/resize/rename/delete запрещены до атомарной поддержки MFT, bitmap, indexes и связанных attributes.

### Этап F — GUI/CLI, статусы и документация

- Переводить GUI/CLI на единый VFS постепенно; удалять broad-exception parser fallbacks.
- Показывать фактический provider, режим read/write, операции, ограничения и причины отказа в API, backend status, CLI и GUI.
- Синхронизировать документацию с реальным capability matrix; формулировки не должны обещать полную поддержку формата при ограниченном профиле.
- FUSE/WinFsp/macFUSE описывать и тестировать отдельно как optional OS integration. Отсутствие driver не должно блокировать in-app VFS.

**Приёмка:** пользовательский интерфейс не предлагает недоступную мутацию как успешную операцию и не скрывает смену backend.

### Этап G — воспроизводимые benchmark и differential reports

- Опубликовать benchmark методику для `list`, `stat`, `read-range` и `extract`; write workload добавлять позднее и только на временных копиях.
- Для каждого набора фиксировать размер образа, filesystem и block size, partition offset/size, набор файлов/размеры, cache state, backend и runtime версии, число повторов и способ агрегирования.
- Сравнивать native и `pytsk3` на одинаковых образах и путях, отделяя parser time от I/O/cache effects.
- Проверить ориентир не более 20% регрессии на заранее определённом workload. Если ориентир недостижим — опубликовать узкие места и измерения; безопасность не ослаблять ради скорости.

**Приёмка:** отчёт воспроизводим по указанным входам и командам; результаты не распространяются за пределы протестированных workload.

## Общие требования к тестированию

- Synthetic fixtures для граничных размеров, malformed metadata, неизвестных features, partition offsets, разделов рядом и отказов backend.
- Regression fixtures и реальные образы там, где доступны безопасные инструменты-оракулы.
- Differential проверки не делают optional tooling обязательной runtime зависимостью.
- Матрица Linux, macOS и Windows для supported Python versions. Недоступный optional tool может давать skip, но это не считается успешной проверкой функционала, требующего такого oracle.
- Failure injection для каждого изменяющего путь этапа; тестировать повторное открытие после успешной и неуспешной операции.
- После каждой успешной мутации перепроверять изменённые данные read-back и независимым filesystem checker на копии, где он существует.
- Проверять image hash/identity, соседние partition sentinels, counters, bounded I/O, rollback и сообщение об отказе.

## Итоговые критерии закрытия issue

Issue можно закрыть только после выполнения и документирования всех следующих пунктов либо после явного исключения соответствующего формата/операции из проекта с удалением заявлений о поддержке:

1. Для каждого заявленного формата опубликована матрица поддержанных профилей и операций с явными отказами для остальных.
2. Native read API работает без обязательного `pytsk3` на валидных bounded fixtures; fallback policy тестируется и прозрачно маркируется.
3. Каждая заявленная write operation выполняется native backend без mount/CLI subprocess и имеет read-back, independent checker, bounds, rollback и failure-injection coverage.
4. Dirty, recovery-required, malformed и unknown-feature профили не модифицируются и не маршрутизируются в неподходящий fallback.
5. Cross-platform test status, включая пропуски из-за optional tooling, опубликован и соответствует заявленному объёму.
6. Документация и интерфейсы показывают фактическую стратегию, ограничения и отказные причины.
7. Воспроизводимая benchmark методика и результаты опубликованы; performance target либо достигнут, либо описаны измеренные отклонения.
8. Ограничения crash recovery и OS mount отражены явно; host snapshot не выдаётся за filesystem journal recovery.

Каждый этап можно выпускать отдельно. Незавершённые этапы не мешают закрыть конкретный PR, но не позволяют объявить всю umbrella issue завершённой.

## Безопасность и исключения

Никакой этап этой задачи не должен запускать repair на исходном образе. Независимые checker-ы используются только на временных копиях и в non-repair режиме. При неопределённом rollback, обнаружении конкурентного изменения или невозможности подтвердить результат writer обязан отказаться от новых writes и оставить диагностируемое состояние восстановления. Любая новая поддержка формата начинается с read-only validator и тестов отказа; writer появляется только после доказанной целостности транзакций.
