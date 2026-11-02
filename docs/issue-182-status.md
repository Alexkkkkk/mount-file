# Issue #182 — статус выполнения

Этот файл — источник статуса для автоматического progress-комментария в GitHub issue #182. При **каждом commit** на ветке с `issue-182` или в commit message со ссылкой `#182` обновляйте этот файл; GitHub Action проверяет это условие и после push синхронизирует один progress-комментарий. Для merge push проверяется first-parent цепочка ветки, а не уже существовавшие commits второй родительской ветки. GitHub получает commits только после push, поэтому локальный commit появится в issue после отправки ветки.

Статус относится к commit/ветке, указанным в автоматически обновляемом комментарии. Пока изменение не влито в `main`, оно не считается готовым в `main`.

**Последняя заметка:** bounded classic JBD2 serializer и wrap-safe planner остаются отдельным экспериментом: escaping не поддерживается, serializer отказывает на требующем escape payload, а кодировщик и planner не записывают образ или journal superblock. ext3/ext4 writer API остаётся отключённым до ordering/checkpoint/recovery gates. Progress publisher валидирует issue-ветки read-only и публикует один комментарий только с актуального `main`; tracker не сканирует исходники. Native ext2/ext3/ext4 readers теперь используют консервативную allowlist inode flags и отвергают неизвестные, зарезервированные и неподдержанные биты при обращении к inode, не меняя образ. Native exFAT теперь ограничен one-FAT профилем: двух-FAT TexFAT отказывается до чтения metadata или записи, без legacy fallback; геометрический probe не является разрешением доступа. Capability/refusal status, bounded benchmark provenance, FAT12/FAT16 identity и повторное открытие VFS также усилены. Полное завершение issue не заявляется.

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

## Решение для обновления статуса

Не помечать семейство «готово» только из-за наличия reader/writer класса. Для завершения профиля нужны точные поддерживаемые операции и features, положительные и отрицательные fixtures, независимая проверка, failure/crash tests, согласованная документация и CI на заявленных платформах.
