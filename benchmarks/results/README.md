# Issue #182: preliminary ext2 benchmark (ARM64)

This is a **single-host, preliminary measurement**, not a cross-platform performance claim or full acceptance of issue #182. Raw schema-v2 output is in [`issue-182-ext2-arm64-2026-10-28.json`](issue-182-ext2-arm64-2026-10-28.json). The 64 MiB ext2 fixture itself is not committed; its SHA-256 is recorded in the JSON.

## Method

Both providers read the same read-only image extent (`offset=0`, `volume_size=67,108,864`). The workload was `/bench/tiny.txt`, `/bench/1m.bin`, and `/bench/8m.bin`: one small text file, deterministic 1 MiB and 8 MiB payloads, one directory listing, metadata lookup, range reads capped at 1 MiB per file, and full extraction to temporary directories. The ext2 image was created with e2fsprogs using `mke2fs -q -F -t ext2 -b 4096 -O none,filetype,sparse_super,large_file`; the three files were added with `debugfs` only during fixture construction. Neither benchmark backend modifies the image.

The harness ran two warm-up iterations and five measured iterations per backend with `--cache-state unspecified`. It does not flush, inspect, or control the host page cache, so this run must not be described as cold-cache or as a controlled warm-cache comparison. The JSON captures the image hash, extent, workloads, raw samples, and per-operation summaries. Native and `pytsk3` file sizes and SHA-256 hashes matched for every workload file (`content_equivalent: true`).

## Observed result

Environment: Linux ARM64 (`aarch64`), kernel `6.18.38+`, Python `3.12.3`, `pytsk3 20260715`. For the median full iteration (provider-open time excluded), native took **0.05638 s** and `pytsk3` took **0.13862 s**: a **59.3% lower** median iteration time for native on this run.

| Operation median | Native | `pytsk3` |
|---|---:|---:|
| `list_dir` | 0.000186 s | 0.008442 s |
| `stat` | 0.000115 s | 0.008386 s |
| 1 MiB-bounded `read_range` | 0.003142 s | 0.000305 s |
| full extraction | 0.004325 s | 0.001398 s |

The result is workload-specific: native was faster for listing and metadata lookup, while `pytsk3` was faster for bounded range reads and full extraction in this sample. The synthetic, small file set, uncontrolled cache, single ARM64 host, and five repetitions limit generalization. Do not use this report alone to claim broad performance superiority or to close the benchmark acceptance criterion. Repeat on representative real-world images and supported OS/Python combinations with a documented cache procedure before making release-level claims.

## Reproduction outline

The raw image is not checked in. To recreate the fixture, create a 64 MiB image with the `mke2fs` command above, generate the 1 MiB and 8 MiB deterministic payloads, create `/bench`, and add the three files with `debugfs -w -R 'write <host-file> /bench/<name>'`. The payload byte at index `i` was generated as `(i * 73 + (i >> 8) * 19 + 17) % 256` for `1m.bin`, and `(i * 131 + (i >> 11) * 37 + 53) % 256` for `8m.bin`; `tiny.txt` contains `MountFile benchmark fixture\n`. Filesystem metadata timestamps and UUID may differ when rebuilding, so compare the resulting image hash with the recorded run rather than assuming byte-identical regeneration.

From the repository root, install the optional baseline (`pip install 'mountfile[legacy-tsk]'`) and run:

```bash
PYTHONPATH=. python benchmarks/bench_vfs.py native-ext2-benchmark.img \
  --offset 0 --volume-size 67108864 \
  --baseline both --repeat 5 --warmup 2 --cache-state unspecified \
  --hash-image --json-out benchmarks/results/benchmark.json \
  /bench/tiny.txt /bench/1m.bin /bench/8m.bin
```
