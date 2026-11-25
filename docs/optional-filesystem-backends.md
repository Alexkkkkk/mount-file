# Optional filesystem backends

MountFile keeps filesystem backends optional and fail-closed. An installed
library is not automatically an acceptance oracle, and a write-capable system
tool is not automatically the native replay implementation.

## Python extras

The supported optional forensic extra is:

```bash
uv sync --extra forensics
# or: pip install 'mountfile[forensics]'
```

It installs `libfsntfs-python`, the LGPL-3.0-or-later Python binding for
`libfsntfs`. The binding is read-only and is intended as an independent checker
candidate. It does not implement NTFS `$LogFile` redo/undo replay, MFT
allocation, or product writes.

`pyext2fs` is not added as a dependency. It is a source-built wrapper that
requires `libext2fs` and `libnbd`; MountFile already has a bounded `ctypes`
binding to the system `libext2fs` in `mountfile/ext4_native_writer.py`, followed
by journal gates and `e2fsck -fn` verification. Adding a second wrapper would
not add replay evidence.

`dfir_ntfs` is intentionally not a normal dependency. Upstream is GPL-3.0 and
its parser is read-only. MountFile keeps it behind the quarantined
`mountfile.experimental.dfir_ntfs_adapter` boundary; install it externally only
for experiments and do not use it as an authoritative checker.

## System tools

The NTFS helpers are system packages, not Python wheels. On Debian/Ubuntu,
install `ntfs-3g` for `ntfsfix`, `ntfscp`, and `ntfscat`. The native writer uses
these only on disposable copies, performs read-back, and refuses publication on
mismatch. `ntfsfix` is secondary evidence and cannot establish Windows
`chkdsk` parity. `ntfs-3g` is GPL-2.0-or-later and is not claimed as the native
replay engine.

For an auditable inventory of what is actually available, run:

```bash
python scripts/backend_inventory.py --pretty
```

The output reports availability, license, and bounded capability separately.
It never upgrades `read-only`, `secondary`, `unavailable`, or
`not_claimed` outcomes to a product support claim.
