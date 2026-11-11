# План безопасной реализации issues #385–#390

Проверено по `main` на коммите `ec7b4c61c3fc0f6e7845dec11215416161323e3d` от 10 ноября 2026 года. Этот документ задаёт техническое решение и критерии готовности; он сам по себе не включает запись в файловые системы и не заменяет приёмочные тесты.

## Главный вывод

Задачи #385 и #386 нельзя безопасно закрыть простым включением уже существующих writer-профилей. Для ext3/ext4 нет интегрированного JBD2 transaction/replay/checkpoint протокола; для NTFS есть ограниченная проверка restart area `$LogFile`, но нет полноценного разбора и проигрывания redo/undo-записей. Включать запись до восстановления после аварийного завершения означает рисковать повреждением образа. Поэтому ext3/ext4 и NTFS должны оставаться read-only, пока не пройдут отдельные gates из #387–#390. Это соответствует текущей capability matrix и существующему отказу для неподдерживаемых профилей. [1] [2] [7] [8]

При этом у проекта уже есть значимая основа: ограниченные native readers, профильная capability matrix, typed refusal коды, тесты отказа без legacy fallback, checker/recovery acceptance runners и read-only VFS benchmark. Задачи #387–#390 в основном требуют довести эти разрозненные механизмы до сквозных и воспроизводимых доказательств, а не переписывать их с нуля. [3] [4] [5] [6] [7]

### Что уже попало в main

- **#385:** PR #447 добавил строгую non-mutating проверку JBD2 superblock state и использование independent checker на disposable copy. Полный transaction writer, checkpoint и process-kill recovery ещё не готовы; issue прямо требует держать ext3/ext4 read-only. [24]
- **#386:** PR #474 усилил read-only `$LogFile` restart checks. Это валидация, не NTFS writer/replay. [25]
- **#387:** PR #407 добавил machine-readable recovery matrix, PR #417 — executable recovery acceptance и Linux artifacts. Остались непокрытые profile/failure-phase cells; recovery contract сам помечает process-kill/quarantine для FAT12/16, FAT32 и exFAT как `not_proven`, а power-loss — как `unsupported`. [26] [27] [8]
- **#388:** PR #399 добавил `stat`, PR #400 — bounded RW `read_range` (до 1 MiB). Требование полного вызова каждой положительной/отрицательной operation cell и сохранения refusal/fallback parity across interfaces всё ещё требует отдельного conformance прохода. [28] [29] [30]
- **#389:** PR #397/#404/#416 добавили bounded tool inventory, декларативную/исполняемую checker acceptance; PR #441 расширил cross-platform native-read evidence. Это не означает, что каждый независимый filesystem checker исполняется на каждой OS, или что все image/profile/operation cells уже имеют fixtures. [31] [32] [33] [34]
- **#390:** PR #402/#409/#419 добавили capability/evidence поля, status contract и benchmark artifact validator; PR #468 заставляет `status-sync` падать при устаревшем generated README status. Эта проверка находит и в данном PR реальный drift: README показывал 1.86.0 при package version 1.86.1; файл перегенерирован штатным `scripts/generate_readme.py`. Cross-platform benchmark provenance и write benchmark matrix остаются открытыми; текущий benchmark read-only и Linux-specific. [35] [36] [37] [38] [39] [40]

## #385 — ext3/ext4 writer с JBD2

### Что не готово

`jbd2_writer.py` умеет работать с ограниченными записями журнала, а `jbd2_checkpoint.py` содержит отдельные checkpoint helpers, однако они не объединены в полный путь файловой мутации, commit, replay и recovery. ext3/ext4 остаются read-only; проверка `e2fsck` на временной копии не доказывает crash safety писателя. [1] [9] [10]

### Решение

Сохранить fail-closed read-only режим и построить отдельный transaction coordinator. До первой записи он должен проверить superblock, геометрию, UUID, состояние тома, журнал и конкретный allowlist layout. Затем заранее составить ограниченный план всех изменяемых блоков; записать валидные JBD2 descriptor/data/revoke/commit records с корректными sequence и durability barriers; восстановление должно быть однозначным и идемпотентным. Checkpoint домашних metadata-блоков и очистка journal/recovery state допускаются только после подтверждённой устойчивой записи предыдущего этапа. Ошибка записи, синхронизации, неизвестное feature или двусмысленное восстановление переводят образ в quarantine/read-only, а не запускают альтернативный writer. [9] [10]

