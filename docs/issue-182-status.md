# Issue #182 — статус выполнения

Этот файл — источник статуса для автоматического progress-комментария в GitHub issue #182. При **каждом commit** на ветке с `issue-182` или в commit message со ссылкой `#182` обновляйте этот файл; GitHub Action проверяет это условие и после push синхронизирует один progress-комментарий. GitHub получает commits только после push, поэтому локальный commit появится в issue после отправки ветки.

Статус относится к commit/ветке, указанным в автоматически обновляемом комментарии. Пока изменение не влито в `main`, оно не считается готовым в `main`.

**Последняя заметка:** добавлен лёгкий sync-процесс статуса при push; он публикует этот checklist и не сканирует исходники.

| ID | Задача | Статус | Что подтверждено / что осталось |
|---|---|---|---|
| R1 | Native read API для заявленных bounded-профилей FAT, exFAT, ext2/3/4 и NTFS | Частично | Есть readers для принятых profiles; полный feature/layout/API parity не заявлен. |
| W1 | Native write API для файловых систем из scope | Не готово | Есть ограниченные FAT12/16, FAT32, ext2 и exFAT writers; native ext3/ext4/NTFS writers отсутствуют. |
| A1 | Единый capability/refusal contract во всех интерфейсах | Частично | Matrix и typed refusals есть; проводится сверка документации и реальных profile/state limits. |
| S1 | Fail-closed, bounds, отказ без изменения исходного образа | Частично | Есть общие primitives и значительное профильное покрытие; не доказана полная матрица `profile × state × operation`. |
| V1 | Read-back и независимый checker для каждого writer | Частично | Есть read-back и отдельные ext2 проверочные тесты; требуются полные обязательные oracle runs для FAT/exFAT и всех заявленных профилей. |
| C1 | Crash recovery/quarantine и проверка process-kill/power-loss | Частично | ext2 имеет sidecar recovery и process-crash tests; нет общей полной crash/power-loss гарантии для всех writers. |
| CI1 | Обязательная Linux/Windows/macOS матрица с реальными fixtures | Частично | CI workflow и локальные тесты существуют; полный набор внешних checker/integration tests на всех ОС не подтверждён. |
| B1 | Воспроизводимые benchmark на Linux, Windows и macOS | Не готово | Опубликован ограниченный ext2/Linux ARM64 результат; cross-platform benchmark matrix отсутствует. |
| D1 | Единый актуальный статус в docs, GUI, CLI, VFS и release | Частично | FAT12/16, ext2 double-indirect files и ext4 64BIT claims исправлены в этой ветке; согласование всех интерфейсов, release status и merge ещё впереди. |
| P1 | Пройти все acceptance gates и объявить поддержанные профили production-ready | Не готово | Все обязательные gates должны выполняться одновременно; текущие ограничения выше не позволяют считать issue завершённой. |

## Решение для обновления статуса

Не помечать семейство «готово» только из-за наличия reader/writer класса. Для завершения профиля нужны точные поддерживаемые операции и features, положительные и отрицательные fixtures, независимая проверка, failure/crash tests, согласованная документация и CI на заявленных платформах.
