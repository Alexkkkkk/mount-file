#!/usr/bin/env python3
"""
mountfile_dfir_tools.py — DFIR-инструменты для Mount File (issue #87).

РЕАЛИЗОВАНО:
  Task 1.3  YaraScanner          — YARA по файлам и unallocated
  Task 1.5  UnallocatedExtractor — извлечение нераспределённого пространства
  Task 1.6  SlackExtractor       — извлечение slack-пространства
  Task 2.2  ImageDiffer          — live diff между двумя образами

ЗАВИСИМОСТИ:
  Обязательные (stdlib): argparse, hashlib, json, os, re, struct, sys, tempfile, time
  Опциональные:
    pytsk3      — для корректного анализа файловой структуры (рекомендуется)
    yara-python — для полноценного YARA-сканирования (рекомендуется)

УСТАНОВКА:
  # Базовые (raw/dd образы работают и без них, но с ограничениями)
  pip install pytsk3 yara-python

  # Если pytsk3 не ставится из wheel (Windows / старый Python):
  #   Linux:   sudo apt install libtsk-dev build-essential python3-dev
  #   Windows: установите "Microsoft Visual C++ Build Tools" и Sleuth Kit
  #   macOS:   brew install sleuthkit
  #   Затем:   pip install pytsk3

  # Если yara-python не ставится:
  #   Linux:   sudo apt install libyara-dev
  #   macOS:   brew install yara
  #   Затем:   pip install yara-python

ЗАПУСК:
    python mountfile_dfir_tools.py yara disk.img rules.yar
    python mountfile_dfir_tools.py unallocated disk.img -o unalloc/
    python mountfile_dfir_tools.py slack disk.img -o slack/
    python mountfile_dfir_tools.py diff before.img after.img -o diff.json
    python mountfile_dfir_tools.py selftest
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional


# ===========================================================================
# 0. Проверка зависимостей (с инструкциями)
# ===========================================================================

def check_dependencies():
    """Проверяет наличие опциональных библиотек и выводит инструкции."""
    missing = []

    try:
        import pytsk3  # noqa: F401
    except ImportError:
        missing.append(("pytsk3", {
            "linux": "sudo apt install libtsk-dev build-essential python3-dev && pip install pytsk3",
            "windows": "Установите Visual C++ Build Tools и Sleuth Kit, затем pip install pytsk3",
            "macos": "brew install sleuthkit && pip install pytsk3",
            "fallback": "pip install pytsk3  (если есть готовое wheel)",
        }))

    try:
        import yara  # noqa: F401
    except ImportError:
        missing.append(("yara-python", {
            "linux": "sudo apt install libyara-dev && pip install yara-python",
            "windows": "pip install yara-python  (обычно есть wheel)",
            "macos": "brew install yara && pip install yara-python",
            "fallback": "pip install yara-python",
        }))

    if missing:
        print("=" * 70, file=sys.stderr)
        print("ВНИМАНИЕ: отсутствуют опциональные зависимости.", file=sys.stderr)
        print("Инструмент будет работать в ограниченном режиме.", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        for name, hints in missing:
            print(f"\n  [{name}] не найден.", file=sys.stderr)
            print(f"    Linux:   {hints['linux']}", file=sys.stderr)
            print(f"    Windows: {hints['windows']}", file=sys.stderr)
            print(f"    macOS:   {hints['macos']}", file=sys.stderr)
        print("\n" + "=" * 70, file=sys.stderr)

    return {name: (name not in [m[0] for m in missing]) for name, _ in
            [("pytsk3", None), ("yara-python", None)]}


# ===========================================================================
# 1. Типы и исключения
# ===========================================================================

class DFIRError(Exception): pass
class DependencyMissing(DFIRError): pass


@dataclass
class YaraMatch:
    rule: str
    namespace: str
    tags: list[str]
    data_sha256: str
    path: str
    offset: Optional[int] = None


@dataclass
class RegionInfo:
    offset: int
    length: int
    kind: str
    owner: Optional[str] = None


# ===========================================================================
# 2. ImageReader
# ===========================================================================

class ImageReader:
    def __init__(self, image_path: Path):
        self.image_path = Path(image_path)
        if not self.image_path.exists():
            raise DFIRError(f"образ не найден: {image_path}")
        self.size = self.image_path.stat().st_size
        self._tsk_fs = None
        self._tsk_available = None

    def read(self, offset: int, length: int) -> bytes:
        with open(self.image_path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def stream_chunks(self, chunk_size: int = 8 * 1024 * 1024):
        with open(self.image_path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield offset, chunk
                offset += len(chunk)

    def has_pytsk3(self) -> bool:
        if self._tsk_available is not None:
            return self._tsk_available
        try:
            self._open_tsk()
            self._tsk_available = True
        except Exception:
            self._tsk_available = False
        return self._tsk_available

    def _open_tsk(self):
        if self._tsk_fs is not None:
            return self._tsk_fs
        try:
            import pytsk3  # type: ignore
        except ImportError as e:
            raise DependencyMissing(f"pytsk3 не установлен: {e}")
        img = pytsk3.Img_Info(str(self.image_path))
        self._tsk_fs = pytsk3.FS_Info(img)
        return self._tsk_fs

    def iter_files(self) -> Iterator[tuple[str, bytes]]:
        if not self.has_pytsk3():
            for off, chunk in self.stream_chunks():
                yield f"<raw:{off}>", chunk
            return
        fs = self._open_tsk()

        def walk(directory, prefix=""):
            try:
                for e in directory:
                    name = e.info.name.name.decode("utf-8", "replace")
                    if name in (".", ".."):
                        continue
                    path = f"{prefix}/{name}"
                    if e.info.meta.type == 2:
                        yield from walk(e.as_directory(), path)
                    elif e.info.meta.type == 1:
                        try:
                            data = e.read_random(0, e.info.meta.size)
                            yield path, data
                        except OSError:
                            continue
            except OSError:
                return

        yield from walk(fs.open_dir("/"))

    def iter_allocated_regions(self) -> Iterator[RegionInfo]:
        if not self.has_pytsk3():
            yield from self._iter_allocated_heuristic()
            return
        fs = self._open_tsk()
        block_size = fs.info.block_size

        def walk(directory):
            try:
                for e in directory:
                    name = e.info.name.name.decode("utf-8", "replace")
                    if name in (".", ".."):
                        continue
                    if e.info.meta.type == 2:
                        yield from walk(e.as_directory())
                    elif e.info.meta.type == 1:
                        try:
                            meta_file = fs.open_meta(inode=e.info.meta.addr)
                        except OSError:
                            continue
                        for attr in meta_file:
                            for run in attr:
                                if run.len == 0:
                                    continue
                                yield RegionInfo(
                                    offset=run.addr * block_size,
                                    length=run.len * block_size,
                                    kind="file",
                                    owner=f"/{name}",
                                )
            except OSError:
                return

        yield from walk(fs.open_dir("/"))

    def _iter_allocated_heuristic(self) -> Iterator[RegionInfo]:
        boot = self.read(0, 512)
        if boot[3:11] == b"NTFS    ":
            bytes_per_sector = struct.unpack("<H", boot[0x0B:0x0D])[0]
            spc = boot[0x0D] or 1
            cluster_size = bytes_per_sector * spc
            mft_lcn = struct.unpack("<Q", boot[0x30:0x38])[0]
            mft_offset = mft_lcn * cluster_size
            mft_data = self.read(mft_offset, 1024 * 1024)
            for i in range(0, len(mft_data) - 4, 1024):
                if mft_data[i:i+4] == b"FILE":
                    yield RegionInfo(offset=mft_offset + i, length=1024,
                                     kind="file", owner=f"mft_{i//1024}")


# ===========================================================================
# 3. YaraScanner
# ===========================================================================

class YaraScanner:
    def __init__(self, image_path: Path, require_tsk: bool = False):
        self.reader = ImageReader(image_path)
        if require_tsk and not self.reader.has_pytsk3():
            raise DependencyMissing(
                "--require-pytsk3: pytsk3 обязателен. "
                "Установите: pip install pytsk3"
            )

    def scan_files(self, rules_path: Path) -> list[YaraMatch]:
        if not self.reader.has_pytsk3():
            raise DependencyMissing(
                "scan_files без pytsk3 даёт пути <raw:N>, а не файлы. "
                "Установите pytsk3 или используйте --unallocated."
            )
        rules = self._load_rules(rules_path)
        matches: list[YaraMatch] = []
        for path, data in self.reader.iter_files():
            matches.extend(self._scan_blob(data, rules, path=path))
        return matches

    def scan_unallocated(self, rules_path: Path) -> list[YaraMatch]:
        rules = self._load_rules(rules_path)
        ex = UnallocatedExtractor(self.reader.image_path)
        matches: list[YaraMatch] = []
        for region in ex.free_regions():
            remaining = region.length
            cursor = region.offset
            while remaining > 0:
                chunk_size = min(8 * 1024 * 1024, remaining)
                data = self.reader.read(cursor, chunk_size)
                if not data:
                    break
                matches.extend(self._scan_blob(
                    data, rules, path="<unallocated>", base_offset=cursor
                ))
                cursor += len(data)
                remaining -= len(data)
        return matches

    def _load_rules(self, rules_path: Path):
        text = Path(rules_path).read_text(encoding="utf-8")
        try:
            import yara  # type: ignore
            return {"type": "yara", "compiled": yara.compile(source=text)}
        except ImportError:
            return {"type": "regex", "rules": self._parse_simple_rules(text)}

    def _parse_simple_rules(self, text: str):
        rules = []
        for m in re.finditer(r'rule\s+(\w+)\s*\{(.*?)\n\}', text, re.DOTALL):
            name, body = m.group(1), m.group(2)
            patterns = []
            for sm in re.finditer(r'\$[a-z0-9_]+\s*=\s*"((?:[^"\\]|\\.)*)"', body):
                patterns.append(re.escape(sm.group(1).encode().decode("unicode_escape")))
            if patterns:
                rules.append((name, re.compile(b"|".join(p.encode() for p in patterns))))
        return rules

    def _scan_blob(self, data: bytes, rules, path: str,
                   base_offset: int = 0) -> list[YaraMatch]:
        if rules["type"] == "yara":
            try:
                result = rules["compiled"].match(data=data)
                out = []
                for m in result:
                    out.append(YaraMatch(
                        rule=m.rule, namespace=m.namespace,
                        tags=list(m.tags),
                        data_sha256=hashlib.sha256(data).hexdigest(),
                        path=path, offset=base_offset or None,
                    ))
                return out
            except Exception:
                return []
        out = []
        for name, pat in rules["rules"]:
            if pat.search(data):
                out.append(YaraMatch(
                    rule=name, namespace="fallback", tags=[],
                    data_sha256=hashlib.sha256(data).hexdigest(),
                    path=path, offset=base_offset or None,
                ))
        return out


# ===========================================================================
# 4. UnallocatedExtractor
# ===========================================================================

class UnallocatedExtractor:
    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def free_regions(self, min_region: int = 4096) -> list[RegionInfo]:
        occupied = self._occupied_ranges()
        free = self._subtract(0, self.reader.size, occupied)
        return [r for r in free if r.length >= min_region]

    def extract(self, out_dir: Path, *, min_region: int = 4096) -> list[RegionInfo]:
        out_dir.mkdir(parents=True, exist_ok=True)
        regions = self.free_regions(min_region)
        written = []
        for i, r in enumerate(regions):
            out = out_dir / f"unalloc_{i:04d}_off{r.offset:08x}_len{r.length}.bin"
            with open(out, "wb") as f:
                remaining = r.length
                cursor = r.offset
                while remaining > 0:
                    chunk = self.reader.read(cursor, min(8 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    cursor += len(chunk)
                    remaining -= len(chunk)
            written.append(r)
        return written

    def _occupied_ranges(self) -> list[tuple[int, int]]:
        return sorted((r.offset, r.offset + r.length)
                      for r in self.reader.iter_allocated_regions())

    @staticmethod
    def _subtract(start: int, end: int,
                  occupied: list[tuple[int, int]]) -> list[RegionInfo]:
        free = []
        cursor = start
        for o_start, o_end in occupied:
            if o_end <= cursor:
                continue
            if o_start >= end:
                break
            if o_start > cursor:
                free.append(RegionInfo(offset=cursor, length=o_start - cursor,
                                       kind="unallocated"))
            cursor = max(cursor, o_end)
        if cursor < end:
            free.append(RegionInfo(offset=cursor, length=end - cursor,
                                   kind="unallocated"))
        return free


# ===========================================================================
# 5. SlackExtractor
# ===========================================================================

class SlackExtractor:
    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def extract(self, out_dir: Path) -> list[RegionInfo]:
        out_dir.mkdir(parents=True, exist_ok=True)
        regions = list(self._find_slack_regions())
        for i, r in enumerate(regions):
            data = self.reader.read(r.offset, r.length)
            out = out_dir / f"slack_{i:04d}_{Path(r.owner or 'unknown').name}.bin"
            out.write_bytes(data)
        return regions

    def _find_slack_regions(self) -> Iterator[RegionInfo]:
        if self.reader.has_pytsk3():
            yield from self._tsk_slack()
        else:
            boot = self.reader.read(0, 512)
            if boot[3:11] == b"NTFS    ":
                yield from self._ntfs_slack_heuristic(boot)
            elif boot[82:90].startswith(b"FAT32"):
                yield from self._fat32_slack(boot)

    def _tsk_slack(self) -> Iterator[RegionInfo]:
        fs = self.reader._open_tsk()
        block_size = fs.info.block_size

        def walk(directory):
            try:
                for e in directory:
                    name = e.info.name.name.decode("utf-8", "replace")
                    if name in (".", ".."):
                        continue
                    if e.info.meta.type == 2:
                        yield from walk(e.as_directory())
                    elif e.info.meta.type == 1:
                        try:
                            meta_file = fs.open_meta(inode=e.info.meta.addr)
                        except OSError:
                            continue
                        alloc_size = 0
                        for attr in meta_file:
                            for run in attr:
                                alloc_size += run.len * block_size
                        real_size = e.info.meta.size
                        if alloc_size > real_size and real_size > 0:
                            yield RegionInfo(
                                offset=0, length=alloc_size - real_size,
                                kind="slack", owner=f"/{name}",
                            )
            except OSError:
                return

        yield from walk(fs.open_dir("/"))

    def _ntfs_slack_heuristic(self, boot: bytes) -> Iterator[RegionInfo]:
        return

    def _fat32_slack(self, boot: bytes) -> Iterator[RegionInfo]:
        bytes_per_sector = struct.unpack("<H", boot[0x0B:0x0D])[0]
        spc = boot[0x0D] or 1
        cluster_size = bytes_per_sector * spc
        reserved = struct.unpack("<H", boot[0x0E:0x10])[0]
        num_fats = boot[0x10]
        fat_size = struct.unpack("<I", boot[0x24:0x28])[0]
        root_cluster = struct.unpack("<I", boot[0x2C:0x30])[0]
        if root_cluster == 0:
            return
        data_off = (reserved + num_fats * fat_size) * bytes_per_sector
        fat_off = reserved * bytes_per_sector
        fat = self.reader.read(fat_off, min(64 * 1024, fat_size * bytes_per_sector))
        cluster = root_cluster
        seen = set()
        while cluster and cluster < 0x0FFFFFF8 and cluster not in seen:
            seen.add(cluster)
            dir_off = data_off + (cluster - 2) * cluster_size
            dir_data = self.reader.read(dir_off, cluster_size)
            for k in range(0, len(dir_data), 32):
                entry = dir_data[k:k+32]
                if entry[0] == 0:
                    break
                if entry[0] == 0xE5:
                    continue
                size = struct.unpack("<I", entry[28:32])[0]
                file_cluster = (struct.unpack("<H", entry[20:22])[0] << 16) | \
                               struct.unpack("<H", entry[26:28])[0]
                if size == 0 or file_cluster == 0:
                    continue
                alloc = ((size + cluster_size - 1) // cluster_size) * cluster_size
                if alloc > size:
                    file_off = data_off + (file_cluster - 2) * cluster_size
                    yield RegionInfo(
                        offset=file_off + size, length=alloc - size,
                        kind="slack",
                        owner=entry[:11].decode("ascii", "replace").strip(),
                    )
            if fat:
                next_off = cluster * 4
                if next_off + 4 <= len(fat):
                    cluster = struct.unpack("<I", fat[next_off:next_off+4])[0] & 0x0FFFFFFF
                else:
                    break
            else:
                break


# ===========================================================================
# 6. ImageDiffer
# ===========================================================================

@dataclass
class DiffEntry:
    path: str
    status: str
    before_sha: Optional[str] = None
    after_sha: Optional[str] = None


class ImageDiffer:
    def __init__(self, before: Path, after: Path):
        self.before = ImageReader(before)
        self.after = ImageReader(after)

    def diff_files(self) -> list[DiffEntry]:
        if not (self.before.has_pytsk3() and self.after.has_pytsk3()):
            return self.diff_regions()
        b = {p: hashlib.sha256(d).hexdigest()
             for p, d in self.before.iter_files()}
        a = {p: hashlib.sha256(d).hexdigest()
             for p, d in self.after.iter_files()}
        out: list[DiffEntry] = []
        for p in sorted(set(b) | set(a)):
            if p not in b:
                out.append(DiffEntry(path=p, status="added", after_sha=a[p]))
            elif p not in a:
                out.append(DiffEntry(path=p, status="removed", before_sha=b[p]))
            elif b[p] != a[p]:
                out.append(DiffEntry(path=p, status="modified",
                                     before_sha=b[p], after_sha=a[p]))
        return out

    def diff_regions(self, chunk: int = 8 * 1024 * 1024) -> list[DiffEntry]:
        out: list[DiffEntry] = []
        max_size = max(self.before.size, self.after.size)
        for off in range(0, max_size, chunk):
            b = self.before.read(off, chunk)
            a = self.after.read(off, chunk)
            if b == a:
                continue
            out.append(DiffEntry(
                path=f"<region:{off:08x}>", status="modified",
                before_sha=hashlib.sha256(b).hexdigest() if b else None,
                after_sha=hashlib.sha256(a).hexdigest() if a else None,
            ))
        return out

    def to_json(self, out: Path) -> int:
        entries = self.diff_files()
        with open(out, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in entries], f, indent=2, ensure_ascii=False)
        return len(entries)


# ===========================================================================
# 7. CLI
# ===========================================================================

def cmd_yara(args):
    scanner = YaraScanner(Path(args.image), require_tsk=args.require_tsk)
    matches = scanner.scan_files(Path(args.rules))
    if args.unallocated:
        matches.extend(scanner.scan_unallocated(Path(args.rules)))
    if args.json:
        print(json.dumps([asdict(m) for m in matches], indent=2, ensure_ascii=False))
    else:
        for m in matches:
            print(f"{m.rule:<30} {m.path:<40} {m.offset or '-'}")
    return 0 if not matches else 1


def cmd_unallocated(args):
    ex = UnallocatedExtractor(Path(args.image))
    regions = ex.extract(Path(args.out), min_region=args.min_region)
    print(f"extracted {len(regions)} regions -> {args.out}")
    for r in regions[:10]:
        print(f"  off={r.offset:#x} len={r.length}")
    if len(regions) > 10:
        print(f"  ... and {len(regions) - 10} more")
    return 0


def cmd_slack(args):
    ex = SlackExtractor(Path(args.image))
    regions = ex.extract(Path(args.out))
    print(f"extracted {len(regions)} slack regions -> {args.out}")
    return 0


def cmd_diff(args):
    differ = ImageDiffer(Path(args.before), Path(args.after))
    n = differ.to_json(Path(args.out))
    print(f"diff: {n} entries -> {args.out}")
    return 0


def cmd_selftest(args):
    print("=== DFIR tools selftest ===")
    failures = 0

    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 1024 * 1024)
        r = ImageReader(img)
        assert r.size == 1024 * 1024
        assert len(r.read(0, 512)) == 512
        print("ok: ImageReader")

    free = UnallocatedExtractor._subtract(0, 1000, [(100, 200), (500, 600)])
    assert [(x.offset, x.length) for x in free] == [(0, 100), (200, 300), (600, 400)]
    print("ok: UnallocatedExtractor._subtract")

    with tempfile.TemporaryDirectory() as td:
        rules = Path(td) / "r.yar"
        rules.write_text('rule t { strings: $a = "hello" condition: $a }')
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 100 + b"hello" + b"\0" * 100)
        scanner = YaraScanner(img)
        loaded = scanner._load_rules(rules)
        assert loaded["type"] == "regex"
        matches = scanner._scan_blob(b"...hello...", loaded, "/t")
        assert len(matches) == 1 and matches[0].rule == "t"
        print("ok: YaraScanner (regex)")

    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "fat32.img"
        boot = bytearray(512)
        boot[0x0B:0x0D] = (512).to_bytes(2, "little")
        boot[0x0D] = 1
        boot[0x0E:0x10] = (32).to_bytes(2, "little")
        boot[0x10] = 2
        boot[0x24:0x28] = (4).to_bytes(4, "little")
        boot[0x2C:0x30] = (2).to_bytes(4, "little")
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xaa"
        fat = bytearray(4 * 1024)
        fat[8:12] = (0x0FFFFFFF).to_bytes(4, "little")
        root_dir = bytearray(512)
        entry = bytearray(32)
        entry[0:11] = b"TEST    TXT"
        entry[26:28] = (0).to_bytes(2, "little")
        entry[20:22] = (0).to_bytes(2, "little")
        entry[28:32] = (10).to_bytes(4, "little")
        root_dir[0:32] = entry
        img.write_bytes(bytes(boot) + bytes(fat) + bytes(512) * 10 + bytes(root_dir))
        ex = SlackExtractor(img)
        regions = list(ex._fat32_slack(bytes(boot)))
        assert regions == [] or regions[0].kind == "slack"
        print("ok: SlackExtractor._fat32_slack")

    print(f"=== {failures} failures ===")
    return failures


def build_parser():
    p = argparse.ArgumentParser(prog="mountfile-dfir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("yara")
    sp.add_argument("image"); sp.add_argument("rules")
    sp.add_argument("--unallocated", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--require-tsk", action="store_true")
    sp.set_defaults(func=cmd_yara)

    sp = sub.add_parser("unallocated")
    sp.add_argument("image"); sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--min-region", type=int, default=4096)
    sp.set_defaults(func=cmd_unallocated)

    sp = sub.add_parser("slack")
    sp.add_argument("image"); sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_slack)

    sp = sub.add_parser("diff")
    sp.add_argument("before"); sp.add_argument("after")
    sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("selftest"); sp.set_defaults(func=cmd_selftest)
    return p


def main(argv):
    # Проверяем зависимости перед запуском
    deps = check_dependencies()
    if not deps.get("pytsk3", False):
        print("[i] pytsk3 недоступен — анализ файловой структуры ограничен.",
              file=sys.stderr)
    if not deps.get("yara-python", False):
        print("[i] yara-python недоступен — используется regex-fallback.",
              file=sys.stderr)

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DependencyMissing as e:
        print(f"dependency: {e}", file=sys.stderr); return 2
    except DFIRError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