Безопасный MVP — отдельное решение владельца проекта, которое надо утвердить до кода. Рекомендуемое ограничение первой фазы: только чистый ext3 с внутренним JBD2 и явно allowlisted feature/layout profile; одна операция над линейным каталогом без освобождения и повторного использования блоков. ext4, extents, metadata checksums, external journal, неизвестные journal features, удаления и замены файлов остаются запрещены до своих валидаторов и фикстур. Это только этапирование риска, а не закрытие issue #385: исходный acceptance всё равно требует реализации/проверки features, нужных выбранному профилю, и полного recovery gate. Если операция не укладывается в строго поддержанный формат, её следует отвергнуть до первого изменения образа. [1] [24]

### Критерии включения записи

Для положительных фикстур каждая разрешённая операция должна пройти native read-back и независимый `e2fsck -f -n` на disposable-копии. Отрицательные тесты должны покрыть повреждённый superblock/journal, sequence mismatch и rollover, неверные bounds, неподдержанные checksum/revoke/features и dirty state. Fault injection и завершение отдельного процесса должны проверять каждую границу записи/синхронизации; после перезапуска образ должен быть целиком старым либо целиком committed новым. Проверки никогда не запускаются с режимом ремонта на исходном образе. Отсутствующий checker означает отказ в commit authorization, а не pass. [1] [3] [9] [10]

## #386 — NTFS writer и `$LogFile`

### Что не готово

Текущий NTFS reader проверяет ограниченную restart area `$LogFile` и состояние тома для чтения. Он не реализует полный parser страниц журнала, transaction model, redo/undo, compensation records, checkpoint и безопасное восстановление. Microsoft описывает журнал на высоком уровне как механизм восстановления, но этого описания недостаточно для реализации низкоуровневого on-disk replay; сам issue перечисляет широкий набор требуемых NTFS metadata/transaction операций. [2] [11] [12] [25]

### Решение

Оставить native NTFS mutation выключенной. Сначала закончить read-only разбор и модель replay на Windows-созданных fixtures для точно перечисленных версий и состояний NTFS. Parser обязан ограничивать каждую страницу и запись выбранным extent тома, проверять restart pages/USA/LSN, bounds и поддерживаемые record types; неизвестная версия, операция или повреждение — typed refusal до записи. Затем реализовать writer/recovery для очень узкого набора операций, только на disposable-копиях, с Windows reference-проверками. `ntfsfix` может быть дополнительным диагностическим сигналом, но не заменяет Windows checker и не доказывает корректное восстановление. [2] [11] [12] [25]

Даже замена содержимого существующего файла не должна включаться до доказательства поведения payload при аварии: `$LogFile` recovery нельзя автоматически считать атомарностью данных пользовательского потока. Создание, resize, rename, delete, каталожные индексы, `$MFT`/`$MFTMirr`, bitmap, ADS, reparse points, sparse/compressed/encrypted streams и прочие изменения — отдельные расширения scope с отдельными фикстурами.

### Критерии включения записи

Нужны corpus clean/dirty/interrupted образов от нескольких Windows/NTFS версий, differential comparison с Windows, parser fuzz/property tests и subprocess kill после каждого write/flush boundary. Recovery должна быть идемпотентной; при неоднозначности должна включаться quarantine и запрещаться повторная запись. Каждый отказ до первого write сохраняет исходный SHA-256; после начавшейся транзакции тест проверяет согласованное исходное либо committed состояние и неповреждение соседних областей. Исходные образы не монтируются и не ремонтируются. [2] [3] [11] [12]

## #387 — acceptance: failure injection, process kill, quarantine

### Что уже есть и чего не хватает

