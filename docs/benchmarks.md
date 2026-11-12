# VFS benchmarks: методология и результаты (issue #182)

Критерий приёмки № 6 issue #182: «Benchmark native ≤ 20 % slower на
`range_read_1m`, `extract_10m`». Этот документ фиксирует воспроизводимую
методологию, фактические результаты и честные выводы. Главное ограничение
проекта: **производительность не может оправдать ослабление integrity gates** —
все измерения выполнены на неизменном read-only пути, и ни одна проверка
целостности не была отключена ради цифр.

## 1. Инструментарий

- `benchmarks/bench_vfs.py` — harness (schema v2, read-only):
  - источник открывается только для чтения; провайдер обязан вернуть нативный
    backend — если роутинг выбрал pytsk3-fallback, `native`-прогон
    помечается ошибкой, а не засчитывается (`test_native_benchmark_does_not_count_pytsk_fallback_as_native`);
  - обязателен явный ограниченный экстент тома (`--offset`, `--volume-size`),
    выход за границы образа отклоняется;
  - пути валидируются: абсолютные, нормализованные, без `..`;
  - range-read ограничен 1 МиБ на файл (`range_read_1m`); extract — полный файл
    во временную директорию с проверкой размера;
  - harness **не управляет** page cache ОС — состояние кэша фиксируется
    меткой (`--cache-state`), а не создаётся искусственно;
  - когда запускаются оба backend'а, их повторы выполняются попарно с
    чередованием порядка (`native → pytsk3`, затем `pytsk3 → native`); точная
    последовательность сохраняется в `backend_execution_order`. Это снижает
    систематический временной/order bias, но не очищает cache и не делает его
    cold или строго контролируемым;
  - сравнение backend'ов публикуется только при побайтовом совпадении
    содержимого (размер + SHA-256 каждого файла против эталонного прогона);
    при расхождении сравнение производительности скрывается.
  - каждый отчёт содержит `capability_contract` с версией общей matrix,
    проверенными операциями и явными значениями `evidence=benchmark_observed_only`
    и `parity=not_claimed`; каждый backend также получает вложенный
    `capability_status`. Benchmark не превращает измерение скорости или совпадение
    содержимого в доказательство полной filesystem parity.

Сохранённый отчёт можно проверить перед публикацией:

```bash
python scripts/benchmark_artifact.py /tmp/vfs-bench/bench_report.json \
  --json-out /tmp/vfs-bench/benchmark-validation.json
```

Validator требует schema v2, bounded image/workload, полный набор операций,
ровно заявленное число повторов, clean source provenance и консервативные
`evidence=benchmark_observed_only`/`parity=not_claimed`. Это проверка качества
артефакта, а не заявление filesystem parity; `write_operations` всегда false.
Report фиксирует обнаруженные версии `mke2fs` и `debugfs` вместе с жесткими
resource limits: image до 2 GiB, до 256 путей, совокупное извлечение до 256 MiB
на итерацию, range-read до 1 MiB на файл, до 100 измеряемых повторов и 20
warmups. Перед extraction harness сначала проверяет суммарный размер выбранных
файлов, чтобы превысивший лимит workload завершился до записи временных файлов.

## 2. Фикстура: синтетический ext2-образ

Бинарные образы в репозиторий не коммитятся. Детерминированный образ
генерируется локально:

```bash
python scripts/make_bench_ext2_image.py --output /tmp/bench.ext2
# Optional extended fixture for the acceptance workload:
python scripts/make_bench_ext2_image.py --output /tmp/bench-10m.ext2 --include-10m
```

Скрипт форматирует образ через `mke2fs -t ext2 -b 4096` и наполняет его через
собственный `NativeExt2ImageWriter` (staged-запись с hash-верификацией и
rollback-снимком), т. е. генерация фикстуры проходит через те же integrity
gates, что и продукт. Содержимое файлов детерминировано (SHA-256 от
`seed:counter`, псевдослучайные, плохо сжимаемые данные).

