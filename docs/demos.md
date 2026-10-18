# Reproducible demos

The repository includes short [asciinema recordings and their runners](https://github.com/Alexkkkkk/mount-file/tree/main/examples/demos).
Replay a checked-in recording with:

```bash
asciinema play examples/demos/timeline.cast
asciinema play examples/demos/recovery.cast
asciinema play examples/demos/ai-copilot.cast
```

Regenerate the first and third scenarios from the source tree:

```bash
bash examples/demos/timeline_demo.sh
python examples/demos/ai_copilot_demo.py
```

## Timeline

`timeline_demo.sh` imports a tiny, synthetic CSV into a temporary SQLite database
with `mountfile_dfir.py`, then queries it. It demonstrates the CSV-to-SQLite
workflow only; it does not collect timestamps from a filesystem image.

## Deleted-entry recovery

The recovery demo requires `asciinema` to record and `dosfstools`, `mtools`,
`pytsk3`, and `pydantic` to run. Create/activate a Python environment, install
`pytsk3` and `pydantic`, and then run:

```bash
bash examples/demos/recovery_demo.sh
```

It creates a disposable FAT32 image, deletes a synthetic text file, enumerates
the deleted directory entry in a read-only process, then recovers the selected
inode in a separate process to a temporary output file. The demo is not file
carving, does not establish full file integrity, and must not be used as a
recovery command for an original evidence image.

## Forensic copilot

`ai_copilot_demo.py` passes fixed, already-derived metadata to
`mountfile.ai.copilot.build_summary`. It opens no image, reads no evidence bytes,
and uses neither a network connection nor a language model. It demonstrates the
summary builder's interface, not an automatic GUI investigation.