В `recovery_acceptance.py` перечислены реальные fixtures для FAT12/16, FAT32, ext2 и exFAT, но это ограниченный набор тестов, а не полная таблица операций и точек отказа. ext2 уже имеет более развитые process-kill/recovery проверки. Для FAT12/16, FAT32 и exFAT capability contract всё ещё указывает `process_kill`/`quarantine` как `not_proven`; для всех writer-профилей `power_loss` остаётся `unsupported`. Process-kill нельзя выдавать за доказательство внезапного отключения питания. [3] [7] [14]

### Решение

Сформировать тестовую матрицу `профиль × операция × фаза × исход`: подготовка, запись данных, обновление allocation metadata, публикация каталога/inode, flush/sync, commit, cleanup, recovery. Добавить детерминированный test-only fault seam для short write, I/O exception, cancellation и kill отдельного процесса на каждой изменяющей фазе. После рестарта проверять hash выбранного extent, sentinels вне него, внешние конкурентные изменения, marker/sidecar, точную quarantine-политику, read-back и повторяемость recovery. Checker запускается только на свежей копии в режиме no-repair.

Runner должен выдавать отдельный результат для каждой обязательной ячейки. Отсутствующий тест, skip, недоступная обязательная утилита, невалидный JUnit или повреждённый recovery artifact — blocker, не pass. В отчёте следует явно оставлять `power_loss=unsupported`, пока нет отдельного контролируемого испытания долговечности на описанной конфигурации хранения. [3] [14] [15]

## #388 — единый VFS/API/refusal контракт

### Что уже есть и чего не хватает

PR #399 и #400 уже добавили RW `stat` в capabilities и bounded `read_range` с лимитом 1 MiB. В main есть schema v6, canonical operation matrix и отдельные тесты no-fallback для распознанных повреждённых/неподдерживаемых профилей. Остаток — сделать полный сквозной conformance suite для каждой пары профиль × операция × интерфейс, а также доказать, что runtime вызов соответствует статусу, а typed refusal одинаково проходит через VFS, RW, CLI/API и GUI. Наличие `callable`-метода само по себе не доказывает, что операция работает на подходящей фикстуре. [4] [7] [16]

### Решение

Оставить `mountfile.capabilities` единственным источником canonical operation cells. Написать data-driven suite, которая для всех профилей проверяет опубликованный статус и реально вызывает доступные операции на подходящей временной фикстуре. Доступная операция должна успешно закончить read-back/content check; отказанная — возвращать typed `operation_not_supported`, а malformed/dirty/out-of-bounds случаи — соответствующий стабильный refusal code/category. Для распознанных неподдерживаемых и повреждённых образов тестировать, что никакой pytsk3/legacy fallback не был вызван, даже если compatibility fallback включён. Истинно нераспознанный whole-image допускает legacy путь только при явном opt-in. Любая mutation-тестовая фикстура должна быть disposable-копией.

## #389 — cross-platform fixtures и independent checkers

### Что уже есть и чего не хватает

CI уже выполняет Python-тесты на Linux, Windows и macOS. Linux integration отдельно устанавливает filesystem utilities и исполняет checker/recovery acceptance. OS matrix публикует native-read evidence и checker inventory, но inventory — это наличие инструмента, а не доказательство его выполнения. Реальные checker fixtures выполняются в Linux; текущая конфигурация не означает, что все checkers запускаются на Windows/macOS или что каждый профиль×операция имеет независимый oracle. [5] [15] [17]

### Решение

Сохранить разделение: portable native-read/adapter evidence на каждой поддерживаемой OS и реальные независимые filesystem checkers там, где они действительно доступны и авторитетны — сейчас в Linux integration. Для каждой ячейки профиля/операции публиковать статус `pass`, `unavailable` или `not_claimed`, reason, OS/runner, commit, fixture hash, checker/tool version, exit status и before/after hashes. Нельзя переводить отсутствующую утилиту, skip, synthetic fixture или pytsk3-only сравнение в independent checker pass. Не объявлять APFS/HFS+ parity там, где проверен только adapter/mock контракт.

Все image fixtures — тестовые и disposable; `e2fsck`, `fsck.fat`, `fsck.exfat` запускаются только с no-write/no-repair аргументами. `ntfsfix` не считается эквивалентом `chkdsk` или полноценным NTFS oracle. Отсутствующая обязательная Linux fixture/checker должна блокировать соответствующий gate и сохранять диагностический artifact. [5] [15] [18] [19] [20]

