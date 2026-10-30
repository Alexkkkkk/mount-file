# Issue #182: preliminary ext2 benchmark (ARM64)

This is a **single-host, preliminary measurement**, not a cross-platform performance claim or full acceptance of issue #182. Raw schema-v2 output is in [`issue-182-ext2-arm64-2026-10-28.json`](issue-182-ext2-arm64-2026-10-28.json). The 64 MiB ext2 fixture itself is not committed; its SHA-256 is recorded in the JSON. This historical run predates the harness's alternating backend order; use a fresh run for order-balanced comparisons.

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


## Alternating-order ext2 10 MiB run (2026-10-29)

A second, distinct synthetic run is stored in [`ext2-10mib-aarch64.json`](ext2-10mib-aarch64.json). It uses one 10 MiB file in a 64 MiB ext2 image, 4 KiB blocks, and `dir_index` disabled to match the native reader's allowlist. The run used CPython 3.12.3 on Linux 6.18.38+ aarch64, seven measured iterations after two warmups, with alternating backend order. The source image was temporary; the result records its SHA-256. The output hash and file size match between native and `pytsk3` for the file in this workload.

| Operation median | Native | `pytsk3` |
|---|---:|---:|
| `list_dir` | 0.000154 s | 0.007881 s |
| `stat` | 0.000104 s | 0.007831 s |
| 1 MiB `read_range` | 0.000492 s | 0.008108 s |
| full extraction, 10 MiB | 0.012592 s | 0.094690 s |
| combined iteration | 0.023445 s | 0.129058 s |

The harness reports a `warm` cache label, but it does not flush or control the host page cache. This small synthetic workload is not representative enough to claim the issue's performance target is satisfied. It does not cover cold-cache access, directories with many entries, several file sizes, write workloads, other ext profiles, or other operating systems. The exact fixture and command are reproducible with [`run_ext2_smoke.sh`](../run_ext2_smoke.sh), which requires e2fsprogs and the optional `pytsk3` package.


## Mixed-size and many-entry run (2026-10-29)

[`ext2-matrix-aarch64.json`](ext2-matrix-aarch64.json) records a second workload on the same ARM64 host. It uses a 128 MiB clean ext2 image (4 KiB blocks, `dir_index` disabled), three differently sized files (text, 1 MiB, 8 MiB), and 16 small entries in a second directory. Seven measured iterations follow two warmups; backend order alternates. All 19 paths had matching sizes and SHA-256 between native and `pytsk3`.

| Operation median per iteration | Native | `pytsk3` |
|---|---:|---:|
| `list_dir` | 0.000232 s | 0.004032 s |
| `stat` | 0.000257 s | 0.008048 s |
| 1 MiB-bounded `read_range` per path | 0.000106 s | 0.000040 s |
| extraction of all 19 files | 0.000625 s | 0.000695 s |
| combined iteration | 0.040292 s | 0.272564 s |

These figures are specific to this generated fixture and warm-cache run. `pytsk3` was faster on bounded range reads; extraction was nearly tied. The combined iteration includes the harness's per-path work and must not be treated as a universal throughput measure. To reproduce the fixture and JSON, run [`run_ext2_matrix.sh`](../run_ext2_matrix.sh). This is still a small, synthetic, single-host sample and does not complete the broader benchmark acceptance criterion.


## Allowlisted ext4 mixed-size run (2026-10-29)

[`ext4-matrix-aarch64.json`](ext4-matrix-aarch64.json) records a 128 MiB ext4 image with 4 KiB blocks and only the generated features `extent,filetype,sparse_super,large_file`; journal, metadata checksums, 64-bit descriptors and flex_bg are absent. The image profile opened as `native-ext4-ro`. The fixture contains the same 19-path mix as the ext2 matrix: a text file, 1 MiB and 8 MiB files, and 16 small files in a second directory. All 19 sizes and SHA-256 digests matched between native and `pytsk3`.

The generated fixture passes `e2fsck -fn` before measurement. The run used CPython 3.12.3 on Linux 6.18.38+ aarch64, seven measured iterations after two warmups, with alternating backend order and the extraction stream's 1 MiB bounded chunk. The median full iteration was **0.03216 s native** vs **0.03099 s `pytsk3`**, a **3.8%** regression—within the issue's 20% target for this profile and this run.

| Operation (median total per iteration) | Native | `pytsk3` |
|---|---:|---:|
| `list_dir` | 0.000459 s | 0.000229 s |
| `stat` | 0.001928 s | 0.003943 s |
| 1 MiB-bounded `read_range` per path | 0.001641 s | 0.001206 s |
| extraction of all 19 files | 0.017637 s | 0.014568 s |

This is a synthetic, single-host result for one deliberately restricted ext4 profile. It does not establish the target for metadata-checksummed/default ext4, other storage devices, cold caches, or other operating systems. Reproduce it with [`run_ext4_matrix.sh`](../run_ext4_matrix.sh); e2fsprogs and the optional `pytsk3` extra are required. The benchmark's cache label is descriptive; the harness does not flush or control the OS page cache.
