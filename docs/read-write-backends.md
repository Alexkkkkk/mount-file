# Варианты расширения чтения и записи файловых систем

Этот документ фиксирует варианты развития backend-ов Mount File. Это архитектурное предложение, а не заявление о реализованной поддержке. Источником текущих гарантий остаются реестр возможностей и тесты профилей.

## Что уже есть

Mount File использует собственные bounded native readers для поддерживаемых профилей FAT12/16/32, exFAT, ext2, ext3/ext4 и NTFS. Поддержка ограничена конкретными признаками формата и условиями целостности; неизвестные, dirty, повреждённые и неоднозначные профили должны завершаться явным отказом.

Native write-профили существуют для FAT12/16/32, выбранного профиля exFAT и чистого allowlisted ext2. Их операции и ограничения различаются. Native write для ext3/ext4 и NTFS не поддерживается. Подробности: [`write_engine.md`](write_engine.md), [`ext3-ext4-readonly-plan.md`](ext3-ext4-readonly-plan.md), [`issue-182-status.md`](issue-182-status.md) и машинная матрица `mountfile.capabilities`.

## Рассмотренные варианты

### The Sleuth Kit — read-only fallback

TSK предоставляет C-библиотеку и API для открытия файловой системы в образе по смещению, перечисления файлов и чтения файловых диапазонов. Это подходящий optional backend для анализа и извлечения, но не backend записи. Подключать его следует только для явно включённых совместимых случаев. Распознанный профиль, отвергнутый native-парсером из-за повреждения, dirty-state или неизвестной feature, нельзя автоматически отдавать TSK: это скроет отказ и может поменять интерпретацию доказательства.

Источник: [TSK File System API](https://www.sleuthkit.org/sleuthkit/docs/api-docs/4.5/fspage.html).

### libext2fs — отдельный ext-family writer

`libext2fs` предоставляет низкоуровневые средства управления ext filesystem и RW-режим открытия. Это кандидат для будущего ext2/ext3/ext4 writer-а, если проект готов опираться на системную C-библиотеку и поддерживать отдельный native adapter. Наличие RW-флагов в API само по себе не обеспечивает crash consistency, корректный JBD2 replay/checkpoint, checksum validation, safe staging или эквивалентность системному драйверу.

Предложение: сначала исследовать API на disposable fixtures и прототипе для одного строго заданного чистого ext2-профиля. Не использовать `EXT2_FLAG_FORCE` как обход feature gates. Расширять до journaled ext3/ext4 только после полноценной модели журнала, проверок checksum/features и fault-injection на прерываниях.

Источник: [руководство библиотеки EXT2FS](https://android.googlesource.com/platform/external/e2fsprogs/+/master/doc/libext2fs.texinfo).

### libguestfs — изолированный compatibility backend

libguestfs даёт API для чтения и изменения файлов внутри дисковых образов, используя отдельную appliance-среду. Это наиболее общий кандидат для optional compatibility path, когда Mount File не реализует профиль самостоятельно. Для недоверенных образов такой путь предпочтительнее непосредственного монтирования на host kernel: libguestfs описывает защиту через виртуальную среду и отдельно предупреждает против прямого монтирования недоверенной файловой системы в host kernel.

Писатель обязан открывать образ с явно заданным форматом, работать только на остановленном/неиспользуемом образе и сериализовать внешних writers. Документация libguestfs предупреждает, что параллельное редактирование образа может привести к повреждению. Следовательно, backend должен требовать эксклюзивную блокировку и fail closed, если блокировка недоступна; locking не защищает от редакторов, игнорирующих advisory locks.

Минимальная схема интеграции: неизменяемый исходник → временная рабочая копия/overlay → операции внутри libguestfs → закрытие и flush с обработкой ошибок → независимая проверка и read-back → публикация результата только после успеха. Исходный evidence image не должен изменяться неявно. API-отказ или ошибка проверки должны сохранять рабочую копию для диагностики, а не тихо публиковать частичный результат.

Источники: [API libguestfs](https://libguestfs.org/guestfs.3.html), [безопасность libguestfs](https://libguestfs.org/guestfs-security.1.html), [предупреждение guestfish о конкурентной записи](https://libguestfs.org/guestfish.1.html).

### NTFS-3G — платформенный NTFS RW adapter

NTFS-3G предоставляет read/write NTFS driver и утилиты, однако это системная/платформенная зависимость с собственными требованиями к FUSE и mount privileges. Рассматривать его следует только как отдельный optional backend на поддерживаемых ОС, а не как доказательство native NTFS write parity. Не выполнять mount недоверенного образа в host kernel; если backend запускается через guestfs, интеграцию нужно тестировать отдельно.

Источник: [репозиторий NTFS-3G](https://github.com/tuxera/ntfs-3g).

## Рекомендуемое решение

Не заменять нынешние bounded native paths одной универсальной библиотекой. Сохранить capability-gated native readers/writers как предсказуемый путь, использовать TSK только для явно разрешённого read-only анализа, а libguestfs изучить как opt-in compatibility backend для редактирования файлов на копии образа. `libext2fs` оставить специализированным кандидатом для будущего ext-family backend, а NTFS-3G — платформенным NTFS вариантом, не обещая общей совместимости.

Все backend-и должны сообщать выбранный provider, режим и причину отказа через существующий capability/status contract. Не должно быть неявного read-write fallback после ошибки native backend-а. Пользователь должен явно выбрать write backend и подтвердить операцию; исходник следует хэшировать до и после обработки.

## Этапы внедрения

1. Согласовать provider interface для `probe`, `list`, `stat`, `read_range`, `write`, `close` и структурированного refusal status. Все writers по умолчанию выключены.
2. Добавить TSK read-only adapter за optional dependency. Проверить файловые диапазоны, partition offsets, unsupported features, malformed images и отсутствие write syscalls; native rejection для распознанных профилей не должен запускать fallback.
3. Создать libguestfs spike в отдельном optional extra. Начать только с копии raw image, явного raw format, exclusive lock, no network, bounded timeout/resource limits, staged output, read-back и независимого checker. Сравнить зависимости и поддержку Windows/macOS до выбора его как пользовательского backend-а.
4. Выполнить отдельный `libext2fs` spike на чистых ext2 образах и сверить образ до/после с e2fsprogs. Не включать ext3/ext4 write при journal/recovery-required состояниях.
5. Добавлять backend в production только при passing positive/negative real-image matrix, failure injection, process-crash recovery tests, CI на заявленных ОС, проверке лицензий/сборки пакетов и документации по точным profiles/operations.

## Критерии безопасности

- Не работать на единственном оригинале: каждая запись начинается с копии или проверенного staged image.
- Запретить одновременные writers; lock должен удерживаться от probe и открытия до финального закрытия и read-back.
- Перед публикацией результата проверять исходную identity/hash, размеры и ожидаемое смещение раздела.
- Проверять изменённую файловую систему независимым checker-ом, когда он доступен; checker не заменяет crash-recovery тесты.
- При незавершённой транзакции, dirty journal, неизвестной feature, checksum mismatch, out-of-bounds metadata или ошибке recovery — отказ без автоматического downgrade.
- Документировать, что атомарная публикация файла/overlay не равна power-loss atomicity самого файлового формата или всех накопителей.

Для journaled ext4 эти требования существенны: журнал защищает согласованность metadata посредством транзакций и commit records; просто менять блоки файловой системы недостаточно для безопасной записи. См. [документацию ядра Linux о журнале ext4/JBD2](https://docs.kernel.org/filesystems/ext4/journal.html).