## #390 — воспроизводимая benchmark matrix и синхронизация статуса

### Что уже есть и чего не хватает

`benchmarks/bench_vfs.py` — bounded read-only harness, который фиксирует среду, сравнивает размеры/SHA-256 извлечённых файлов и явно устанавливает `parity=not_claimed`. Linux integration генерирует ext2 fixture через `mke2fs`/`debugfs` и запускает benchmark. Документация прямо указывает, что Windows/macOS provenance и write workload не готовы. Поэтому нынешние результаты — измерение конкретной Linux-среды, а не cross-platform benchmark matrix и не доказательство filesystem parity. [6] [21] [22]

### Решение

Сначала сделать Python entrypoint переносимым и использовать одни и те же fixture bytes/hash, workload и schema на Linux/Windows/macOS. Каждый artifact должен включать commit и clean-worktree, OS/архитектуру и runner image version, Python/package/tool versions, fixture generator/hash, offset/volume size, workload, warmups/repeats, cache policy, limits и raw samples. CI агрегатор должен требовать все ожидаемые legs/cells и отклонять несогласованные fixtures, пропуски и неполную provenance. Если runner не может выполнить конкретный checker, он выдаёт `unavailable`/`not_claimed`, а не pass.

Write microbenchmark можно добавлять только как отдельный этап, только для уже объявленных bounded writer-профилей, только на новой disposable-копии для каждого повтора. Запись и независимый checker не включаются в измеряемый таймер; source hash сверяется до/после. Benchmark не повышает readiness, parity, recovery или power-loss claims. Синхронизацию README/CHANGELOG/CLI/GUI/VFS следует расширить до generated machine-readable claim manifest; validator остаётся проверочным и никогда не меняет пользовательский PR/файлы автоматически. [6] [7] [21] [22] [23]

## Оценка приложенных черновиков интеграции

Предложенные фрагменты **не добавляются в runtime-код как готовые реализации**. В `JBD2Backend` методы start/commit/abort/recover возвращают `True`, не читают и не меняют журнал и не обеспечивают durable ordering; `Ext4Writer.write_file()` всё ещё выбрасывает `NotImplementedError`. Такой API может создать ложное впечатление, что транзакция защищена, хотя записи и восстановления нет. В проекте уже есть ограниченные JBD2 record/checkpoint helpers; следующий шаг — интегрировать их в проверяемый coordinator и пройти gates из #385/#387, а не добавлять ещё одну no-op-обёртку. [1] [9] [10]

`NTFSLogFileValidator` из черновика также не является валидатором: `is_consistent()` безусловно возвращает `True`, а `recover_pending()` всегда сообщает пустой список. Заявленные импорты/API `dfir_ntfs` в примере не проверены контрактными тестами и не заменяют существующий bounded read-only NTFS parser проекта. До подтверждения поддержки нужных версий и формата журналов нельзя трактовать отсутствие исключения или пустой результат как доказательство чистого `$LogFile`; текущий native NTFS mutation остаётся запрещённым. [2] [11] [12] [25]

Обёртка `safe_image_write()` не может сама гарантировать crash consistency для текущих image writers: копирование целого образа может потребовать сопоставимый объём дополнительного места и времени, а поведение при short write, `fsync`, сбое восстановления, конкурентном изменении, открытом Windows handle и падении процесса в каждой фазе примера не проверяется. Упомянутая фикстура `pyfakefs` проверяет только исключение при записи в fake filesystem; это не тест реального rollback, аварийного завершения процесса или отключения питания. Эти идеи следует выражать через существующий writer fault seam и disposable-image/process-kill acceptance из #387; новую транзакционную библиотеку принимать только после аудита её API, гарантий и поведения на поддерживаемых ОС. [3] [14] [15]

