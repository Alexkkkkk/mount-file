# Issue #182 — статус выполнения

Этот файл — источник статуса для автоматического progress-комментария в GitHub issue #182. При **каждом commit** на ветке с `issue-182` или в commit message со ссылкой `#182` обновляйте этот файл; GitHub Action проверяет это условие и после push синхронизирует один progress-комментарий. Для merge push проверяется first-parent цепочка ветки, а не уже существовавшие commits второй родительской ветки. GitHub получает commits только после push, поэтому локальный commit появится в issue после отправки ветки.

Статус относится к commit/ветке, указанным в автоматически обновляемом комментарии. Пока изменение не влито в `main`, оно не считается готовым в `main`.

**Последняя заметка (preflight):** добавлен read-only слой `mountfile/fs_preflight.py` и CLI-команда `preflight`: он не открывает writer, а публикует выбранное семейство ФС, профиль из capability matrix, трёхзначные гейты (pass/fail/unknown), typed refusal и явный отказ на любую мутацию. Неизвестный формат не маршрутизируется в pytsk3-fallback. Полное завершение issue не заявляется.

**Последняя заметка:** bounded classic JBD2 serializer и wrap-safe planner остаются отдельным экспериментом: escaping не поддерживается, serializer отказывает на требующем escape payload, а кодировщик и planner не записывают образ или journal superblock. ext3/ext4 writer API остаётся отключённым до ordering/checkpoint/recovery gates. Progress publisher валидирует issue-ветки read-only и публикует один комментарий только с актуального `main`; tracker не сканирует исходники. Native ext2/ext3/ext4 readers теперь используют консервативную allowlist inode flags и отвергают неизвестные, зарезервированные и неподдержанные биты при обращении к inode, не меняя образ. Native exFAT теперь ограничен one-FAT профилем: двух-FAT TexFAT отказывается до чтения metadata или записи, без legacy fallback; геометрический probe не является разрешением доступа. Capability/refusal status, bounded benchmark provenance, FAT12/FAT16 identity и повторное открытие VFS также усилены. Для quality milestone #182 scoped correctness gate (`S110`, `S112`, `F841`, `B006`, `E722`) теперь проходит с 0 findings; полный Ruff modernization backlog и coverage >80% остаются отдельными задачами. Полное завершение issue не заявляется.

**Recovery-contract milestone:** schema v5 capability/status теперь публикует для каждого профиля отдельные поля `rollback`, `process_kill`, `power_loss` и `quarantine`. Для ext2 подтверждены failure rollback, process-crash recovery sidecar и quarantine при recovery failure; для FAT12/16, FAT32 и one-FAT exFAT failure rollback покрыт, но process-kill/quarantine не доказаны. Для всех native writers power-loss остаётся явно `unsupported`; это не объявление полной crash-safety или filesystem parity.

Для Issue #387 добавлен `scripts/recovery_matrix.py`: он строит machine-readable
acceptance report по всем native RW profiles. Отчёт не запускает тесты и не
повышает claims: неподтверждённые cells получают `blocking_cells`, verdict
остаётся `blocked`, а `production_ready` — `false`. Даже ext2, где process-kill
и quarantine покрыты recovery-sidecar тестами, остаётся заблокированным из-за
явно неподдержанного power-loss.

**External-checker matrix milestone:** `scripts/checker_matrix.py` и Linux integration artifact `external-checker-matrix-linux` публикуют воспроизводимую матрицу native profile × checker × runner availability × fixture evidence для `e2fsck`, `fsck.fat`, `fsck.exfat`, `ntfsfix` и `pytsk3`. Отсутствующий инструмент даёт `unavailable`, а `parity` всегда остаётся `not_claimed`; `ntfsfix` явно не считается authoritative checker. Полная format parity и cross-platform parity по-прежнему не заявляются.

**Product-status contract milestone:** `scripts/product_status_contract.py`
сверяет версию capability matrix, recovery report, checker evidence и
консервативные markers этого status sheet. Его `pass` означает только
согласованность заявлений; это не означает production readiness. Validator
обязывает сохранять `parity=not_claimed`, `power_loss=unsupported`, а native
ext3/ext4 и NTFS writes — `unsupported`.

**Executed recovery evidence milestone:** `scripts/recovery_acceptance.py`
запускает cancellation, process-die, rollback и quarantine fixtures для ext2,
FAT32 и exFAT и публикует JSON/JUnit artifact в Linux CI. Успешное выполнение
этих fixtures подтверждает только воспроизводимость конкретных сценариев;
`production_ready` остаётся `false`, а `power_loss` — `unsupported`.

| ID | Задача | Статус | Что подтверждено / что осталось |
|---|---|---|---|
| R1 | Native read API для заявленных bounded-профилей FAT, exFAT, ext2/3/4 и NTFS | Частично | Есть readers для принятых profiles; полный feature/layout/API parity не заявлен. |
| W1 | Native write API для файловых систем из scope | Не готово | Есть ограниченные FAT12/16, FAT32, ext2 (файлы и каталоги: bounded double-indirect) и one-FAT exFAT writers; native ext3/ext4/NTFS writers отсутствуют. |
| A1 | Единый capability/refusal contract во всех интерфейсах | Частично | Matrix и typed refusals есть; проводится сверка документации и реальных profile/state limits. |
| S1 | Fail-closed, bounds, отказ без изменения исходного образа | Частично | Есть общие primitives и значительное профильное покрытие; не доказана полная матрица `profile × state × operation`. |
| V1 | Read-back и независимый checker для каждого writer | Частично | Linux CI требует e2fsprogs/dosfstools/exfatprogs/ntfs-3g для реальных oracle-тестов; full profile × operation matrix и writer-oracle parity ещё не закрыты. |
| C1 | Crash recovery/quarantine и проверка process-kill/power-loss | Частично | ext2 имеет sidecar recovery и process-crash tests; нет общей полной crash/power-loss гарантии для всех writers. |
| CI1 | Обязательная Linux/Windows/macOS матрица с реальными fixtures | Частично | CI workflow и локальные тесты существуют; полный набор внешних checker/integration tests на всех ОС не подтверждён. |
| B1 | Воспроизводимые benchmark на Linux, Windows и macOS | Не готово | Опубликован ограниченный ext2/Linux ARM64 результат; cross-platform benchmark matrix отсутствует. |
| D1 | Единый актуальный статус в docs, GUI, CLI, VFS и release | Частично | FAT12/16, ext2 files/directories, ext4 inode flag policy, one-FAT exFAT policy и benchmark provenance синхронизированы; полное согласование интерфейсов и release status ещё впереди. |
| P1 | Пройти все acceptance gates и объявить поддержанные профили production-ready | Не готово | Все обязательные gates должны выполняться одновременно; текущие ограничения выше не позволяют считать issue завершённой. |

