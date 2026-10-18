# Asciinema demos

These are local cast files; nothing is uploaded to asciinema.org.

Replay from the repository root:

```bash
asciinema play examples/demos/timeline.cast
asciinema play examples/demos/recovery.cast
asciinema play examples/demos/ai-copilot.cast
```

Regenerate the recordings with asciinema 2.x:

```bash
asciinema rec -c 'bash examples/demos/timeline_demo.sh' examples/demos/timeline.cast
asciinema rec -c 'bash examples/demos/recovery_demo.sh' examples/demos/recovery.cast
asciinema rec -c 'python3 examples/demos/ai_copilot_demo.py' examples/demos/ai-copilot.cast
```

The recovery script needs `dosfstools`, `mtools`, `pytsk3`, and `pydantic` in
the active Python environment. It creates and deletes a sample file in a
throwaway FAT32 image, scans the deleted directory entry read-only, and writes
the selected inode stream only to a temporary output. It is not a generic file
carver and must not be pointed at a real evidence image.

The timeline demo uses `timeline.csv` as synthetic input; the copilot demo uses
only fixed metadata in memory and makes no network or model call.