Предложенные зависимости тоже не переносятся в основные requirements: `pip-audit` уже устанавливается и запускается в `.github/workflows/security.yml` на `requirements.txt` и `requirements-win7.txt`, поэтому отдельный `security-audit.yml` дублировал бы gate, а включение самого аудитора в runtime dependencies не требуется. `pyfakefs` может рассматриваться только как test extra, если появятся тесты, которые реально выигрывают от fake filesystem; он не заменяет disposable images и fault injection. `safefile` и `dfir_ntfs` не добавляются в core dependencies без проверки происхождения/лицензии, совместимости, стабильного закрепления версии и доказательства того, что их реальные интерфейсы обеспечивают нужный контракт. Текущий dependency/security gate сохраняется единственным источником аудита. [41]

Безопасная последовательность остаётся такой: сначала узко определить форматный профиль и транзакционные guarantees; затем написать настоящие bounded операции поверх существующей архитектуры, возвращающие отказ, пока гарантия не доказана; после этого добавить реальные image fixtures, failure injection/process-kill tests, проверку исходного/нового целого состояния и независимый checker. Никакие методы `recover`, `is_consistent` или `commit` не должны возвращать успех, пока соответствующая работа действительно не выполнена и не проверена.

### Второй черновик: Dissect, dfVFS, parser sandbox и Hypothesis

Эти фрагменты также не принимаются как готовый runtime-код. `DissectReader.read_file()` вызывает `fh.read()` без лимита, а рекурсивный `iter_files()` не задаёт предел глубины/числа элементов и политику для ссылок/циклов; обёртка читает образ через произвольный file handle вместо существующего read-only `ImageSource`/`BoundedImageSource`. В примере импортируются только Btrfs и XFS, хотя заявлен также F2FS. До появления отдельного профильного scope и fixtures нельзя объявлять поддержку этих ФС; adapter должен проверять bounds и metadata limits и выполнять bounded range reads. [1] [5]

`open_dfvfs_entry()` открывает лишь верхний entry, а `read_dfvfs_file()` прямо выбрасывает `NotImplementedError`; навигация по внутреннему filesystem object и bounded content read не реализованы. Поэтому это пока API-эскиз, не reader контейнеров. Любая будущая интеграция EWF/QCOW2/VMDK должна отдельно тестировать сегменты/extent bounds, parser errors и связь с VFS, не возвращая успех для unsupported layout.

Предложенный `run_parser_isolated()` не является OS security sandbox: модуль `resource` и `preexec_fn` POSIX-специфичны и не работают как переносимая изоляция на Windows; дочерний процесс наследует те же права пользователя, доступ к файловой системе/сети и почти всё окружение (включая возможные секреты). `capture_output=True` не ограничивает объём stdout/stderr, а timeout не гарантирует завершение всей группы потомков. `RLIMIT_AS/CPU` полезны лишь как дополнительные limits на совместимых системах, но не заменяют отдельный threat model и OS-specific confinement. Не подключать этот API к обработке враждебных образов под обещанием «изоляции»; сначала нужны минимальное очищенное окружение, ограниченные input/output, read-only handle, process-tree termination и подтверждённые platform controls.

Hypothesis state machine в примере импортирует `mountfile.safe_write.safe_atomic_commit/read`, которых в checkout нет. Кроме того, она ловит любой `Exception` при чтении и считает его допустимым, а её единственная invariant проверяет отсутствие `*.tmp`; поэтому повреждение уже успешно записанных данных может пройти тест. Это не моделирует commit/abort, writer operations или crash boundaries. `hypothesis` уже присутствует в dev extra; применять его следует после появления реального writer API, с моделью ожидаемого состояния и fault injection, не подавляя неожиданные ошибки.

Зависимости `dissect.btrfs`, `dissect.xfs`, `dfvfs`, `atomicwriter` и `safeatomic` в предложенном виде не добавляются: API/совместимость и транзакционные гарантии ещё не подтверждены, а `dfir_ntfs` задан прямым mutable Git ref. `pip-audit` уже обслуживается имеющимся security workflow. Btrfs/XFS/F2FS и контейнерные форматы расширяют scope за пределы текущих #385–#390; сначала требуется отдельное решение о приоритетах, лицензиях/provenance, optional dependency extra, bounds, VFS contract и независимых fixtures. До этого не добавлять модули, которые создают видимость поддержки.

### Третий черновик: `UnifiedForensicFacade` и `LegacyAdapter`