### Обновление bounded ImageSource / NTFS

Добавлен общий read-only `ImageSource` с positioned reads, проверкой границ выбранного extent, защитой от concurrent truncation и bounded `NTFS` integration. Это закрывает общий источник чтения и lifecycle safety, но не объявляет NTFS writer или полную NTFS feature parity production-ready.

## Решение для обновления статуса

Не помечать семейство «готово» только из-за наличия reader/writer класса. Для завершения профиля нужны точные поддерживаемые операции и features, положительные и отрицательные fixtures, независимая проверка, failure/crash tests, согласованная документация и CI на заявленных платформах.

### Quality-gate follow-up

В PR #344 проверка пяти правил correctness-lint распространена на весь репозиторий, а не только на `mountfile/` и `tests/`. В базе `main` было 51 нарушение; все оставшиеся нарушения в приложении и maintenance tools исправлены, расширенная проверка проходит с 0 findings. Это обновление включено в тот же commit, чтобы пройти проверку issue-182 ветки.

- 2026-11-05: NTFS dirty `$Volume` и recovery-required `$LogFile` получают стабильный `DIRTY_OR_RECOVERY_REQUIRED` code; добавлены regression assertions. API, capability registry и audit docs уточняют bounded resident/nonresident `$ATTRIBUTE_LIST` support; это follow-up hardening, не закрытие полной profile × operation/crash-safety acceptance matrix.

- 2026-11-05: exFAT rename выполняет полный allocation-owner scan перед изменением stage и отказывается от cross-linked file/directory clusters без изменения исходного образа. Writer также очищает stage после partial-write или interrupted-copy exception, включая `KeyboardInterrupt`; regression проверяет byte-identical source и отсутствие временных файлов. Это не гарантия cleanup/recovery после process kill или power loss.

- 2026-11-04: PR #349 native VFS tree synchronized with main and local suite verified (1812 passed).
<!-- issue-182 validation checkpoint -->
<!-- issue-182 validation checkpoint f5547524c074f406c5756509c1f5b61761f35ab0 -->
<!-- issue-182 validation checkpoint merge -->
- 2026-11-04: read-only per-filesystem preflight (профили, гейты, отказы) + CLI `preflight`.

- 2026-11-04: PR #356 (preflight gate) — `mountfile/cli.py` и `mountfile/fs_preflight.py` приведены к black, импорты `tests/test_fs_preflight.py` к isort; Quality/lint проходят.

- 2026-11-04: PR #347 — ветка синхронизирована с main, CLI status follow-up зафиксирован в статусе.

- 2026-11-04: status sheet refreshed so the issue-182 progress validator has a green head on `main` (previous `ci(actions)` push had no status update and left `validate-push` red).
- 2026-11-04: ветка CLI status синхронизирована с `main` (merge): конфликт status sheet разрешён, CLI-команда status не изменилась.

- 2026-11-04: PR #352 — ветка синхронизирована с main, случайный пустой файл log удалён.

- 2026-11-04: status sheet refreshed so the issue-182 progress validator has a green head on `main` (previous `ci(actions)` push had no status update and left `validate-push` red).
- 2026-11-04: ветка синхронизирована с `main` (merge): конфликт status sheet разрешён, гейты preflight не изменились.

- 2026-11-05: независимый ext2 `e2fsck -f -n` oracle теперь также проверяет файл 300 KiB+ через disposable copy, проходящий границу 12 direct + 256 single-indirect блоков при 1 KiB block size. Checker suite: 8 passed, 2 skipped (локально отсутствуют exFAT/NTFS tools); V1 остаётся частично закрытым.

- 2026-11-04: ветка fail-closed gaps синхронизирована с `main` (merge): конфликт status sheet разрешён, fail-closed гейты не изменились.
- 2026-11-05: real-image acceptance покрывает FAT12/16 и FAT32 native writers: `mkfs.fat` создаёт fixtures, `fsck.fat -n` проверяет disposable copies после поддерживаемых create/mkdir/write/rename/delete/read-back операций; FAT12/16 failure injection подтверждает точный rollback и чистый checker verdict. Focused suites: 17 passed, 2 skipped (локально отсутствуют exFAT/NTFS tools). Это закрывает только часть V1; full profile × operation matrix остаётся незавершённой.

- 2026-11-04: ветка fail-closed gaps синхронизирована с `main` (merge): конфликт status sheet разрешён, гейты preflight не изменились.
- 2026-11-04: ветка preflight повторно синхронизирована с `main` после вливания PR #352; конфликт status sheet разрешён.
- 2026-11-04: ветка CLI status повторно синхронизирована с `main` после вливания PR #352; конфликт status sheet разрешён.