Опциональный `--include-10m` добавляет `/bench/large_10m.dat` в готовый образ
через `debugfs` исключительно как test/benchmark fixture setup для reader-а.
Текущий bounded ext2 writer поддерживает double-indirect regular files до
64 MiB; использование `debugfs` здесь не является обходом production writer
limit и само по себе не проверяет writer parity на 10 MiB. Нужны `mke2fs` и
`debugfs` из e2fsprogs.

После staged-записей генератор нормализует только metadata самой fixture:
UUID, directory-hash seed и поля времени superblock/inode. Поэтому два запуска
на одной версии `mke2fs` дают побайтно идентичный образ; это проверяют
`test_benchmark_fixture_generator_is_byte_reproducible` и
`test_10m_benchmark_fixture_is_reproducible_and_native_readable`. Production
writer не меняется: нормализация и debugfs fixture setup находятся только в
benchmark generator.

В стандартной фикстуре самый большой файл — 4 МиБ по выбору benchmark workload;
writer profile допускает большие regular files через bounded double-indirect
addressing. Для критерия `extract_10m` запустите расширенный режим runner:

```bash
INCLUDE_10M=1 scripts/run_vfs_benchmarks.sh /tmp/vfs-bench-10m
```

Параметры фикстуры (32 МиБ):

| Параметр | Значение |
| --- | --- |
| Размер образа | 33 554 432 байта (32 МиБ) |
| Файловая система | ext2 (mke2fs, `none,filetype,sparse_super,large_file`) |
| Размер блока ФС | 4096 байт |
| Offset тома | 0 байт |
| Volume size | 33 554 432 байта |
| mke2fs | e2fsprogs 1.47.0 |
| SHA-256 default fixture | `686da57622d45ad1906337f00d93561fc0be75f12bd222e604a1d10c1a1c1be4` |
| SHA-256 extended 10 MiB fixture | `2eb5a1b64d081d789fc5eb810489f7e2608dafb8015d7430db9489a7d41f6a43` |

Набор файлов (все под `/bench`, плюс каталог `many` для list_dir):