Этот фасад не может быть добавлен поверх предыдущих эскизов как «единый fail-closed API»: в checkout отсутствуют импорты `dissect_reader`, `dfvfs_reader`, `safe_write` и `sandbox`, а также функции `safe_write_image`, `safe_atomic_commit` и `safe_atomic_read`. Следовательно, модуль не импортируется и не создаёт работоспособную интеграционную границу.

Даже после появления тех модулей примеры содержат ошибки контракта: `read_container_file()` вызывает непроверенный `GetFileEntryByName()` и читает файл целиком через `.read()`; `isolation=False` только пишет warning, но всё равно запускает `run_parser_isolated()`; `write_image_atomic()` получает `atomic_writer`, однако `LegacyAdapter.migrate_fat32_write()` его не передаёт старому writer-у — вызов `writer.write_file(path, data)` продолжает писать через прежний объект. Поэтому лог «выполнена атомарно» не обоснован.

`sync_all()` изменяет переданные вызывающим кодом dict через `pop("op")`, не имеет ветки `read_output`, хотя пример-тест её вызывает, и передаёт `output=...` в метод, который принимает `output_path`; показанный `test_sync_all_operations` завершится ошибкой до заявленной проверки результата. Последовательный `sync_all` также не является одной транзакцией: сбой поздней операции оставит ранние результаты, если отдельно не определить rollback/commit contract.

Не добавлять facade/legacy adapter как универсальную точку обхода существующих VFS/profile/refusal contracts. Если такая абстракция понадобится, её API должен вызывать реальные backend capabilities, передавать один проверенный image/session object, ограничивать объём чтения, возвращать typed refusals и иметь executable tests для каждой операции. До этого безопасное решение — продолжать интеграцию через текущие VFS/API contracts, не маркировать старые writer-ы как атомарные и не добавлять отсутствующие modules/dependencies ради удовлетворения imports в черновике.

## Рекомендуемый порядок работ

1. **#388 — conformance/refusal parity.** Зафиксировать, что каждая объявленная доступная операция действительно работает на fixture, а каждый запрет возвращает правильный typed refusal; зафиксировать no-fallback policy.
2. **#389 и #390 — доказательства.** Сделать версионированную схему artifacts и переносимый benchmark/evidence runner; отдельно сохранить Linux-only checker execution там, где нет подходящего checker на других OS.
3. **#387 — crash/failure acceptance.** Сопоставить заявленные recovery cells с реально исполняемыми тестами и запустить deterministic injection/process-kill по каждой включаемой write операции.
4. **#385 — ext3 MVP.** Только после одобрения точной allowlist операций, интегрировать JBD2 coordinator и пройти весь acceptance gate.
5. **#386 — NTFS.** Начать с corpus и read-only replay model; native writer — последний этап, поскольку полная внутренняя `$LogFile` recovery спецификация недостаточна и потребуется differential validation с Windows.

До завершения соответствующего этапа ext3/ext4 и NTFS должны оставаться read-only; dirty, malformed, unknown и recovery-required состояния должны отказывать до первой записи. Ни performance, ни наличие parser, ни успешная проверка копии одним checker-ом не заменяют транзакционные crash tests. [1] [2] [3] [7]

## References

