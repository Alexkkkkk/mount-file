<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->

[![CI](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/ci.yml)
[![Quality](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/quality.yml)
[![Security](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/security.yml)
[![Docs](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docs.yml)
[![Docker](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml/badge.svg)](https://github.com/Alexkkkkk/mount-file/actions/workflows/docker.yml)
[![Latest release](https://img.shields.io/github/v/release/Alexkkkkk/mount-file?sort=semver&label=release)](https://github.com/Alexkkkkk/mount-file/releases/latest)
[![License: MIT](https://img.shields.io/github/license/Alexkkkkk/mount-file?label=license&color=yellow)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

# Mount File

**Mount File** — desktop file manager and DFIR toolkit for raw disk images,
NAND/eMMC dumps and filesystem analysis. It can inspect partitions, browse and
extract files, run bounded read-only analysis, and perform limited native writes
only for explicitly validated filesystem profiles.

The project works on image files. It does **not** flash data back to physical
devices and does not claim complete filesystem or forensic-tool parity.

> **English:** Mount File is a cross-platform desktop application for browsing,
> extracting and analyzing raw disk images and NAND/eMMC dumps. Native readers
> and writers are deliberately bounded and fail closed when a profile is dirty,
> malformed, unsupported or ambiguous.

| | |
|---|---|
| Current package version | `1.86.0` on `main`; see [Releases](https://github.com/Alexkkkkk/mount-file/releases) for packaged binaries |
| Documentation | [alexkkkkk.github.io/mount-file](https://alexkkkkk.github.io/mount-file/) |
| Repository | [github.com/Alexkkkkk/mount-file](https://github.com/Alexkkkkk/mount-file) |
| License | [MIT](LICENSE) |
| Platforms | Windows 10/11, Linux and macOS |

<!-- MOUNTFILE-AUTO-STATUS:START -->
## Generated project status

This block is generated from the canonical status data in this repository. Package version: `1.86.0`.

Validated automation: checker acceptance, recovery acceptance, benchmark artifact validation.

Canonical status data: `docs/generated/capability-matrix.json`, `checker-matrix.json`, `recovery-matrix.json` and `product-status.json`.

Capability profiles currently declared: capability matrix available.

This generated summary is informational: unsupported, dirty, ambiguous and recovery-required profiles remain fail-closed; benchmark and fixture evidence do not claim full filesystem parity or power-loss safety.
<!-- MOUNTFILE-AUTO-STATUS:END -->

## What is supported today

Mount File has two related layers:

- a Qt desktop application in `main.py`;
- a package CLI available as `python -m mountfile` and, after installation,
  as `mountfile`.

The native filesystem layer supports **validated bounded profiles**, not every
layout accepted by a filesystem specification:

| Family | Native read | Native write | Scope |
|---|---:|---:|---|
| FAT12/16/32 | Yes | Limited | Bounded geometry and metadata; FAT12/16 use fixed-root constraints |
| exFAT | Yes | Limited | One-FAT profile with validated metadata and volume state |
| ext2 | Yes | Limited | Allowlisted metadata and bounded direct/indirect capacity |
| ext3/ext4 | Read-only | No | Conservative profiles; journal/recovery and unsupported features fail closed |
| NTFS | Read-only | No | Bounded reader; native writer is intentionally disabled |

Native ext3/ext4 and NTFS writes are unsupported; power-loss safety is
unsupported. Limited writes are available only through the explicitly enabled
bounded profiles above. Backup, rollback and benchmark evidence do not imply
power-loss safety or full filesystem parity.

The versioned machine-readable matrix is the source of truth for operations,
state gates and refusal codes:

```bash
python -m mountfile capabilities
```

A selected image can be checked without mutation:

```bash
python -m mountfile status dump.img
```

For the detailed status and remaining acceptance work, see
[`docs/issue-182-status.md`](docs/issue-182-status.md) and
[issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).

For self-learning layer controls, explicit feedback, privacy boundaries and
the OLMA code-repair gate, see [`docs/self-learning-layers.md`](docs/self-learning-layers.md).

## What is not ready yet

The current release is functional, but several production-readiness items remain
open. These are tracked as separate GitHub issues rather than being implied as
completed by the capability matrix:

| Area | Status | Tracking issue |
|---|---|---|
| Native ext3/ext4 writer with JBD2 transaction and recovery gates | Not ready; read-only | [#385](https://github.com/Alexkkkkk/mount-file/issues/385) |
| Native NTFS writer with `$LogFile` recovery | Not ready; read-only | [#386](https://github.com/Alexkkkkk/mount-file/issues/386) |
| Writer acceptance: failure injection, process-kill and quarantine tests | Not ready | [#387](https://github.com/Alexkkkkk/mount-file/issues/387) |
| Unified native VFS/refusal parity across profiles | In progress | [#388](https://github.com/Alexkkkkk/mount-file/issues/388) |
| Cross-platform filesystem fixtures and independent checker CI | In progress | [#389](https://github.com/Alexkkkkk/mount-file/issues/389) |
| Reproducible benchmark matrix and synchronized product status | In progress | [#390](https://github.com/Alexkkkkk/mount-file/issues/390) |

The production-readiness epic is [#384](https://github.com/Alexkkkkk/mount-file/issues/384).
The DFIR/AI roadmap remains tracked in [#87](https://github.com/Alexkkkkk/mount-file/issues/87).
Until these issues are closed, do not interpret limited native writes, backups or
read-only journal overlays as universal filesystem support or power-loss safety.

## Quick start

### Install from source

Python 3.10 or newer is required. The basic package installs the Qt application
and native bounded readers:

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install .
python main.py
```

You can open an image directly:

```bash
python main.py path/to/image.img
```

The project installer can prepare the Python environment and optional system
components:

```bash
python install_mountfile.py
python install_mountfile.py --minimal
python install_mountfile.py --dry-run
python install_mountfile.py --dev
```

Use `--minimal` for the smallest native profile. Use `--no-sys` to skip system
package installation. The installer does not install AI models, API keys or
external forensic databases. Full installer options are documented in
[`docs/install.md`](docs/install.md).

### Install optional features

```bash
python -m pip install '.[api]'       # FastAPI/uvicorn/httpx
python -m pip install '.[ai]'        # MCP and AI backends
python -m pip install '.[dfir]'      # dateutil and YARA support
python -m pip install '.[legacy-tsk]'# explicit pytsk3 compatibility fallback
python -m pip install '.[dev]'       # tests, formatters and build tools
python -m pip install '.[images]'    # Pillow-based image checks
python -m pip install '.[fuzz]'      # Linux fuzzing tools
python -m pip install -r requirements-all.txt  # all optional features and contributor tools
```

`pytsk3` is not an automatic fallback for a recognized but unsupported or dirty
native profile. Enable legacy compatibility explicitly when it is appropriate
for the investigation.

### Download a packaged build

Stable Windows, Linux and macOS artifacts are published on
[GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases). Release
artifacts include SHA-256 files. Always verify the checksum and work on a copy
of the evidence image.

## GUI

Run:

```bash
python main.py                 # open the desktop application
python main.py dump.img        # open an image immediately
python main.py --version       # print the version
python main.py --diag          # diagnose the local environment
```

The GUI provides a two-panel file-manager workflow, partition and volume
selection, directory browsing, extraction, a HEX editor, image summary,
autopick for ambiguous NAND/eMMC dumps, optional AI analysis and bounded write
operations where the selected profile allows them.

Writing changes the image file. Before using **Import**, **New folder**,
**Rename**, **Delete** or HEX save:

1. make an independent copy of the image;
2. verify the selected volume and offset;
3. keep sufficient free space for the configured backup;
4. confirm that the operation belongs to the active native profile.

The GUI creates a backup before its first mutation in a session. This backup is
a safety aid, not a filesystem journal and not a power-loss guarantee.

Useful shortcuts include `Tab` to switch panels, `Space` to select an entry,
`F5`/`F6` to copy or move, `F7` to create a directory, `F8` to delete,
`Ctrl+O` to open an image, `Ctrl+R` to refresh and `Ctrl+Shift+X` to open the
command center. The complete GUI guide is in
[`docs/gui.md`](docs/gui.md).

## Command line

The package CLI does not require Qt:

```bash
python -m mountfile --help
python -m mountfile volumes dump.img
python -m mountfile info dump.img
python -m mountfile capabilities
python -m mountfile status dump.img
python -m mountfile ls dump.img /
python -m mountfile stat dump.img /etc/hosts
python -m mountfile get dump.img /photos -o ./evidence/photos
```

After `pip install .`, the equivalent installed command is `mountfile`.
`python main.py <same command>` is also supported by the desktop entry point.

### Read and evidence commands

| Command | Purpose | Changes source image |
|---|---|---:|
| `volumes` | list detected partitions and filesystem volumes | No |
| `info` | print image and volume summary | No |
| `capabilities` | print the versioned native profile matrix | No |
| `status` | report selected backend, profile and typed refusal status | No |
| `ls`, `stat` | list or inspect a filesystem path | No |
| `read-range` | read a bounded range from a regular file | No |
| `get` | extract a file or directory | No |
| `autopick` | choose a validated volume automatically | No |
| `hash` | calculate a SHA-256 identity hash | No |
| `evidence-manifest` | create a separate JSON manifest for one evidence file | No |
| `grep`, `search` | search raw bytes or filesystem files | No |
| `timeline`, `timeline-export` | inspect or normalize caller-supplied timeline CSV | No |
| `timeline-build`, `timeline-query` | build/query a filesystem-aware timeline | Output only |
| `yara`, `yara-scan` | run YARA rules against permitted targets | No |
| `timestomp`, `timestomp-scan` | inspect timestamp anomalies | No |
| `unallocated-scan` | scan explicitly supplied unallocated ranges | No |
| `slack`, `slack-extract` | inspect or extract explicitly resolved file slack | Output only |
| `live-diff` | compare images and write a Markdown report | Output only |

Filesystem-aware DFIR commands may require `pytsk3`/`libtsk`; YARA commands
also require `yara-python`. They do not replace a validated allocation map,
carving workflow or chain-of-custody system.

### Limited write commands

Write commands require `--yes` and refuse unsupported profiles instead of
silently falling back:

```bash
python -m mountfile put dump.img ./hello.txt --dest /docs --backup --yes
python -m mountfile mkdir dump.img /docs/new --backup --yes
python -m mountfile mv dump.img /hello.txt renamed.txt --backup --yes
python -m mountfile ln dump.img /hello.txt /docs/hello-link.txt --yes
python -m mountfile rm dump.img /docs/new --recursive --backup --yes
```

The `ln` hard-link operation is available only for the validated native ext2
write profile; other filesystems refuse it through the capability gate.

`--backup` creates a neighboring `<image>.bak`; it is not a transaction. The
usual exit codes are `0` for success, `1` for an error, `2` when confirmation is
missing and `130` for interruption.

### RT809H and NAND/eMMC helpers

```bash
python -m mountfile decode dump.bin
python -m mountfile split dump.bin udev.txt -o ./parts
python -m mountfile autopick dump.bin --json > candidates.json
python -m mountfile autopick dump.bin --hint system --extract ./rootfs
```

`autopick` combines MBR/GPT, `udev` metadata and filesystem signatures, then
verifies candidates by opening them. `--no-verify` is faster but less safe.
Raw NAND OOB/ECC interleaving is outside this tool's scope.

For the complete option reference, see [`docs/cli.md`](docs/cli.md).

## Standalone mount helper

`mountfile.py` is a separate single-file utility for mounting supported image
containers through the host operating system. It is not the same as the native
reader/writer path described above:

```bash
python mountfile.py                  # GUI
python mountfile.py dump.img --to /mnt/x
python mountfile.py dump.img --open
python mountfile.py --list
python mountfile.py --unmount /mnt/x
```

It uses platform tools such as `udisksctl`/`mount` on Linux, `hdiutil` on macOS
and PowerShell disk-image commands on Windows. Availability and permissions
depend on the host OS.

## Optional AI, API and automation layers

These are opt-in components and are not required for offline image browsing:

- `.[api]` provides the authenticated REST API. Its capability endpoint is
  documented in [`docs/api.md`](docs/api.md).
- `.[ai]` provides MCP/AI helpers and local/OpenAI-compatible backends. See
  [`docs/ai.md`](docs/ai.md).
- OLMA provides project self-analysis and reports; it is an engineering tool,
  not a forensic recovery guarantee.
- `mountfile_all.py` runs a broad local smoke flow; it is useful for
  development and CI, not a replacement for independent filesystem checkers.

Network access is opt-in for update checks, AI backends and API integrations.
The native reader and bounded writer paths operate locally on the selected image.

## Safety boundaries

Mount File intentionally fails closed in these situations:

- selected extents or metadata exceed image bounds;
- a recognized filesystem is dirty, malformed, ambiguous or outside its
  allowlisted feature profile;
- a requested operation is not present in the selected profile's capability
  matrix;
- a legacy fallback was not explicitly enabled;
- a backup or staged mutation cannot be completed safely.

The project does **not** currently claim:

- full filesystem specification parity;
- native ext3/ext4 or NTFS writing;
- complete JBD2 or NTFS journal recovery;
- universal crash/power-loss atomicity;
- a complete cross-platform external-checker matrix;
- forensic acquisition, chain-of-custody or legal admissibility.

The full acceptance status and roadmap are tracked in
[`docs/issue-182-status.md`](docs/issue-182-status.md),
[`docs/issue-182-full-filesystem-parity-task.md`](docs/issue-182-full-filesystem-parity-task.md)
and [issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).

## Development

```bash
python -m pip install -e '.[dev]'
pre-commit install
make test
make lint
make lint-correctness
make fmt
```

Useful targets:

| Command | Purpose |
|---|---|
| `make test` | run the pytest suite |
| `make test-standalone` | test the single-file mount helper |
| `make lint` | run the CI syntax/error lint gate |
| `make lint-correctness` | run repository-wide correctness rules |
| `make fmt` | apply Ruff, isort and Black |
| `make sample` | build sample images when `dosfstools` and `mtools` are available |
| `make all-in-one` | run the broad no-GUI smoke flow |
| `make smoke-images` | end-to-end CLI and GUI checks on freshly built images, verified by `mtools`, `fsck.vfat`, `fsck.exfat` and `e2fsck` |
| `make exe` | build the main PyInstaller binary |
| `make mountfile-exe` | build the standalone binary |

CI covers Linux, Windows and macOS across the supported Python versions, with
additional security, dependency, documentation and build checks. Optional real-
image tests may be skipped locally when tools such as `pytsk3`, `dosfstools`,
`exfatprogs`, `ntfs-3g`, YARA or PowerShell are unavailable.

See [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`docs/structure.md`](docs/structure.md),
[`docs/quality.md`](docs/quality.md) and [`docs/sync.md`](docs/sync.md) before
opening a pull request.

Проверка исправленных пользовательских сценариев, команды повторного тестирования
и оставшиеся ограничения описаны в
[`docs/functional-validation.md`](docs/functional-validation.md).

## Repository map

```text
main.py                 Qt application and CLI bridge
mountfile.py            standalone mount helper
mountfile/cli.py        package CLI
mountfile/vfs.py        native/compatibility VFS routing
mountfile/capabilities.py versioned capability/refusal matrix
mountfile/*_reader.py   bounded filesystem readers
mountfile/*_writer.py   bounded native writer implementations
mountfile/extract.py    containment-safe extraction
mountfile/dfir*.py      DFIR commands and analysis helpers
mountfile/ai/           optional AI/MCP layer
mountfile/olma/         optional engineering self-analysis
scripts/                build, sample and release helpers
tools/                  maintenance utilities
tests/                  pytest suite
docs/                   user, API, quality and roadmap documentation
```

## Contributing and security

Fork the repository, create a focused branch, add tests for behavior changes and
open a pull request using [Conventional Commits](https://www.conventionalcommits.org/).
Do not include real evidence images, credentials, tokens or private dumps in
issues or pull requests.

Report vulnerabilities privately according to [SECURITY.md](SECURITY.md), not
through a public issue. Community standards are in
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Support the project

If Mount File is useful, a GitHub star, reproducible bug report, documentation
fix or code contribution is welcome. Optional donation information is maintained
in [`docs/donations.md`](docs/donations.md).

The project donation wallet is **TON**: `UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv`.
Verify the same address in [TON Viewer](https://tonviewer.com/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv)
or use [the TON transfer link](ton://transfer/UQCZ10Eh8IJ5sFC6JKn-YralMF45Bk5fkNT6SqTHSIRGxfQv?text=MountFile%20donation).
Never share a seed phrase or private key; see [`docs/donations.md`](docs/donations.md)
for the full safety note.

## License

Mount File is distributed under the [MIT License](LICENSE).

## Copilot coding agent: MCP servers

Free, no-API-key MCP servers for the [GitHub Copilot coding agent](https://docs.github.com/en/enterprise-cloud@latest/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers)
are pre-configured in [`.github/copilot-mcp-config.json`](.github/copilot-mcp-config.json)
(GitHub and Playwright MCP servers are built in and are not duplicated).

Setup: open **Settings → Copilot → MCP servers** in this repository, paste the
JSON from that file into the "MCP configuration" field and click
**Save MCP configuration**. No secrets are required — every server below works
without an API key.

Added no-key reasoning helpers (`sequential-thinking`, `memory`), the Context7
docs server, and switched `cloudflare_docs` to its current Streamable HTTP
endpoint (`https://docs.mcp.cloudflare.com/mcp`; the legacy `/sse` URL is
retired).
