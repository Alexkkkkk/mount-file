# VFS benchmark

`bench_vfs.py` benchmarks `list`, `stat`, bounded 1 MiB `read-range`, and extraction without writing the source image. The partition extent is mandatory:

```bash
PYTHONPATH=. python benchmarks/bench_vfs.py image.ext4 \
  --offset 0 --volume-size 67108864 \
  --baseline both /etc/hostname /var/log/example.log
```

The output is JSON with timings and operation counts for the native provider and, when available, the explicit `pytsk3` baseline. Use identical path lists, image, offset, volume size and cache conditions when comparing runs. A failure to open a profile is reported as `native_error` rather than being silently downgraded.