[1]: https://github.com/Alexkkkkk/mount-file/issues/385 "Implement native ext3 ext4 writer with JBD2 transaction and recovery gates"
[2]: https://github.com/Alexkkkkk/mount-file/issues/386 "Implement native NTFS writer with LogFile transaction recovery"
[3]: https://github.com/Alexkkkkk/mount-file/issues/387 "Complete writer acceptance matrix"
[4]: https://github.com/Alexkkkkk/mount-file/issues/388 "Unify native VFS API and capability refusal parity"
[5]: https://github.com/Alexkkkkk/mount-file/issues/389 "Expand cross-platform filesystem fixtures and independent checker CI"
[6]: https://github.com/Alexkkkkk/mount-file/issues/390 "Add reproducible benchmark matrix and synchronize product status"
[7]: https://github.com/Alexkkkkk/mount-file/tree/ec7b4c61c3fc0f6e7845dec11215416161323e3d "Mount File main at reviewed commit"
[8]: https://github.com/Alexkkkkk/mount-file/blob/main/mountfile/capabilities.py "Canonical profile, operation and recovery capability matrix"
[9]: https://docs.kernel.org/filesystems/ext4/journal.html "Linux ext4 JBD2 journal format and recovery"
[10]: https://github.com/torvalds/linux/blob/master/fs/jbd2/recovery.c "Linux kernel JBD2 recovery implementation"
[11]: https://github.com/Alexkkkkk/mount-file/blob/main/mountfile/ntfs_reader.py "Bounded NTFS metadata and LogFile restart-area reader"
[12]: https://learn.microsoft.com/en-us/windows-server/storage/file-server/ntfs-overview "Microsoft NTFS overview"
[14]: https://github.com/Alexkkkkk/mount-file/blob/main/scripts/recovery_acceptance.py "Executed writer recovery acceptance fixtures"
[15]: https://github.com/Alexkkkkk/mount-file/blob/main/scripts/checker_acceptance.py "Independent filesystem checker acceptance runner"
[16]: https://github.com/Alexkkkkk/mount-file/pull/399 "Add RW stat to capability vocabulary"
[17]: https://github.com/Alexkkkkk/mount-file/blob/main/.github/workflows/ci.yml "Cross-platform tests and Linux checker integration"
[18]: https://man7.org/linux/man-pages/man8/e2fsck.8.html "e2fsck read-only mode and exit statuses"
[19]: https://manpages.debian.org/testing/dosfstools/fsck.fat.8.en.html "fsck.fat no-write mode"
[20]: https://manpages.debian.org/testing/ntfs-3g/ntfsfix.8.en.html "ntfsfix scope and limitations"
[21]: https://github.com/Alexkkkkk/mount-file/blob/main/benchmarks/bench_vfs.py "Bounded read-only VFS benchmark harness"
[22]: https://github.com/Alexkkkkk/mount-file/blob/main/docs/benchmarks.md "Benchmark methodology and current cross-platform limitations"
[23]: https://github.com/Alexkkkkk/mount-file/blob/main/scripts/product_status_contract.py "Conservative product-status contract validator"
[24]: https://github.com/Alexkkkkk/mount-file/pull/447 "JBD2 superblock validation and disposable checker gate"
[25]: https://github.com/Alexkkkkk/mount-file/pull/474 "Harden read-only NTFS LogFile restart checks"
[26]: https://github.com/Alexkkkkk/mount-file/pull/407 "Machine-readable writer recovery matrix"
[27]: https://github.com/Alexkkkkk/mount-file/pull/417 "Executable writer recovery acceptance fixtures"
[28]: https://github.com/Alexkkkkk/mount-file/pull/399 "RW stat capability"
[29]: https://github.com/Alexkkkkk/mount-file/pull/400 "Bounded RW read_range capability"
[30]: https://github.com/Alexkkkkk/mount-file/blob/main/tests/test_capabilities.py "Current capability contract tests"
[31]: https://github.com/Alexkkkkk/mount-file/pull/397 "Checker evidence vocabulary"
[32]: https://github.com/Alexkkkkk/mount-file/pull/404 "Checker fixture declarations"
[33]: https://github.com/Alexkkkkk/mount-file/pull/416 "Checker acceptance runner and artifacts"
[34]: https://github.com/Alexkkkkk/mount-file/pull/441 "Cross-platform native read evidence"
[35]: https://github.com/Alexkkkkk/mount-file/pull/402 "Benchmark capability evidence"
[36]: https://github.com/Alexkkkkk/mount-file/pull/409 "Product status contract"
[37]: https://github.com/Alexkkkkk/mount-file/pull/419 "Benchmark artifact validation"
[38]: https://github.com/Alexkkkkk/mount-file/pull/468 "Generated README status synchronization"
[39]: https://github.com/Alexkkkkk/mount-file/blob/main/.github/workflows/ci.yml "Current OS matrix and Linux integration acceptance"
[40]: https://github.com/Alexkkkkk/mount-file/blob/main/scripts/generate_readme.py "Canonical generated README status block updater"
[41]: https://github.com/Alexkkkkk/mount-file/blob/main/.github/workflows/security.yml "Existing dependency review and pip-audit gates"
