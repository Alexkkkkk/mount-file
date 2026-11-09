# Отчёт: продолжение функционального аудита NTFS

**Ветка:** `fix/full-functional-audit-2026-11-05`
**Область:** `mountfile/ntfs_reader.py`, `tests/test_functional_audit_ntfs_followup.py`
**Режим:** ограниченная проверка; без записи в образы, commit, merge или push.

## Исправления

- Обычные независимые образы `mkfs.ntfs` размером 32 MiB с кластерами 512, 4096 и 8192 байта теперь проходят публичные `list_dir('/')`, `stat` корня и файлов, чтение полных файлов и bounded range-чтение. В корне распознаётся и перечисляется `$BadClus`; публичная запись явно помечает `content_supported: false` и `unsupported_streams: ["$Bad"]`.
- Дескриптор NTFS-атрибута теперь сохраняет признак поддержки содержимого. Sparse-run декодируется как VCN-экстент `lcn=None`, без подстановки физического смещения. Разрешение разрежённого дескриптора ограничено именованным `$Bad` в **системной записи MFT 8**; перед принятием проверяются корневое имя `$BadClus`, ссылка и sequence корневой записи, атрибуты DATA, полный диапазон VCN, размеры, нулевой initialized size и исключительно sparse-runlist. Повреждённая или иная метадата не становится допустимой только из-за номера записи.
- `file_size`/listing позволяют описать `$BadClus`, но `read_file`, `read_file_range` (включая нулевую длину), прямое чтение sparse range и низкоуровневое чтение sparse runlist явно отказывают с `OPERATION_NOT_SUPPORTED`. Это не выглядит как успешно прочитанные нулевые байты.
- Обычная проверка runlist по умолчанию совместима с `origin/main` (`lcn=None`); потоковый парсер атрибутов не допускает sparse extents, кроме указанного узкого случая. Сохранены проверки границ VCN/LCN, runlist, атрибутов, индексов, профиля и dirty/recovery.

## Тесты и форматирование

Новый функциональный тест независимо создаёт и заполняет NTFS через `mkfs.ntfs`/`ntfscp`, сверяет публичный список с pytsk3 и проверяет статусы, содержимое, диапазонные чтения, отсутствие изменений image, отказы чтения `$BadClus`, malformed sparse runlist/размеры/флаги/имя/родителя/directory flag и dirty-volume refusal.

- `pytest` на snapshot upstream `origin/main` для `tests/test_ntfs_reader.py` плюс `tests/test_ntfs_mft.py`, `tests/test_ntfs_probe.py`, `tests/test_ntfs_fuzz_properties.py` и новый follow-up файл: **208 passed**.
- Только новый файл: **62 passed** (512/4096/8192 B варианты включены).
- `isort --check-only`, `black --check` обоих принадлежащих этой задаче файлов: прошли.
- `git diff --check -- mountfile/ntfs_reader.py`: прошёл.
- Сверка ожидаемых изменений: проверен только для чтения `git show origin/main -- mountfile/ntfs_reader.py tests/test_ntfs_reader.py`; два теста upstream для sparse extents исполнены отдельно и прошли.

## Ограничения и передача родителю

Сжатые, sparse (кроме описанной специальной метадаты `$BadClus`) и иные неподдерживаемые потоки не читаются. `$BadClus:$Bad` остаётся явно нечитаемым; записи или восстановления NTFS нет.

Старый локальный `tests/test_ntfs_reader.py::test_rejects_sparse_ntfs_run` конфликтует с upstream-изменением, которое ожидает, что общий `_parse_runlist` распознаёт sparse-run как `lcn=None`. Также локальная `tests/test_functional_audit_filesystems.py::test_real_ntfs_cluster_vcns_and_mft_padding` ожидает, что пустой `$BadClus` заставит root-listing упасть; это предположение опровергнуто новым публичным тестом. После upstream-снимка полный NTFS-прогон прошёл. Эти чужие ожидания намеренно не редактировались: согласовать/обновить их следует при родительском объединении.

Изменённый NTFS reader уже содержит находившиеся в рабочем дереве upstream-правки MFT padding, directory flags, root self-entry и index VCN; их diff не создавался повторно. Файлы вне двух выделенных путей не редактировались. Изменения не коммичены и не отправлены.
