# VFS benchmark

`bench_vfs.py` benchmarks `list_dir`, file-size/stat, bounded range reads, and full-file extraction. It opens the source image read-only and requires the exact selected volume extent. Extraction always goes to a temporary directory and verifies the number of bytes written. It never changes the image or runs filesystem repair.

Install the optional `pytsk3` dependency to compare against that explicit fallback. Use the same image, partition extent, ordered path list, cache state, runtime, and command options for both backends:

```bash
PYTHONPATH=. python benchmarks/bench_vfs.py image.ext4 \
  --offset 0 --volume-size 67108864 \
  --baseline both --repeat 7 --warmup 2 --cache-state warm \
  --hash-image --json-out results/image-ext4.json \
  /etc/hostname /var/log/example.log
```

The JSON schema records image path, image size and optional SHA-256, exact extent, filesystem profile fields exposed by each provider, Python/platform versions, workload paths, file sizes, warm-up/repetition counts, and per-operation timings. Backend summaries include sample count, minimum, median, p95 and maximum. Raw per-iteration counts, bytes read and bytes extracted are retained so runs can be audited and aggregated later. The shared `stat` adapter may traverse `list_dir` at each path component; the report names this implementation so the stat metric is not mistaken for a backend-native inode syscall. If `--hash-image` is selected, hashing happens after timed runs to avoid priming the image cache before measurement.

`--cache-state` is a label only. The harness does not flush or control the operating-system page cache. Do not label a run `warm` or compare cache conditions unless the host procedure actually establishes them. To get comparable results, document that procedure alongside the JSON artifact. The harness does not claim a performance target has passed merely because a run completed; compare the native and `pytsk3` medians for the same workload and report the result with the artifact.

A provider-open failure is recorded as `<backend>_error`; it is never silently replaced by another parser. The native benchmark rejects a `pytsk3` fallback and reports it as an error. The selected offset and volume size are range-checked against the image before the run. Absolute normalized VFS paths are required.
