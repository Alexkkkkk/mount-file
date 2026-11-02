# Issue #182 — воспроизводимый benchmark report (Stage G)

## Статус

В репозитории уже есть benchmark harness (`benchmarks/bench_vfs.py`), генератор детерминированного ext2 fixture (`scripts/make_bench_ext2_image.py`) и тесты harness (`tests/test_benchmarks.py`). Эта страница фиксирует воспроизводимую методику и область выводов.

Отдельный smoke-контракт добавлен в `mountfile/native_parity.py` и покрывает FAT12/16/32, exFAT, ext2, clean ext3 и ext4 synthetic fixtures в `tests/test_native_parity.py`. NTFS здесь намеренно не включён: текущий VFS маршрут открывает ограниченный NTFS metadata view, а не общий file-content provider для полного `read-range`/`extract` контракта.

**Измерения для этой версии отчёта не запускались; числовых результатов и performance-claims здесь нет.** Stage G остаётся незавершённым до публикации JSON-результата вместе с описанием фактической машины и cache state. Не заполняйте таблицу оценками или результатами тестов.

| Workload | Native | pytsk3 | Сравнение |
|---|---:|---:|---|
| `list_dir`, `stat`, `read_file_range`, `extract_file` | ожидает запуска | ожидает запуска | ожидает проверенной эквивалентности содержимого |

## Fixture и команды

Для эталонного прогона используются целый ext2 image без partition table, 32 MiB, block size 4096 bytes и deterministic files, созданные генератором. Он формирует `/bench/` и `/bench/many/`; пути, размеры данных и содержимое заданы в `scripts/make_bench_ext2_image.py`. При сравнении `pytsk3` выбранный filesystem extent должен охватывать весь образ: legacy adapter намеренно отказывает для неподдерживаемого partition-sized ограниченного extent.

```bash
python scripts/make_bench_ext2_image.py --output /tmp/issue-182-bench.ext2
python benchmarks/bench_vfs.py \
  /tmp/issue-182-bench.ext2 \
  /bench/tiny_1k.dat \
  /bench/small_16k.dat \
  /bench/small_64k.dat \
  /bench/medium_512k.dat \
  /bench/large_1m.dat \
  /bench/large_4m.dat \
  /bench/many/file_000.dat \
  /bench/many/file_008.dat \
  /bench/many/file_016.dat \
  --offset 0 \
  --volume-size 33554432 \
  --baseline both \
  --repeat 5 \
  --warmup 1 \
  --cache-state unspecified \
  --hash-image \
  --json-out /tmp/issue-182-bench-results.json
```

The JSON records the image SHA-256, selected extent, filesystem profile (including block size where the provider exposes it), file sizes and SHA-256 digests, Python/platform data, pytsk3 package version (or `null` if unavailable), source Git revision (or `null` outside a Git checkout), backend names, per-backend open time, individual operation samples, warm-up and repeat counts, and paired execution order. Native and pytsk3 runs are interleaved to reduce order bias. The harness verifies content equivalence before reporting a relative performance comparison and labels the 20% target as descriptive for that run.

The harness does not manipulate or flush the operating-system page cache. Record the actual host cache condition alongside any published results; `unspecified` is not a cold-cache claim. Provider open time is reported separately from workload iteration time. The benchmark is read-only with respect to the source image; extraction targets temporary directories.

## Publishing results

When a measurement is run, preserve the raw JSON artifact and add a short summary here containing the actual date, OS/kernel, CPU, memory, Python version, pytsk3/libtsk version, image hash, cache procedure/state, and observed per-operation median/p95. Include optional-tool failures as skips/errors rather than treating them as successful measurements. Results apply only to this fixture, extent, runtime and workload; they do not establish performance parity for other filesystem formats or images.