| Путь | Размер |
| --- | --- |
| `/bench/tiny_1k.dat` | 1 КиБ |
| `/bench/small_16k.dat` | 16 КиБ |
| `/bench/small_64k.dat` | 64 КиБ |
| `/bench/medium_512k.dat` | 512 КиБ |
| `/bench/large_1m.dat` | 1 МиБ |
| `/bench/large_4m.dat` | 4 МиБ (максимум нативного writer'а) |
| `/bench/large_10m.dat` | 10 МиБ (только с `--include-10m`, test-only debugfs fixture) |
| `/bench/many/file_{000,008,016}.dat` | 4 КиБ × 3 |

## 3. Окружение

| Параметр | Значение |
| --- | --- |
| Python | CPython 3.12.3 |
| Платформа | Linux-6.18.38+-aarch64-with-glibc2.39 |
| pytsk3 | 20260715 (manylinux aarch64 wheel; сборка из исходников не требовалась) |
| Cache state | `unspecified`; page cache не управлялся harness'ом |
| Методика повтора | 1 разогревочная итерация + 5 измеряемых итераций на backend; каждая итерация открывает и закрывает провайдера заново; open исключён из метрики итерации |

Воспроизведение:

```bash
scripts/run_vfs_benchmarks.sh /tmp/vfs-bench
# отчёт: /tmp/vfs-bench/bench_report.json (schema v2, полные выборки)
```

В обязательном Linux integration CI выполняется тот же read-only runner с
опцией `INCLUDE_10M=1`: он строит детерминированный 32 MiB ext2 образ с файлом
10 MiB, измеряет `list_dir`, `stat`, range-read (до 1 MiB на файл) и полное
извлечение для native и pytsk3, а затем валидирует JSON через
`scripts/benchmark_artifact.py`. Raw report содержит commit, состояние source
worktree, версии Python/pytsk3 и инструментов `mke2fs`/`debugfs`, объявленные
resource limits, размер и SHA-256 fixture, workload и метрики;
GitHub Actions сохраняет report и validation summary как artifact
`vfs-benchmark-linux`. Изменение benchmark contract или невалидный report
проваливает integration gate.

CI предоставляет воспроизводимый Linux baseline, но не заявляет cross-platform
performance: Windows/macOS benchmark provenance и fixture/checker matrix ещё
не готовы. Write workload в этом read-only harness отсутствует; его нельзя
считать выполненным или поддержанным. Для локального acceptance report запускайте
из чистого checkout, иначе validator закономерно отвергнет dirty source
provenance.

Каждый Linux/Windows/macOS test-leg теперь также публикует
`platform-benchmark-provenance.json`, создаваемый
`scripts/platform_benchmark_provenance.py`. Артефакт фиксирует commit и clean
worktree, OS/runner/Python, доступные checker tools, resource limits и полный
набор операций `list_dir`, `stat`, `read_range`, `extract`, `write`. Это
cross-platform provenance contract и не скрытая замена реальному filesystem
benchmark: каждый отчёт явно содержит `evidence=benchmark_observed_only`,
`parity=not_claimed`, `power_loss=unsupported` и
`crash_recovery=not_claimed`. Validator блокирует dirty source, неполный набор
операций, изменённые limits и любые claims о полной parity или power-loss
гарантиях.

## 4. Результаты

Два независимых запуска до новой 10 MiB fixture/reader оптимизации сохранены как
baseline и не являются текущим результатом. Для всех запусков ниже harness
использует один warmup, пять измеряемых итераций на backend и чередует порядок
пар. Метрика full iteration — медиана полного времени без открытия провайдера;
page cache не контролируется.

### 4.1 Baseline: исходная 9-файловая фикстура

Raw schema-v2 отчёты: [run 1](https://github.com/Alexkkkkk/mount-file/blob/main/benchmarks/results/issue-182-ext2-aarch64-2026-10-29-run1.json)
и [run 2](https://github.com/Alexkkkkk/mount-file/blob/main/benchmarks/results/issue-182-ext2-aarch64-2026-10-29-run2.json).

| Прогон | native, медиана итерации | pytsk3, медиана итерации | native/pytsk3 |
| --- | --- | --- | --- |
| run 1 | 0.02575 с | 0.01701 с | 1.51× (+51.4 %) |
| run 2 | 0.02924 с | 0.01950 с | 1.50× (+49.9 %) |

В обоих прогонах `content_equivalent = true`; SHA-256 fixture совпал
(`686da57622d45ad1906337f00d93561fc0be75f12bd222e604a1d10c1a1c1be4`).
Содержимое native и `pytsk3` совпало по размерам и SHA-256 каждого файла.

Оба baseline прогона были content-equivalent и использовали fixture SHA-256
`686da57622d45ad1906337f00d93561fc0be75f12bd222e604a1d10c1a1c1be4`.
Их результаты фиксируют gap до следующих изменений: native full iteration был
на 49.9–51.4 % медленнее `pytsk3`.

### 4.2 Текущий extended benchmark: 10 MiB extraction и range-read optimization

Два итоговых прогона выполнены на CPython 3.12.3 / Linux ARM64 с pytsk3 20260715.
Они используют одну и ту же детерминированную 32 MiB fixture с
`/bench/large_10m.dat`; fixture SHA-256:
`2eb5a1b64d081d789fc5eb810489f7e2608dafb8015d7430db9489a7d41f6a43`.
Raw schema-v2 отчёты: [run 1](https://github.com/Alexkkkkk/mount-file/blob/main/benchmarks/results/issue-182-ext2-10m-aarch64-2026-10-29-run1.json)
и [run 2](https://github.com/Alexkkkkk/mount-file/blob/main/benchmarks/results/issue-182-ext2-10m-aarch64-2026-10-29-run2.json).

| Прогон | Full iteration native / pytsk3 | `large_1m.dat` range-read native / pytsk3 | `large_10m.dat` extract native / pytsk3 |
| --- | --- | --- | --- |
| run 1 | 0.04048 с / 0.21686 с (0.187×) | 0.218 мс / 0.322 мс (0.679×) | 10.191 мс / 92.756 мс (0.110×) |
| run 2 | 0.04066 с / 0.21543 с (0.189×) | 0.215 мс / 0.312 мс (0.691×) | 10.594 мс / 92.335 мс (0.115×) |

Оба extended прогона вернули `content_equivalent = true`; SHA-256 всех файлов
совпал между backend'ами. `large_1m.dat` range-read — ровно 1 MiB; extraction
полностью записывает и проверяет размер 10 MiB. Хеширование destination
выполняется вне таймера.

### 4.3 Baseline operation aggregates (9 файлов, до range-read optimization)

| Операция | Вызовов/итерацию | native, медиана суммы за итерацию | pytsk3, медиана суммы за итерацию |
| --- | --- | --- | --- |
| list_dir | 2 | 0.622 мс | 0.315 мс |
| stat | 9 | 1.937 мс | 1.804 мс |
| read_range | 9 | 3.149 мс | 0.619 мс |
| extract | 9 | 13.282 мс | 7.462 мс |

Операционные времена в baseline таблице — медианы суммы вызовов за одну
итерацию run 1. `read_range` был ограничен 1 МиБ на файл.

## 5. Выводы (честные)

1. **Acceptance performance criterion № 6 достигнут на измеренной ARM64
   workload:** в обоих финальных прогонах 1 MiB range-read и 10 MiB extraction
   native были быстрее `pytsk3`; результаты приведены в § 4.2. Вывод ограничен
   этим окружением, этой fixture и uncontrolled OS page cache — его нельзя
   переносить на другие машины без повторных измерений.
2. 10 MiB fixture строится отдельным opt-in режимом через `debugfs` только при
   подготовке тестового образа. Production writer и read-only benchmark path
   этим инструментом не записывают данные.
3. Оптимизации чтения сохраняют integrity gates: direct/single-indirect
   pointers bulk-unpack'ятся в пределах запрошенного bounded range; contiguous
   physical runs валидируются целиком по block-group metadata и allocation
   bitmap bitmask перед чтением; sparse/noncontiguous runs проходят прежний
   checked path. Для полностью contiguous range возвращаются непосредственно
   проверенные bytes, без промежуточных копий.
4. Harness скрывает сравнение производительности при любом расхождении размера
   или SHA-256. В обоих финальных прогонах содержимое совпало, поэтому ratio
   относятся к одинаковой полезной нагрузке.

## 6. Как повторить сравнение на другой машине

pytsk3 на этой платформе установился готовым manylinux wheel'ом
(`pip install pytsk3`, версия 20260715), отдельная сборка не понадобилась.
Если на целевой машине wheel'а нет (нет компилятора/заголовков libtsk):

1. Установите системные зависимости: `libtsk-dev` (или эквивалент) и
   компилятор (`build-essential` / VS Build Tools).
2. `pip install pytsk3` — соберёт binding из исходников (займёт несколько
   минут; нужен совместимый Python ≤ 3.12 на момент написания).
3. Либо используйте prebuilt-окружение (например, образ с установленным
   sleuthkit/pytsk3) и запустите `scripts/run_vfs_benchmarks.sh` в нём.
4. Если pytsk3 недоступен, запустите `--baseline native`: harness запишет
   `pytsk3_error` с точной причиной, а числовое сравнение публиковать нельзя
   — тогда приложите к результату вывод `pip install pytsk3` и причину отказа.
