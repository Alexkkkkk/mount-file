# VFS benchmark

`bench_vfs.py` benchmarks `list`, `stat`, bounded 1 MiB `read-range`, and extraction without writing the source image. The partition extent is mandatory:

```bash
PYTHONPATH=. python benchmarks/bench_vfs.py image.ext4 \
  --offset 0 --volume-size 67108864 \
  --baseline both --warmups 2 --repeat 9 \
  /etc/hostname /var/log/example.log
```

Each baseline is opened independently for every run. Warm-ups are untimed; timed samples are reported individually and summarized by median, median absolute deviation (MAD), minimum and maximum. JSON also records the image size, selected extent, paths, backend name, operation counts, Python version, platform and CPU count. A failure to open a profile is reported as `native_error` rather than being silently downgraded. `--baseline native` and `--baseline pytsk3` allow separate invocations; the explicit baseline requires the optional `pytsk3` installation.

For a meaningful comparison, use the same immutable image bytes, exact offset and volume size, path list, software/dependency versions, warm-up/repeat counts, and host. Record whether the OS page cache is warm or cold. Because `both` runs the native backend before `pytsk3`, it does not control for cache-order effects; for publishable comparisons, run each backend separately and alternate which one is measured first across equivalent trials. Do not compare timings from different machines or cache states as a speedup claim. The harness reports measurements only: it is not a correctness oracle, does not validate mutations, and cannot establish support for formats or profiles that the provider declines.

No benchmark results are asserted here: publish results only after running a documented workload against the same image and environment for both providers. Include the raw JSON, workload/image provenance (without publishing sensitive images), versions, cache protocol, and limitations with any published results. The issue's performance target is subordinate to correctness and integrity; this tooling does not itself establish that target.
